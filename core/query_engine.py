from __future__ import annotations

import json
import re
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse

from core.database import Event, lexical_candidates, list_events_around, list_events_between, list_recent_events
from core.semantic import cosine_similarity, embed_text, tokenize


_STOP_WORDS = {
    "a",
    "about",
    "am",
    "an",
    "and",
    "around",
    "at",
    "did",
    "do",
    "for",
    "have",
    "how",
    "i",
    "in",
    "is",
    "last",
    "me",
    "my",
    "of",
    "on",
    "the",
    "this",
    "time",
    "to",
    "today",
    "use",
    "was",
    "what",
    "when",
    "where",
    "which",
    "yesterday",
    "com",
    "net",
    "org",
    "io",
    "ai",
    "co",
    "dev",
    "app",
}


@dataclass(slots=True)
class EventMatch:
    event: Event
    score: float
    lexical_score: float
    semantic_score: float
    fuzzy_score: float
    phrase_match: bool
    entity_match: bool


@dataclass(slots=True)
class ActivitySpan:
    start_at: datetime
    end_at: datetime
    duration_seconds: int
    label: str
    session_title: str
    session_flow: str
    attention_cue: str | None
    tab_preview: list[str]
    application: str
    url: str | None
    events: list[Event]
    relevance: float
    snippet: str
    match_reason: str
    before_context: str | None
    after_context: str | None
    moment_summary: str


@dataclass(slots=True)
class SearchSuggestion:
    title: str
    subtitle: str
    completion: str
    category: str


@dataclass(slots=True)
class QueryAnswer:
    answer: str
    summary: str
    details_label: str
    evidence: list[ActivitySpan]
    time_scope_label: str | None
    result_count: int
    related_queries: list[str]


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return "local file"
    if parsed.netloc:
        return parsed.netloc.removeprefix("www.")
    return None


def _extract_domains(query: str) -> set[str]:
    if not query:
        return set()
    matches = re.findall(r"(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})", query.lower())
    domains = {match.removeprefix("www.").strip(".") for match in matches if match}
    return {domain for domain in domains if domain and "." in domain}


def _extract_app_hint(query: str, events: list[Event]) -> str | None:
    if not query or not events:
        return None
    lower = query.lower()
    seen: set[str] = set()
    candidates: list[str] = []
    for event in events:
        app = _friendly_app_name(event.application)
        key = app.casefold()
        if key in seen or len(key) < 3:
            continue
        seen.add(key)
        candidates.append(app)
    candidates.sort(key=len, reverse=True)
    for app in candidates:
        if app.lower() in lower:
            return app
    return None


def _event_matches_app(event: Event, app_hint: str | None) -> bool:
    if not app_hint:
        return False
    return _friendly_app_name(event.application).casefold() == app_hint.casefold()


def _estimate_duration_seconds(
    events: list[Event],
    *,
    match_event,
    max_gap_seconds: int = 6 * 60,
    min_step_seconds: int = 20,
) -> int:
    if not events:
        return 0
    ordered = sorted(events, key=lambda item: (item.occurred_at, item.id))
    total = 0
    for index, event in enumerate(ordered):
        if not match_event(event):
            continue
        current_time = _parse_timestamp(event.occurred_at)
        if index + 1 < len(ordered):
            next_time = _parse_timestamp(ordered[index + 1].occurred_at)
            delta = int((next_time - current_time).total_seconds())
        else:
            delta = min_step_seconds
        if delta <= 0:
            continue
        if delta > max_gap_seconds:
            continue
        total += max(delta, min_step_seconds)
    return total


def _filter_events(
    events: list[Event],
    *,
    target_domains: set[str],
    app_hint: str | None,
) -> list[Event]:
    if target_domains:
        filtered = [
            event
            for event in events
            if any(_event_matches_domain(event, domain) for domain in target_domains)
        ]
        if filtered:
            return filtered
    if app_hint:
        filtered = [event for event in events if _event_matches_app(event, app_hint)]
        if filtered:
            return filtered
    return events


def _span_matches_domain(span: ActivitySpan, domain: str) -> bool:
    if not domain:
        return False
    span_domain = (_domain(span.url) or "").lower()
    if span_domain == domain:
        return True
    if span_domain.endswith(f".{domain}"):
        return True
    label = _display_label(span).lower()
    return domain in label


def _event_matches_domain(event: Event, domain: str) -> bool:
    if not domain:
        return False
    event_domain = (_domain(event.url) or "").lower()
    if event_domain == domain:
        return True
    if event_domain.endswith(f".{domain}"):
        return True
    return domain in event.searchable_text.lower()


def _event_label(event: Event) -> str:
    domain = _domain(event.url)
    if domain:
        return domain
    title = (event.content_text or event.window_title or "").strip()
    if title:
        return title
    return event.application.removesuffix(".exe")


def _friendly_app_name(value: str) -> str:
    base = value.removesuffix(".exe")
    return base.replace("_", " ").title()


def _normalize_label(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip(" -|:"))
    if not text:
        return text
    parts = [part.strip() for part in re.split(r"\s*[-|:]\s*", text) if part.strip()]
    deduped_parts: list[str] = []
    seen_parts: set[str] = set()
    for part in parts:
        key = part.casefold()
        if key in seen_parts:
            continue
        deduped_parts.append(part)
        seen_parts.add(key)
    normalized = " - ".join(deduped_parts) if deduped_parts else text

    # Collapse repeated adjacent words like "Codex Codex" into one label.
    words = normalized.split()
    collapsed: list[str] = []
    previous_key = None
    for word in words:
        key = word.casefold()
        if key == previous_key:
            continue
        collapsed.append(word)
        previous_key = key
    return " ".join(collapsed)


def _dedupe_label_against_app(label: str, application: str) -> str:
    normalized = _normalize_label(label)
    if not normalized:
        return _friendly_app_name(application)
    app_name = _friendly_app_name(application)
    label_tokens = normalized.split()
    app_tokens = app_name.split()
    if app_tokens and len(label_tokens) >= len(app_tokens):
        if [token.casefold() for token in label_tokens[-len(app_tokens):]] == [
            token.casefold() for token in app_tokens
        ]:
            normalized = " ".join(label_tokens[:-len(app_tokens)]).strip()
    return normalized or app_name


def _display_label(span: ActivitySpan) -> str:
    app_name = _friendly_app_name(span.application)
    label = _dedupe_label_against_app(span.label, span.application)
    if not label:
        return app_name
    if label.casefold() == app_name.casefold():
        return app_name
    if app_name.casefold() in label.casefold():
        return label
    return label


def _unique_span_labels(spans: list[ActivitySpan], limit: int = 3) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for span in spans:
        label = _display_label(span)
        key = label.casefold()
        if not label or key in seen:
            continue
        unique.append(label)
        seen.add(key)
        if len(unique) >= limit:
            break
    return unique


def _unique_session_titles(spans: list[ActivitySpan], limit: int = 3) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for span in spans:
        title = span.session_title.strip()
        key = title.casefold()
        if not title or key in seen:
            continue
        unique.append(title)
        seen.add(key)
        if len(unique) >= limit:
            break
    return unique


def _format_duration(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} hours {minutes} minutes"
    if hours:
        return f"{hours} hours"
    if minutes:
        return f"{minutes} minutes"
    return "less than a minute"


def _format_clock(value: datetime) -> str:
    return value.strftime("%#I:%M %p" if value.strftime("%p") else "%H:%M")


def _meaningful_tokens(text: str) -> list[str]:
    return [token for token in tokenize(text) if token not in _STOP_WORDS]


def _time_window_for_query(query: str) -> tuple[datetime | None, datetime | None, str | None]:
    text = query.lower()
    today = date.today()
    label: str | None = None
    start: datetime | None = None
    end: datetime | None = None

    if "last week" in text:
        end_day = today - timedelta(days=today.weekday() + 1)
        start_day = end_day - timedelta(days=6)
        start = datetime.combine(start_day, time.min)
        end = datetime.combine(end_day, time.max)
        label = "last week"
    elif "this week" in text:
        start_day = today - timedelta(days=today.weekday())
        start = datetime.combine(start_day, time.min)
        end = datetime.combine(today, time.max)
        label = "this week"
    else:
        day = None
        if "yesterday" in text:
            day = today - timedelta(days=1)
            label = "yesterday"
        elif "today" in text:
            day = today
            label = "today"

        for bucket_label, bucket_start, bucket_end in (
            ("morning", time(5, 0), time(11, 59, 59)),
            ("afternoon", time(12, 0), time(16, 59, 59)),
            ("evening", time(17, 0), time(21, 59, 59)),
            ("tonight", time(18, 0), time(23, 59, 59)),
        ):
            if bucket_label in text:
                if day is None:
                    day = today
                start = datetime.combine(day, bucket_start)
                end = datetime.combine(day, bucket_end)
                label = f"{label} {bucket_label}".strip() if label else f"this {bucket_label}"
                break

        around_match = re.search(r"\b(?:around|at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
        if around_match:
            if day is None:
                day = today
            hour = int(around_match.group(1))
            minute = int(around_match.group(2) or 0)
            meridiem = around_match.group(3)
            if meridiem == "pm" and hour < 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0
            center = datetime.combine(day, time(hour % 24, minute))
            start = center - timedelta(minutes=45)
            end = center + timedelta(minutes=45)
            label = f"{label} around {_format_clock(center)}".strip() if label else f"around {_format_clock(center)}"
        elif day is not None and start is None and end is None:
            start = datetime.combine(day, time.min)
            end = datetime.combine(day, time.max)

    if start and end and start > end:
        start, end = end, start
    return start, end, label


def _load_candidate_events(query: str, start_at: datetime | None, end_at: datetime | None) -> list[Event]:
    start_text = start_at.isoformat(sep=" ", timespec="seconds") if start_at else None
    end_text = end_at.isoformat(sep=" ", timespec="seconds") if end_at else None
    recent_pool = list_events_between(start_text, end_text, limit=1200)
    lexical_pool = lexical_candidates(query, start_at=start_text, end_at=end_text, limit=180)
    fallback_pool = list_recent_events(limit=500)

    combined: list[Event] = []
    seen_ids: set[int] = set()
    for pool in (lexical_pool, recent_pool, fallback_pool):
        for event in pool:
            if event.id in seen_ids:
                continue
            combined.append(event)
            seen_ids.add(event.id)
    return combined


def _idf_by_token(events: list[Event]) -> dict[str, float]:
    document_frequency: Counter[str] = Counter()
    for event in events:
        for token in set(tokenize(event.searchable_text)):
            document_frequency[token] += 1
    total = max(len(events), 1)
    return {
        token: math.log((1 + total) / (1 + count)) + 1.0
        for token, count in document_frequency.items()
    }


def _fuzzy_overlap(query_tokens: list[str], event_tokens: set[str]) -> float:
    score = 0.0
    for token in query_tokens:
        if len(token) < 4 or token in event_tokens:
            continue
        best = 0.0
        for event_token in event_tokens:
            if abs(len(event_token) - len(token)) > 2:
                continue
            ratio = SequenceMatcher(None, token, event_token).ratio()
            if ratio > best:
                best = ratio
        if best >= 0.82:
            score += best
    return score


def _rank_events(
    query: str,
    events: list[Event],
    *,
    target_domains: set[str] | None = None,
    app_hint: str | None = None,
) -> list[EventMatch]:
    query_tokens = _meaningful_tokens(query)
    query_embedding = embed_text(query)
    normalized_query = " ".join(tokenize(query))
    idf = _idf_by_token(events)
    now = datetime.now()
    matches: list[EventMatch] = []
    for event in events:
        try:
            event_embedding = json.loads(event.embedding_json)
        except Exception:
            event_embedding = embed_text(event.searchable_text)
        semantic_score = max(cosine_similarity(query_embedding, event_embedding), 0.0)
        event_tokens = set(tokenize(event.searchable_text))
        lexical_score = sum(idf.get(token, 1.0) for token in query_tokens if token in event_tokens)
        fuzzy_score = _fuzzy_overlap(query_tokens, event_tokens)
        searchable_text = " ".join(tokenize(event.searchable_text))
        phrase_match = bool(normalized_query and normalized_query in searchable_text)
        domain = (_domain(event.url) or "").lower()
        app_name = _friendly_app_name(event.application).lower()
        entity_match = any(
            token in domain or token in app_name
            for token in query_tokens
            if len(token) >= 3
        )
        domain_match = False
        if target_domains:
            domain_match = any(_event_matches_domain(event, target) for target in target_domains)
        app_match = _event_matches_app(event, app_hint)
        try:
            age_hours = max((now - _parse_timestamp(event.occurred_at)).total_seconds() / 3600.0, 0.0)
        except ValueError:
            age_hours = 0.0
        recency_bonus = max(0.0, 0.12 - min(age_hours / 240.0, 0.12))
        interaction = event.interaction_type.casefold()
        action_bonus = 0.06 if interaction in {"focus", "app_switch", "navigate", "tab_switch", "context_change"} else 0.0
        heartbeat_penalty = -0.05 if "heartbeat" in interaction else 0.0
        score = (
            (semantic_score * 0.56)
            + (min(lexical_score, 4.0) * 0.16)
            + (min(fuzzy_score, 2.0) * 0.08)
            + (0.18 if phrase_match else 0.0)
            + (0.16 if entity_match else 0.0)
            + (0.22 if domain_match else 0.0)
            + (0.16 if app_match else 0.0)
            + recency_bonus
            + action_bonus
            + heartbeat_penalty
        )
        if target_domains and not domain_match:
            if semantic_score < 0.45 and lexical_score < 0.5 and fuzzy_score < 0.6:
                continue
            score *= 0.45
        if app_hint and not app_match:
            score *= 0.7
        if query_tokens and lexical_score == 0 and fuzzy_score == 0 and semantic_score < 0.18:
            continue
        if score <= 0.12:
            continue
        matches.append(
            EventMatch(
                event=event,
                score=score,
                lexical_score=lexical_score,
                semantic_score=semantic_score,
                fuzzy_score=fuzzy_score,
                phrase_match=phrase_match,
                entity_match=entity_match,
            )
        )
    matches.sort(key=lambda item: (item.score, item.event.occurred_at, item.event.id), reverse=True)
    return matches


def _span_key(event: Event) -> tuple[str, str, str]:
    return (
        event.application.lower(),
        (_domain(event.url) or "").lower(),
        (event.window_title or "").strip().lower(),
    )


def _best_event_for_span(events: list[Event], score_by_id: dict[int, float]) -> Event:
    return max(
        events,
        key=lambda event: (
            score_by_id.get(event.id, 0.0),
            len((event.content_text or "").strip()),
            len((event.window_title or "").strip()),
            event.id,
        ),
    )


def _snippet_from_event(event: Event) -> str:
    candidates = [
        (event.content_text or "").strip(),
        (event.window_title or "").strip(),
    ]
    if event.tab_titles:
        candidates.append(" | ".join(event.tab_titles[:3]))
    if event.url:
        candidates.append(event.url)
    for value in candidates:
        if not value:
            continue
        cleaned = re.sub(r"\s+", " ", value)
        if len(cleaned) > 140:
            return f"{cleaned[:137].rstrip()}..."
        return cleaned
    return _friendly_app_name(event.application)


def _context_label(event: Event) -> str:
    domain = _domain(event.url)
    if domain:
        return domain
    title = (event.window_title or event.content_text or "").strip()
    if title:
        return _normalize_label(title)
    return _friendly_app_name(event.application)


def _context_summary(
    events: list[Event],
    exclude_ids: set[int],
    *,
    context_filter=None,
) -> str | None:
    labels: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.id in exclude_ids:
            continue
        if context_filter is not None and not context_filter(event):
            continue
        label = _context_label(event)
        key = label.casefold()
        if not label or key in seen:
            continue
        labels.append(label)
        seen.add(key)
        if len(labels) >= 2:
            break
    if not labels:
        return None
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]} -> {labels[1]}"


def _moment_summary(
    label: str,
    application: str,
    before_context: str | None,
    after_context: str | None,
) -> str:
    primary = label or _friendly_app_name(application)
    if before_context and after_context:
        return f"{before_context} -> {primary} -> {after_context}"
    if before_context:
        return f"{before_context} -> {primary}"
    if after_context:
        return f"{primary} -> {after_context}"
    return primary


def _action_verb(interaction_types: set[str]) -> str | None:
    if "navigate" in interaction_types:
        return "Opened"
    if "tab_switch" in interaction_types:
        return "Switched to"
    if "app_switch" in interaction_types:
        return "Switched to"
    if "context_change" in interaction_types:
        return "Opened"
    return None


def _session_title(
    label: str,
    application: str,
    url: str | None,
    duration_seconds: int,
    *,
    interaction_types: set[str] | None = None,
) -> str:
    app_name = _friendly_app_name(application)
    clean_label = _dedupe_label_against_app(label, application)
    domain = _domain(url)
    lower_app = app_name.casefold()
    verb = _action_verb(interaction_types or set())

    if domain:
        if verb:
            return f"{verb} {domain}"
        if any(browser in lower_app for browser in ("edge", "chrome", "firefox", "browser", "safari", "brave")):
            return f"Browsing {domain}"
        return f"Using {domain} in {app_name}"

    if clean_label and clean_label.casefold() != app_name.casefold():
        if verb:
            return f"{verb} {clean_label}"
        if duration_seconds >= 8 * 60:
            return f"Working on {clean_label}"
        return f"Using {clean_label}"

    if verb:
        return f"{verb} {app_name}"
    if duration_seconds >= 8 * 60:
        return f"Working in {app_name}"
    return f"Using {app_name}"


def _tab_preview(events: list[Event]) -> list[str]:
    previews: list[str] = []
    seen: set[str] = set()
    for event in events:
        for title in event.tab_titles[:4]:
            clean = _normalize_label(title)
            key = clean.casefold()
            if not clean or key in seen:
                continue
            previews.append(clean)
            seen.add(key)
            if len(previews) >= 3:
                return previews
    return previews


def _attention_cue(events: list[Event], duration_seconds: int, url: str | None) -> str | None:
    if not events:
        return None
    interaction_types = {event.interaction_type.casefold() for event in events}
    has_focus = any(
        kind in {"focus", "app_switch", "navigate", "tab_switch", "context_change"}
        for kind in interaction_types
    )
    has_heartbeat = any("heartbeat" in kind for kind in interaction_types)
    if has_focus and has_heartbeat and duration_seconds >= 5 * 60:
        return "Stayed here for a sustained stretch"
    if has_focus and duration_seconds <= 90:
        return "Quick switch into this moment"
    if has_heartbeat and duration_seconds >= 12 * 60:
        return "Likely a deeper attention block"
    if url and any(event.tab_titles for event in events):
        return "Browser context captured with nearby tabs"
    return None


def _flow_label(span: ActivitySpan) -> str:
    domain = _domain(span.url)
    if domain:
        return domain
    title = span.session_title
    for prefix in ("Browsing ", "Using ", "Working on ", "Working in "):
        if title.startswith(prefix):
            return title.removeprefix(prefix)
    return _display_label(span)


def _annotate_session_flows(spans: list[ActivitySpan]) -> None:
    if not spans:
        return
    chronological = sorted(range(len(spans)), key=lambda index: spans[index].start_at)
    by_index = {position: original for position, original in enumerate(chronological)}
    ordered = [spans[index] for index in chronological]

    for position, span in enumerate(ordered):
        prev_span = ordered[position - 1] if position > 0 else None
        next_span = ordered[position + 1] if position + 1 < len(ordered) else None
        flow = span.session_title

        prev_gap = None
        if prev_span is not None:
            prev_gap = (span.start_at - prev_span.end_at).total_seconds()
        next_gap = None
        if next_span is not None:
            next_gap = (next_span.start_at - span.end_at).total_seconds()

        prev_valid = (
            prev_span is not None
            and prev_gap is not None
            and 0 <= prev_gap <= 20 * 60
            and _flow_label(prev_span).casefold() != _flow_label(span).casefold()
        )
        next_valid = (
            next_span is not None
            and next_gap is not None
            and 0 <= next_gap <= 20 * 60
            and _flow_label(next_span).casefold() != _flow_label(span).casefold()
        )

        if prev_valid and next_valid:
            flow = f"Moved from {_flow_label(prev_span)} to {_flow_label(next_span)} around {_flow_label(span)}"
        elif prev_valid:
            flow = f"Moved from {_flow_label(prev_span)} into {_flow_label(span)}"
        elif next_valid:
            flow = f"Started in {_flow_label(span)} and then moved to {_flow_label(next_span)}"

        spans[by_index[position]].session_flow = flow


def _match_reason(match: EventMatch | None) -> str:
    if match is None:
        return "Relevant local activity"
    if match.entity_match and match.phrase_match:
        return "Exact entity and phrase match"
    if match.entity_match:
        return "Strong app or site match"
    if match.phrase_match:
        return "Exact phrase match"
    if match.fuzzy_score >= 0.82:
        return "Recovered from close spelling match"
    if match.semantic_score >= 0.48:
        return "Strong semantic match"
    return "Relevant local activity"


def _build_spans(
    ranked: list[EventMatch],
    *,
    all_events: list[Event] | None = None,
    target_domains: set[str] | None = None,
    app_hint: str | None = None,
) -> list[ActivitySpan]:
    top_matches = ranked[:200]
    score_by_id = {match.event.id: match.score for match in top_matches}
    match_by_id = {match.event.id: match for match in top_matches}
    source_events = all_events if all_events is not None else [match.event for match in top_matches]
    ordered = sorted(source_events, key=lambda item: (item.occurred_at, item.id))
    spans: list[ActivitySpan] = []
    current_events: list[Event] = []
    current_key: tuple[str, str, str] | None = None

    def flush(next_start: datetime | None) -> None:
        nonlocal current_events, current_key
        if not current_events:
            return
        best_event = _best_event_for_span(current_events, score_by_id)
        first = current_events[0]
        start_at = _parse_timestamp(first.occurred_at)
        if next_start is None:
            end_at = start_at + timedelta(seconds=45)
        else:
            end_at = max(next_start, start_at + timedelta(seconds=20))
        duration_seconds = int((end_at - start_at).total_seconds())
        span_scores = [score_by_id.get(event.id, 0.0) for event in current_events]
        span_score = max(span_scores) if span_scores else 0.0
        if span_score <= 0.0:
            if target_domains and not any(
                _event_matches_domain(event, domain)
                for event in current_events
                for domain in target_domains
            ):
                current_events = []
                current_key = None
                return
            if app_hint and not any(_event_matches_app(event, app_hint) for event in current_events):
                current_events = []
                current_key = None
                return
            if not target_domains and not app_hint:
                current_events = []
                current_key = None
                return
        current_ids = {event.id for event in current_events}
        before_events, after_events = list_events_around(
            first.occurred_at,
            before_limit=6,
            after_limit=6,
        )
        context_filter = None
        if target_domains:
            context_filter = lambda event: any(
                _event_matches_domain(event, domain) for domain in target_domains
            )
        elif app_hint:
            context_filter = lambda event: _event_matches_app(event, app_hint)
        before_context = _context_summary(before_events, current_ids, context_filter=None)
        after_context = _context_summary(after_events, current_ids, context_filter=None)
        if context_filter is not None:
            before_context = _context_summary(before_events, current_ids, context_filter=context_filter) or before_context
            after_context = _context_summary(after_events, current_ids, context_filter=context_filter) or after_context
        display_label = _event_label(best_event)
        interaction_types = {event.interaction_type.casefold() for event in current_events}
        session_title = _session_title(
            display_label,
            best_event.application,
            best_event.url,
            duration_seconds,
            interaction_types=interaction_types,
        )
        tab_preview = _tab_preview(current_events)
        attention_cue = _attention_cue(current_events, duration_seconds, best_event.url)
        spans.append(
            ActivitySpan(
                start_at=start_at,
                end_at=end_at,
                duration_seconds=duration_seconds,
                label=display_label,
                session_title=session_title,
                session_flow=session_title,
                attention_cue=attention_cue,
                tab_preview=tab_preview,
                application=best_event.application,
                url=best_event.url,
                events=list(current_events),
                relevance=span_score,
                snippet=_snippet_from_event(best_event),
                match_reason=_match_reason(match_by_id.get(best_event.id)),
                before_context=before_context,
                after_context=after_context,
                moment_summary=_moment_summary(
                    _dedupe_label_against_app(display_label, best_event.application),
                    best_event.application,
                    before_context,
                    after_context,
                ),
            )
        )
        current_events = []
        current_key = None

    for index, event in enumerate(ordered):
        event_key = _span_key(event)
        event_time = _parse_timestamp(event.occurred_at)
        next_time = None
        if index + 1 < len(ordered):
            next_time = _parse_timestamp(ordered[index + 1].occurred_at)
        if not current_events:
            current_events = [event]
            current_key = event_key
            if next_time is None:
                flush(next_time)
            continue
        previous_time = _parse_timestamp(current_events[-1].occurred_at)
        gap_seconds = int((event_time - previous_time).total_seconds())
        if current_key == event_key and gap_seconds <= 360:
            current_events.append(event)
        else:
            flush(event_time)
            current_events = [event]
            current_key = event_key
        if next_time is None:
            flush(next_time)

    spans.sort(key=lambda span: (span.relevance, span.start_at), reverse=True)
    deduped: list[ActivitySpan] = []
    seen: set[tuple[str, str, str]] = set()
    for span in spans:
        key = (
            span.application.casefold(),
            (_domain(span.url) or "").casefold(),
            _display_label(span).casefold(),
        )
        if key in seen:
            continue
        deduped.append(span)
        seen.add(key)
    _annotate_session_flows(deduped)
    return deduped


def _duration_query(query: str) -> bool:
    text = query.lower()
    return any(
        phrase in text
        for phrase in ("how long", "how much time", "time on", "time spent", "hours", "minutes")
    )


def _yes_no_query(query: str) -> bool:
    text = query.strip().lower()
    return text.startswith(("did ", "have ", "was ", "were ", "do i ", "am i "))


def _last_time_query(query: str) -> bool:
    return "when did i" in query.lower() or "last time" in query.lower()


def _listing_query(query: str) -> bool:
    text = query.lower()
    return text.startswith("which ") or "what apps" in text or "what sites" in text


def _detect_intent(query: str) -> str:
    if _duration_query(query):
        return "duration"
    if _last_time_query(query):
        return "last_time"
    if _yes_no_query(query):
        return "yes_no"
    if _listing_query(query):
        return "listing"
    contextual_kind, _ = _contextual_recall_query(query)
    if contextual_kind:
        return "context"
    return "open"


def _low_confidence(spans: list[ActivitySpan]) -> bool:
    if not spans:
        return True
    top = spans[0].relevance
    if top < 0.2:
        return True
    if len(spans) > 1 and top < 0.3 and (top - spans[1].relevance) < 0.05:
        return True
    return False


def _contextual_recall_query(query: str) -> tuple[str | None, str | None]:
    text = query.strip().rstrip("?")
    patterns = (
        ("before", r"(?i)^what was i doing before (.+)$"),
        ("after", r"(?i)^what did i do after (.+)$"),
        ("around", r"(?i)^what else was open around (.+)$"),
        ("around", r"(?i)^what was open around (.+)$"),
        ("around", r"(?i)^what else was i doing around (.+)$"),
    )
    for kind, pattern in patterns:
        match = re.match(pattern, text)
        if match:
            anchor = match.group(1).strip()
            if anchor:
                return kind, anchor
    return None, None


def _summarize_detail(span: ActivitySpan) -> str:
    app_name = _friendly_app_name(span.application)
    if span.url:
        return f"{_format_clock(span.start_at)} to {_format_clock(span.end_at)} in {app_name} on {_domain(span.url) or span.url}"
    return f"{_format_clock(span.start_at)} to {_format_clock(span.end_at)} in {app_name}: {_display_label(span)}"


def _readable_context(value: str | None) -> str | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split("->") if part.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]}, then {parts[1]}"
    return f"{parts[0]}, then {parts[1]}, and later {parts[-1]}"


def _moment_hint(span: ActivitySpan) -> str | None:
    before_context = _readable_context(span.before_context)
    after_context = _readable_context(span.after_context)
    if before_context and after_context:
        return f"around {before_context} and then {after_context}"
    if before_context:
        return f"after {before_context}"
    if after_context:
        return f"before {after_context}"
    return None


def _flow_phrase(span: ActivitySpan) -> str:
    flow = span.session_flow.strip()
    mappings = (
        ("Browsing ", "you were browsing "),
        ("Using ", "you were using "),
        ("Working on ", "you were working on "),
        ("Working in ", "you were working in "),
        ("Started in ", "you started in "),
        ("Moved from ", "you moved from "),
    )
    for prefix, replacement in mappings:
        if flow.startswith(prefix):
            return replacement + flow.removeprefix(prefix)
    return f"you were in {flow.lower()}"


def _memory_summary(
    span: ActivitySpan,
    time_scope: str | None,
    *,
    include_context: bool = True,
) -> str:
    pieces: list[str] = []
    if time_scope:
        pieces.append(f"In {time_scope},")
    pieces.append(f"the strongest local moment suggests {_flow_phrase(span)}")
    if include_context:
        hint = _moment_hint(span)
        if hint:
            pieces.append(hint)
    return " ".join(pieces).replace(" ,", ",") + "."


def _moment_follow_ups(span: ActivitySpan, time_scope: str | None) -> list[str]:
    prompts: list[str] = []
    label = _display_label(span)
    if span.before_context:
        prompts.append(f"What was I doing before {label}?")
    if span.after_context:
        prompts.append(f"What did I do after {label}?")
    if span.before_context or span.after_context:
        prompts.append(f"What else was open around {label}?")
    elif time_scope:
        prompts.append(f"What else was I doing {time_scope}?")
    return prompts


def _query_summary(spans: list[ActivitySpan], time_scope: str | None) -> str:
    labels = _unique_session_titles(spans, limit=3)
    if not labels:
        labels = _unique_span_labels(spans, limit=3)
    if not labels:
        labels = [_friendly_app_name(span.application) for span in spans[:3]]
    count_text = f"{len(spans)} strong local matches"
    if time_scope:
        count_text = f"{count_text} in {time_scope}"
    if not labels:
        return count_text
    if len(labels) == 1:
        return f"{count_text}. Best match: {labels[0]}."
    return f"{count_text}. Top matches include {', '.join(labels[:-1])}, and {labels[-1]}."


def _duration_summary(
    *,
    time_scope: str | None,
    label: str | None,
    best_span: ActivitySpan | None,
) -> str:
    scope = f" in {time_scope}" if time_scope else ""
    if label:
        base = f"Based on local activity for {label}{scope}."
    else:
        base = f"Based on local activity{scope}."
    if best_span:
        return f"{base} Best matching moment: {best_span.session_title}."
    return base


def _build_related_queries(
    query: str,
    spans: list[ActivitySpan],
    time_scope: str | None,
    *,
    target_domains: set[str] | None = None,
    app_hint: str | None = None,
) -> list[str]:
    prompts: list[str] = []
    target_label = None
    if target_domains:
        target_label = sorted(target_domains)[0]
    elif app_hint:
        target_label = app_hint
    for span in spans[:3]:
        label = _display_label(span)
        app = _friendly_app_name(span.application)
        if not target_label:
            prompts.extend(_moment_follow_ups(span, time_scope))
        if span.url:
            domain = _domain(span.url) or label
            prompts.append(f"How much time did I spend on {domain} today?")
            prompts.append(f"When did I last use {domain}?")
        prompts.append(f"When did I last use {app}?")
        prompts.append(f"Did I use {label} today?")
    if target_label:
        prompts.insert(0, f"When did I last use {target_label}?")
        prompts.insert(1, f"How much time did I spend on {target_label} today?")
    if time_scope:
        prompts.append(f"What else was I doing {time_scope}?")
    prompts.append(f"What did I do after {query.strip('?')}?")

    deduped: list[str] = []
    seen: set[str] = set()
    for prompt in prompts:
        key = prompt.casefold()
        if key in seen or key == query.strip().casefold():
            continue
        deduped.append(prompt)
        seen.add(key)
        if len(deduped) >= 3:
            break
    return deduped


def answer_query(query: str) -> QueryAnswer:
    if not query.strip():
        return QueryAnswer(
            answer="Ask a question about what you have been doing.",
            summary="Memact searches only your local activity history.",
            details_label="",
            evidence=[],
            time_scope_label=None,
            result_count=0,
            related_queries=[],
        )

    start_at, end_at, time_scope = _time_window_for_query(query)
    candidates = _load_candidate_events(query, start_at, end_at)
    target_domains = _extract_domains(query)
    app_hint = _extract_app_hint(query, candidates)
    filtered_candidates = _filter_events(
        candidates,
        target_domains=target_domains,
        app_hint=app_hint,
    )
    ranked = _rank_events(
        query,
        filtered_candidates,
        target_domains=target_domains,
        app_hint=app_hint,
    )
    if not ranked and filtered_candidates is not candidates:
        ranked = _rank_events(
            query,
            candidates,
            target_domains=target_domains,
            app_hint=app_hint,
        )
    if not ranked:
        return QueryAnswer(
            answer="I could not find a strong local memory for that yet.",
            summary="Try a clearer app name, site, or time window like today, yesterday evening, or around 3 PM.",
            details_label="",
            evidence=[],
            time_scope_label=time_scope,
            result_count=0,
            related_queries=[],
        )

    intent = _detect_intent(query)
    spans = _build_spans(
        ranked,
        all_events=filtered_candidates,
        target_domains=target_domains,
        app_hint=app_hint,
    )
    if not spans:
        return QueryAnswer(
            answer="I found events, but not enough structure to answer clearly yet.",
            summary="There are matching events in local memory, but they are too weak or fragmented to summarize cleanly.",
            details_label="",
            evidence=[],
            time_scope_label=time_scope,
            result_count=len(ranked),
            related_queries=[],
        )

    relevant_spans = [span for span in spans if span.relevance >= max(spans[0].relevance * 0.42, 0.22)]
    if not relevant_spans:
        relevant_spans = spans[:4]

    if target_domains:
        domain_spans = [
            span
            for span in spans
            if any(_span_matches_domain(span, domain) for domain in target_domains)
        ]
        if domain_spans:
            relevant_spans = domain_spans

    summary = _query_summary(relevant_spans, time_scope)
    related_queries = _build_related_queries(
        query,
        relevant_spans,
        time_scope,
        target_domains=target_domains,
        app_hint=app_hint,
    )
    if intent in {"open", "listing"} and _low_confidence(relevant_spans):
        return QueryAnswer(
            answer="I do not have a strong local memory for that yet.",
            summary="These are the closest local moments I could find.",
            details_label="Show closest matches",
            evidence=relevant_spans[:6],
            time_scope_label=time_scope,
            result_count=len(ranked),
            related_queries=related_queries,
        )
    contextual_kind, contextual_anchor = _contextual_recall_query(query)

    if contextual_kind and contextual_anchor:
        anchor_candidates = _load_candidate_events(contextual_anchor, start_at, end_at)
        anchor_domains = _extract_domains(contextual_anchor)
        anchor_app_hint = _extract_app_hint(contextual_anchor, anchor_candidates)
        anchor_ranked = _rank_events(
            contextual_anchor,
            anchor_candidates,
            target_domains=anchor_domains,
            app_hint=anchor_app_hint,
        )
        anchor_spans = (
            _build_spans(
                anchor_ranked,
                all_events=anchor_candidates,
                target_domains=anchor_domains,
                app_hint=anchor_app_hint,
            )
            if anchor_ranked
            else []
        )
        if anchor_spans:
            anchor = anchor_spans[0]
            answer = "I could not recover enough surrounding context for that moment."
            contextual_summary = _memory_summary(anchor, time_scope, include_context=not bool(target_domains))
            if contextual_kind == "before":
                if anchor.before_context:
                    answer = anchor.before_context
                    contextual_summary = f"Right before {anchor.session_title.lower()}, your local activity pointed to {anchor.before_context}."
            elif contextual_kind == "after":
                if anchor.after_context:
                    answer = anchor.after_context
                    contextual_summary = f"Right after {anchor.session_title.lower()}, your local activity shifted to {anchor.after_context}."
            else:
                around_parts = [part for part in (anchor.before_context, anchor.after_context) if part]
                if around_parts:
                    answer = " then ".join(around_parts)
                    contextual_summary = f"Around {anchor.session_title.lower()}, nearby context included {' and then '.join(around_parts)}."
            return QueryAnswer(
                answer=answer,
                summary=contextual_summary,
                details_label="Show anchor moment",
                evidence=anchor_spans[:1],
                time_scope_label=time_scope,
                result_count=len(anchor_ranked),
                related_queries=_moment_follow_ups(anchor, time_scope),
            )

    if _duration_query(query):
        duration_spans = relevant_spans
        if target_domains:
            filtered = [
                span
                for span in spans
                if any(_span_matches_domain(span, domain) for domain in target_domains)
            ]
            if filtered:
                duration_spans = filtered

        start_text = start_at.isoformat(sep=" ", timespec="seconds") if start_at else None
        end_text = end_at.isoformat(sep=" ", timespec="seconds") if end_at else None
        duration_events = (
            list_events_between(start_text, end_text, limit=4000)
            if start_text or end_text
            else list_recent_events(limit=1600)
        )
        if target_domains:
            total_seconds = _estimate_duration_seconds(
                duration_events,
                match_event=lambda event: any(
                    _event_matches_domain(event, domain) for domain in target_domains
                ),
            )
        elif app_hint:
            total_seconds = _estimate_duration_seconds(
                duration_events,
                match_event=lambda event: _event_matches_app(event, app_hint),
            )
        else:
            total_seconds = sum(span.duration_seconds for span in duration_spans)
        answer = _format_duration(total_seconds)
        if time_scope:
            answer = f"{answer} in {time_scope}"
        label = None
        if target_domains:
            label = sorted(target_domains)[0]
        elif app_hint:
            label = app_hint
        detail_summary = _duration_summary(
            time_scope=time_scope,
            label=label,
            best_span=duration_spans[0] if duration_spans else None,
        )
        return QueryAnswer(
            answer=answer,
            summary=detail_summary,
            details_label="Show top matches",
            evidence=duration_spans[:6],
            time_scope_label=time_scope,
            result_count=len(ranked),
            related_queries=related_queries,
        )

    if _last_time_query(query):
        span = relevant_spans[0]
        answer = f"{_format_clock(span.start_at)} on {span.start_at.strftime('%b %d')}"
        return QueryAnswer(
            answer=answer,
            summary=_memory_summary(span, time_scope, include_context=not bool(target_domains)),
            details_label="Show top matches",
            evidence=relevant_spans[:6],
            time_scope_label=time_scope,
            result_count=len(ranked),
            related_queries=related_queries,
        )

    if _yes_no_query(query):
        strongest = relevant_spans[0]
        threshold = 0.25 if time_scope else 0.31
        answer = "I do not have clear evidence for that."
        summary = _memory_summary(strongest, time_scope, include_context=not bool(target_domains))
        if strongest.relevance >= threshold:
            answer = f"Yes, most likely around {_format_clock(strongest.start_at)}."
        else:
            summary = (
                f"I did not find a strong signal, but the closest local moment suggests {_flow_phrase(strongest)}."
            )
        return QueryAnswer(
            answer=answer,
            summary=summary,
            details_label="Show top matches",
            evidence=relevant_spans[:6],
            time_scope_label=time_scope,
            result_count=len(ranked),
            related_queries=related_queries,
        )

    if _listing_query(query):
        labels = _unique_session_titles(relevant_spans, limit=5)
        if not labels:
            labels = _unique_span_labels(relevant_spans, limit=5)
        if not labels:
            labels = [_friendly_app_name(span.application) for span in relevant_spans[:5]]
        answer = ", ".join(labels[:5]) if labels else "I found matching local activity."
        if relevant_spans:
            summary = _memory_summary(
                relevant_spans[0],
                time_scope,
                include_context=not bool(target_domains),
            )
        return QueryAnswer(
            answer=answer,
            summary=summary,
            details_label="Show top matches",
            evidence=relevant_spans[:6],
            time_scope_label=time_scope,
            result_count=len(ranked),
            related_queries=related_queries,
        )

    top_spans = relevant_spans[:3]
    if time_scope and len(_meaningful_tokens(query)) <= 4:
        phrases = [_summarize_detail(span) for span in top_spans]
        answer = " ; ".join(phrases)
        summary = _memory_summary(
            top_spans[0],
            time_scope,
            include_context=not bool(target_domains),
        )
    else:
        labels = _unique_session_titles(top_spans, limit=3)
        if not labels:
            labels = _unique_span_labels(top_spans, limit=3)
        if not labels:
            labels = [_friendly_app_name(span.application) for span in top_spans[:2]]
        if len(labels) == 1:
            answer = f"I found a local moment where {_flow_phrase(top_spans[0])}."
        elif len(labels) == 2:
            answer = f"I found local moments around {labels[0].lower()} and {labels[1].lower()}."
        else:
            lowered = [label.lower() for label in labels]
            answer = f"I found local moments around {', '.join(lowered[:-1])}, and {lowered[-1]}."
        if top_spans:
            summary = _memory_summary(
                top_spans[0],
                time_scope,
                include_context=not bool(target_domains),
            )
    return QueryAnswer(
        answer=answer,
        summary=summary,
        details_label="Show top matches",
        evidence=relevant_spans[:6],
        time_scope_label=time_scope,
        result_count=len(ranked),
        related_queries=related_queries,
    )


def dynamic_suggestions(limit: int = 4) -> list[SearchSuggestion]:
    events = list_recent_events(limit=120)
    if not events:
        return [
            SearchSuggestion(
                title="What was I doing today?",
                subtitle="Broad overview of your latest activity.",
                completion="What was I doing today?",
                category="Suggested",
            ),
            SearchSuggestion(
                title="What did I do yesterday evening?",
                subtitle="Good for day-part recall.",
                completion="What did I do yesterday evening?",
                category="Suggested",
            ),
            SearchSuggestion(
                title="When did I last use my browser?",
                subtitle="Find the latest browser activity.",
                completion="When did I last use my browser?",
                category="Suggested",
            ),
        ][:limit]

    apps = Counter()
    domains = Counter()
    for event in events:
        apps[_friendly_app_name(event.application)] += 1
        domain = _domain(event.url)
        if domain:
            domains[domain] += 1

    suggestions: list[SearchSuggestion] = []
    if domains:
        prompt = f"How much time did I spend on {domains.most_common(1)[0][0]} today?"
        suggestions.append(
            SearchSuggestion(
                title=prompt,
                subtitle="Estimate time spent on a specific site.",
                completion=prompt,
                category="Frequent site",
            )
        )
    if apps:
        prompt = f"When did I last use {apps.most_common(1)[0][0]}?"
        suggestions.append(
            SearchSuggestion(
                title=prompt,
                subtitle="Jump straight to the latest app usage.",
                completion=prompt,
                category="Frequent app",
            )
        )
    for prompt, subtitle in (
        ("What was I doing yesterday evening?", "Look at a recent time slice."),
        ("What did I work on this week?", "Summarize broader work patterns."),
        ("Did I open GitHub today?", "Ask a direct yes or no question."),
    ):
        suggestions.append(
            SearchSuggestion(
                title=prompt,
                subtitle=subtitle,
                completion=prompt,
                category="Suggested",
            )
        )

    deduped: list[SearchSuggestion] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        if suggestion.completion.casefold() not in seen:
            deduped.append(suggestion)
            seen.add(suggestion.completion.casefold())
        if len(deduped) >= limit:
            break
    return deduped


def autocomplete_suggestions(prefix: str, limit: int = 5) -> list[SearchSuggestion]:
    typed = prefix.strip()
    if not typed:
        return []

    lower = typed.lower()
    lexical = lexical_candidates(typed, limit=32)
    pool = lexical or list_recent_events(limit=80)

    entity_counts = Counter[str]()
    for event in pool:
        for label in (_domain(event.url), _friendly_app_name(event.application), _event_label(event)):
            if label and len(label.strip()) >= 3:
                entity_counts[label.strip()] += 1

    entity_suggestions: list[SearchSuggestion] = []
    token_matches = _meaningful_tokens(lower)
    for label, _count in entity_counts.most_common(8):
        label_lower = label.lower()
        if token_matches and not any(token in label_lower for token in token_matches):
            continue
        entity_suggestions.extend(
            [
                SearchSuggestion(
                    title=f"When did I last use {label}?",
                    subtitle="Direct lookup for the latest matching activity.",
                    completion=f"When did I last use {label}?",
                    category="Quick answer",
                ),
                SearchSuggestion(
                    title=f"How much time did I spend on {label} today?",
                    subtitle="Estimate time spent in the current day.",
                    completion=f"How much time did I spend on {label} today?",
                    category="Time analysis",
                ),
                SearchSuggestion(
                    title=f"Did I use {label} today?",
                    subtitle="Binary check against recent activity.",
                    completion=f"Did I use {label} today?",
                    category="Verification",
                ),
            ]
        )

    intent_map = {
        "what": [
            SearchSuggestion(
                title="What was I doing today?",
                subtitle="Overview of current-day activity.",
                completion="What was I doing today?",
                category="Explore",
            ),
            SearchSuggestion(
                title="What did I do yesterday evening?",
                subtitle="Focus on a specific time window.",
                completion="What did I do yesterday evening?",
                category="Explore",
            ),
        ],
        "when": [
            SearchSuggestion(
                title="When did I last use Chrome?",
                subtitle="Find the latest app or site usage.",
                completion="When did I last use Chrome?",
                category="Quick answer",
            ),
            SearchSuggestion(
                title="When did I last visit GitHub?",
                subtitle="Resolve recent site activity quickly.",
                completion="When did I last visit GitHub?",
                category="Quick answer",
            ),
        ],
        "how": [
            SearchSuggestion(
                title="How much time did I spend on YouTube today?",
                subtitle="Estimate duration from grouped events.",
                completion="How much time did I spend on YouTube today?",
                category="Time analysis",
            ),
            SearchSuggestion(
                title="How long was I coding today?",
                subtitle="Measure time spent in a work session.",
                completion="How long was I coding today?",
                category="Time analysis",
            ),
        ],
        "did": [
            SearchSuggestion(
                title="Did I open GitHub today?",
                subtitle="Check whether an action likely happened.",
                completion="Did I open GitHub today?",
                category="Verification",
            ),
            SearchSuggestion(
                title="Did I use Discord today?",
                subtitle="Verify activity from local events.",
                completion="Did I use Discord today?",
                category="Verification",
            ),
        ],
        "which": [
            SearchSuggestion(
                title="Which apps did I use today?",
                subtitle="List distinct apps from local history.",
                completion="Which apps did I use today?",
                category="Explore",
            ),
            SearchSuggestion(
                title="Which sites did I visit today?",
                subtitle="List distinct domains from browser activity.",
                completion="Which sites did I visit today?",
                category="Explore",
            ),
        ],
    }

    candidates: list[SearchSuggestion] = []
    if token_matches and entity_suggestions:
        candidates.extend(entity_suggestions)
    else:
        for key, suggestions in intent_map.items():
            if lower.startswith(key):
                candidates.extend(suggestions)
                break
        candidates.extend(entity_suggestions)
    if not candidates:
        candidates.extend(dynamic_suggestions(limit=limit))

    deduped: list[SearchSuggestion] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.completion.casefold() in seen:
            continue
        if not lower.startswith(("what", "when", "how", "did", "which")) and lower not in candidate.completion.lower():
            continue
        deduped.append(candidate)
        seen.add(candidate.completion.casefold())
        if len(deduped) >= limit:
            break
    return deduped
