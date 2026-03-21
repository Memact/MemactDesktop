from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from pywinauto import Desktop

from core.browser_bridge import BrowserStateStore
from core.database import append_event
from core.settings import load_excluded_apps


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
logger = logging.getLogger(__name__)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
MAX_PATH = 260
BROWSERS = (
    "chrome.exe",
    "msedge.exe",
    "brave.exe",
    "opera.exe",
    "launcher.exe",
    "vivaldi.exe",
)
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
KNOWN_NOTIFICATION_CLASSES = {
    "Windows.UI.Core.CoreWindow",
    "XamlExplorerHostIslandWindow",
    "NotifyIconOverflowWindow",
    "Shell_TrayWnd",
    "Windows.UI.Composition.DesktopWindowContentBridge",
}
NOTIFICATION_TOKENS = {
    "notification",
    "notifications",
    "toast",
    "reminder",
    "h.notifyicon",
    "notifyicon",
    "battery is running low",
    "not responding",
}
MEMACT_WINDOW_TOKENS = {
    "memact",
    "ask memact",
    "privacy promise",
    "privacy notice",
}


@dataclass(slots=True)
class WindowSnapshot:
    hwnd: int
    title: str
    app_name: str
    exe_path: str | None
    class_name: str
    ex_style: int


@dataclass(slots=True)
class BrowserContext:
    url: str | None
    current_title: str | None
    tab_titles: list[str]
    tab_urls: list[str]
    page_title: str | None
    page_description: str | None
    page_h1: str | None
    page_snippet: str | None
    full_text: str | None
    selection_text: str | None
    active_tag: str | None
    active_type: str | None
    is_typing: bool
    is_scrolling: bool


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _process_image_path(pid: int) -> str | None:
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
        False,
        pid,
    )
    if not handle:
        return None

    try:
        buffer = ctypes.create_unicode_buffer(MAX_PATH)
        copied = psapi.GetModuleFileNameExW(handle, None, buffer, MAX_PATH)
        if copied:
            return buffer.value

        size = ctypes.wintypes.DWORD(MAX_PATH)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        if ok:
            return buffer.value[: size.value]
    finally:
        kernel32.CloseHandle(handle)

    return None


def get_active_window() -> WindowSnapshot | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    title = _window_text(hwnd)
    if not title:
        return None

    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe_path = _process_image_path(pid.value)
    app_name = os.path.basename(exe_path) if exe_path else "Unknown"
    return WindowSnapshot(
        hwnd=hwnd,
        title=title,
        app_name=app_name,
        exe_path=exe_path,
        class_name=_class_name(hwnd),
        ex_style=user32.GetWindowLongW(hwnd, GWL_EXSTYLE),
    )


def should_capture_window(snapshot: WindowSnapshot) -> bool:
    title = snapshot.title.strip().lower()
    app_lower = snapshot.app_name.lower()
    if not title:
        return False
    if title in MEMACT_WINDOW_TOKENS:
        return False
    if snapshot.class_name in KNOWN_NOTIFICATION_CLASSES:
        return False
    if app_lower in {"python.exe", "pythonw.exe"} and any(token in title for token in MEMACT_WINDOW_TOKENS):
        return False
    if title.startswith("h.notifyicon_"):
        return False
    if app_lower in {"pickerhost.exe", "shellexperiencehost.exe"}:
        return False
    if snapshot.ex_style & WS_EX_TOOLWINDOW and app_lower not in BROWSERS:
        return False
    if any(token in title for token in NOTIFICATION_TOKENS):
        return False
    return True


def _read_edit_value(control) -> str:
    try:
        value = control.get_value()
        if isinstance(value, str):
            return value.strip()
    except Exception:
        pass
    try:
        value = control.iface_value.CurrentValue
        if isinstance(value, str):
            return value.strip()
    except Exception:
        pass
    try:
        texts = control.texts()
        if texts:
            return str(texts[0]).strip()
    except Exception:
        pass
    return ""


def _control_name(control) -> str:
    try:
        value = control.window_text()
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass
    try:
        texts = control.texts()
        if texts:
            return str(texts[0]).strip()
    except Exception:
        pass
    try:
        value = control.element_info.name
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass
    return ""


def _is_selected_tab(control) -> bool:
    try:
        return bool(control.iface_selection_item.CurrentIsSelected)
    except Exception:
        return False


def _is_generic_tab_title(title: str) -> bool:
    value = title.strip().lower()
    if not value:
        return True
    if re.fullmatch(r"tab-\d+", value):
        return True
    return value in {"new tab", "tab", "tab search", "tab actions menu"}


def _normalize_browser_title(window_title: str, url: str | None, selected_title: str | None) -> str:
    if selected_title and not _is_generic_tab_title(selected_title):
        return selected_title.strip()
    title_root = window_title.split(" - ")[0].strip()
    if title_root and " and " not in title_root.lower():
        return title_root
    parsed = urlparse(url or "")
    if parsed.scheme == "file":
        return os.path.basename(parsed.path) or "Local file"
    if parsed.netloc:
        return parsed.netloc.removeprefix("www.")
    return window_title.strip()


def get_browser_context(hwnd: int, app_name: str, window_title: str) -> BrowserContext:
    app_name = app_name.lower()
    if not any(browser in app_name for browser in BROWSERS):
        return BrowserContext(
            url=None,
            current_title=None,
            tab_titles=[],
            tab_urls=[],
            page_title=None,
            page_description=None,
            page_h1=None,
            page_snippet=None,
            full_text=None,
            selection_text=None,
            active_tag=None,
            active_type=None,
            is_typing=False,
            is_scrolling=False,
        )

    url = None
    tab_titles: list[str] = []
    tab_urls: list[str] = []
    selected_title = None
    try:
        window = Desktop(backend="uia").window(handle=hwnd)
        for control in window.descendants(control_type="Edit"):
            value = _read_edit_value(control)
            if value.startswith(("http://", "https://", "file://")):
                url = value
                break

        seen_titles: set[str] = set()
        for control in window.descendants(control_type="TabItem"):
            title = _control_name(control)
            if not title or _is_generic_tab_title(title) or title in seen_titles:
                continue
            seen_titles.add(title)
            tab_titles.append(title)
            if _is_selected_tab(control):
                selected_title = title
    except Exception:
        return BrowserContext(
            url=url,
            current_title=None,
            tab_titles=tab_titles,
            tab_urls=tab_urls,
            page_title=None,
            page_description=None,
            page_h1=None,
            page_snippet=None,
            full_text=None,
            selection_text=None,
            active_tag=None,
            active_type=None,
            is_typing=False,
            is_scrolling=False,
        )

    return BrowserContext(
        url=url,
        current_title=_normalize_browser_title(window_title, url, selected_title),
        tab_titles=tab_titles,
        tab_urls=tab_urls,
        page_title=None,
        page_description=None,
        page_h1=None,
        page_snippet=None,
        full_text=None,
        selection_text=None,
        active_tag=None,
        active_type=None,
        is_typing=False,
        is_scrolling=False,
    )


def _browser_key(app_name: str) -> str:
    app_name = app_name.lower()
    if "msedge" in app_name:
        return "edge"
    if "chrome" in app_name:
        return "chrome"
    if "brave" in app_name:
        return "brave"
    if "vivaldi" in app_name:
        return "vivaldi"
    if app_name in {"opera.exe", "launcher.exe"}:
        return "opera"
    return ""


def _browser_context_from_extension(snapshot: WindowSnapshot, store: BrowserStateStore | None) -> BrowserContext | None:
    if store is None:
        return None
    browser_key = _browser_key(snapshot.app_name)
    if not browser_key:
        return None
    session = store.get(browser_key)
    if session is None:
        return None
    return BrowserContext(
        url=session.current_url,
        current_title=_normalize_browser_title(
            snapshot.title,
            session.current_url,
            session.current_title,
        ),
        tab_titles=session.tab_titles,
        tab_urls=session.tab_urls,
        page_title=session.page_title,
        page_description=session.page_description,
        page_h1=session.page_h1,
        page_snippet=session.page_snippet,
        full_text=session.full_text,
        selection_text=session.selection_text,
        active_tag=session.active_tag,
        active_type=session.active_type,
        is_typing=session.typing_active,
        is_scrolling=session.scrolling_active,
    )


def _compose_content_text(snapshot: WindowSnapshot, browser_context: BrowserContext) -> str:
    parts = [
        "typing" if browser_context.is_typing else "",
        "scrolling" if browser_context.is_scrolling else "",
        browser_context.selection_text or "",
        browser_context.page_title or "",
        browser_context.page_description or "",
        browser_context.page_h1 or "",
        browser_context.page_snippet or "",
        browser_context.current_title or snapshot.title,
        snapshot.title,
        " ".join(browser_context.tab_titles[:8]),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


class WindowMonitor(threading.Thread):
    def __init__(
        self,
        on_new_event=None,
        poll_interval: float = 1.0,
        heartbeat_interval: float = 20.0,
        browser_state_store: BrowserStateStore | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.on_new_event = on_new_event
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.browser_state_store = browser_state_store
        self._stop_event = threading.Event()
        self._excluded_apps: set[str] = load_excluded_apps()
        self._last_fingerprint: tuple[str, ...] | None = None
        self._last_recorded_at = 0.0
        self._last_browser_probe_key: tuple[str, str] | None = None
        self._last_browser_probe_at = 0.0
        self._last_browser_probe_context = BrowserContext(
            url=None,
            current_title=None,
            tab_titles=[],
            tab_urls=[],
            page_title=None,
            page_description=None,
            page_h1=None,
            page_snippet=None,
            full_text=None,
            selection_text=None,
            active_tag=None,
            active_type=None,
            is_typing=False,
            is_scrolling=False,
        )
        self._last_snapshot: WindowSnapshot | None = None
        self._last_browser_context: BrowserContext | None = None
        self._last_activity_kind: str | None = None
        self._last_activity_at = 0.0
        self._last_error_at = 0.0

    def stop(self) -> None:
        self._stop_event.set()

    def reload_excluded_apps(self) -> None:
        self._excluded_apps = load_excluded_apps()

    def _emit_event(self, snapshot: WindowSnapshot, browser_context: BrowserContext, interaction_type: str) -> None:
        append_event(
            application=snapshot.app_name,
            window_title=snapshot.title,
            url=browser_context.url,
            interaction_type=interaction_type,
            content_text=_compose_content_text(snapshot, browser_context),
            full_text=browser_context.full_text,
            exe_path=snapshot.exe_path,
            tab_titles=browser_context.tab_titles,
            tab_urls=browser_context.tab_urls,
            source="monitor",
        )
        self._last_recorded_at = time.monotonic()
        if self.on_new_event is not None:
            self.on_new_event()

    def _classify_interaction(
        self,
        snapshot: WindowSnapshot,
        browser_context: BrowserContext,
    ) -> str:
        if browser_context.is_typing:
            return "typing"
        if browser_context.is_scrolling:
            return "scrolling"
        if self._last_snapshot is None:
            return "focus"
        if snapshot.app_name.lower() != self._last_snapshot.app_name.lower():
            return "app_switch"
        last_context = self._last_browser_context
        if (
            browser_context.url
            and last_context
            and browser_context.url != last_context.url
        ):
            return "navigate"
        if (
            browser_context.current_title
            and last_context
            and browser_context.current_title != last_context.current_title
        ):
            return "tab_switch"
        if snapshot.title.strip() != (self._last_snapshot.title or "").strip():
            return "context_change"
        return "focus"

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = get_active_window()
                if snapshot is not None and should_capture_window(snapshot):
                    if snapshot.app_name.lower() in self._excluded_apps:
                        time.sleep(self.poll_interval)
                        continue
                    browser_context = _browser_context_from_extension(snapshot, self.browser_state_store)
                    if browser_context is None:
                        probe_key = (snapshot.app_name.lower(), snapshot.title)
                        now = time.monotonic()
                        if self._last_browser_probe_key == probe_key and now - self._last_browser_probe_at < 2.0:
                            browser_context = self._last_browser_probe_context
                        else:
                            browser_context = get_browser_context(snapshot.hwnd, snapshot.app_name, snapshot.title)
                            self._last_browser_probe_key = probe_key
                            self._last_browser_probe_at = now
                            self._last_browser_probe_context = browser_context

                    fingerprint = (
                        snapshot.app_name.lower(),
                        snapshot.title.strip().lower(),
                        (browser_context.url or "").strip().lower(),
                        (browser_context.current_title or "").strip().lower(),
                    )
                    now = time.monotonic()
                    if fingerprint != self._last_fingerprint:
                        self._last_fingerprint = fingerprint
                        interaction_type = self._classify_interaction(snapshot, browser_context)
                        self._emit_event(snapshot, browser_context, interaction_type)
                        self._last_snapshot = snapshot
                        self._last_browser_context = browser_context
                        self._last_activity_kind = interaction_type
                        self._last_activity_at = now
                    elif now - self._last_recorded_at >= self.heartbeat_interval:
                        if browser_context.is_typing or browser_context.is_scrolling:
                            activity_kind = "typing" if browser_context.is_typing else "scrolling"
                            if (
                                self._last_activity_kind != activity_kind
                                or now - self._last_activity_at >= 6.0
                            ):
                                self._emit_event(snapshot, browser_context, activity_kind)
                                self._last_snapshot = snapshot
                                self._last_browser_context = browser_context
                                self._last_activity_kind = activity_kind
                                self._last_activity_at = now
                                continue
                        self._emit_event(snapshot, browser_context, "heartbeat")
                        self._last_snapshot = snapshot
                        self._last_browser_context = browser_context
            except Exception:
                now = time.monotonic()
                if now - self._last_error_at > 30:
                    logger.exception("Window monitor loop failed")
                    self._last_error_at = now
            time.sleep(self.poll_interval)
