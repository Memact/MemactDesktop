from __future__ import annotations

import json
import os
import urllib.request
from typing import Iterable

from core.database import Event


def _engine_url() -> str:
    return os.environ.get("MEMACT_ENGINE_URL", "http://127.0.0.1:38454")


def engine_candidates(
    query: str,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 180,
) -> list[Event] | None:
    if os.environ.get("MEMACT_ENGINE_DISABLED"):
        return None
    payload = {
        "query": query,
        "start_at": start_at or "",
        "end_at": end_at or "",
        "limit": limit,
    }
    data = json.dumps(payload).encode("utf-8")
    url = _engine_url().rstrip("/") + "/query"
    timeout = float(os.environ.get("MEMACT_ENGINE_TIMEOUT", "0.45"))
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = response.read()
    except Exception:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    decoded: list[Event] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        try:
            decoded.append(Event(**item))
        except TypeError:
            continue
    return decoded


def first_available(candidates: Iterable[list[Event] | None]) -> list[Event] | None:
    for pool in candidates:
        if pool:
            return pool
    return None
