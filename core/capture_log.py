from __future__ import annotations

from core.database import list_events_between
from core.duration import resolve_time_range


def get_todays_captures() -> list[dict]:
    """Return today's captured events as plain dicts for display."""
    start_at, end_at = resolve_time_range("today")
    events = list_events_between(start_at, end_at, limit=500)
    result: list[dict] = []
    for event in reversed(events):
        result.append(
            {
                "occurred_at": event.occurred_at,
                "application": event.application.removesuffix(".exe"),
                "window_title": event.window_title,
                "url": event.url,
                "interaction_type": event.interaction_type,
                "has_full_text": bool(event.full_text),
                "keyphrases": event.keyphrases[:5],
            }
        )
    return result


def format_capture_summary(captures: list[dict]) -> str:
    """Return a short summary string for status display."""
    if not captures:
        return "Nothing captured yet today."
    apps = {capture["application"] for capture in captures}
    return (
        f"{len(captures)} moment{'s' if len(captures) != 1 else ''} "
        f"captured today across {len(apps)} app{'s' if len(apps) != 1 else ''}."
    )
