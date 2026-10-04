"""The macOS app a backend controls, addressed by process ID.

All input goes to this app in the background (see ``macos_background``): the
user's pointer, front app and key window are left alone, so they can keep
working while a subtask runs.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from functools import cached_property
from typing import Any

from ..errors import TargetUnavailable, UnsupportedDesktopAction
from ..models import Bounds
from . import macos_background as background
from .macos_ocr import MacOSWindow, on_screen_windows
from .macos_parking import WindowParking, wait_until

logger = logging.getLogger(__name__)

# Characters macOS menus show for arc-cua key names, for running shortcuts through the menu.
_MENU_CHARACTERS = {
    "MINUS": "-", "EQUAL": "=", "LEFT_BRACKET": "[", "RIGHT_BRACKET": "]", "BACKSLASH": "\\",
    "SEMICOLON": ";", "QUOTE": "'", "COMMA": ",", "PERIOD": ".", "SLASH": "/", "GRAVE": "`",
}

_CHROMIUM_FRAMEWORKS = ("Chromium Embedded Framework.framework", "Electron Framework.framework")

# How long after an input the app counts as having activated itself because of it.
_ACTIVATION_WINDOW_S = 1.5


class MacOSApp:
    """One running app. Fails with ``TargetUnavailable`` once it quits or has no usable window."""

    def __init__(self, pid: int) -> None:
        if type(pid) is not int or pid <= 0:
            raise ValueError("pid must be a positive integer process ID")
        import AppKit  # type: ignore
        import ApplicationServices as AS  # type: ignore

        running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is None or not _alive(pid):
            raise TargetUnavailable(f"No app is running with process ID {pid}.")
        self.pid = pid
        self.name = str(running.localizedName() or f"process {pid}")
        bundle = running.bundleURL()
        self.bundle_path = str(bundle.path()) if bundle is not None else None
        self.ax = AS.AXUIElementCreateApplication(pid)
        self._parking = WindowParking(pid, self.name, frame=self._frame)
        self._user_pid: int | None = None
        self._last_input = 0.0

    @classmethod
    def from_bundle_id(cls, bundle_id: str) -> MacOSApp:
        """The running app with this bundle identifier; the first launched when there are several."""
        import AppKit  # type: ignore

        running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id) or []
        if not running:
            raise TargetUnavailable(f"No app with bundle ID {bundle_id} is running.")
        return cls(int(running[0].processIdentifier()))

    def __repr__(self) -> str:
        return f"MacOSApp(pid={self.pid}, name={self.name!r})"

    # ---- lifecycle ---------------------------------------------------------------

    def check_running(self) -> None:
        if not _alive(self.pid):
            raise TargetUnavailable(f"{self.name} (process ID {self.pid}) quit.")

    def open(self, *, wait_s: float = 2.0, park: bool = True, window_id: int | None = None) -> None:
        """Make sure there is a window to work in.

        Waits briefly for a window that is still opening, then brings a minimized
        window, or a hidden app's windows, onto an invisible display. ``close()``
        puts them back. With ``park=False`` the windows stay out of sight; call
        ``open()`` again when input events or pixels need a window on a display.
        ``window_id`` names the minimized window to bring, when there are several.
        """
        background.ensure_available()
        self.check_running()
        if self._parking.active:
            if window_id is None or self.on_display(window_id):
                return
            self._parking.restore()  # Another window is parked; bring this one instead.
        if not wait_until(lambda: bool(self.windows()) or self._parking.needed(), wait_s):
            raise TargetUnavailable(self._no_window_reason())
        if park and (not self.windows() if window_id is None else not self.on_display(window_id)):
            self._parking.park(window_id)

    @cached_property
    def embeds_chromium(self) -> bool:
        """Whether the app is built on Chromium (Chromium Embedded Framework or
        Electron), whose page content is in the accessibility tree only when the
        app turns it on."""
        if self.bundle_path is None:
            return False
        frameworks = os.path.join(self.bundle_path, "Contents", "Frameworks")
        return any(os.path.isdir(os.path.join(frameworks, name)) for name in _CHROMIUM_FRAMEWORKS)

    @property
    def parked(self) -> bool:
        """True while windows are on the invisible display, until ``close()``."""
        return self._parking.active

    def out_of_sight(self) -> bool:
        """True when the app has no window on screen but a minimized one, or is hidden."""
        return not self.windows() and self._parking.needed()

    def close(self) -> None:
        """Undo ``open()``: minimize or hide parked windows again and move them back."""
        if self._parking.active:
            try:
                self._parking.restore()
            except TargetUnavailable:
                pass

    # ---- windows -----------------------------------------------------------------

    def windows(self) -> list[MacOSWindow]:
        """The app's normal windows on screen, front to back."""
        return on_screen_windows(self.pid)

    def require_window(self) -> MacOSWindow:
        """The app's frontmost usable window."""
        windows = self.windows()
        if not windows:
            self.check_running()
            raise TargetUnavailable(self._no_window_reason())
        return windows[0]

    def all_windows(self) -> list[dict[str, Any]]:
        """Every window of the app: those on a display front to back, then minimized ones
        and a hidden app's: window_id, title, bounds, on_screen, minimized."""
        found: list[dict[str, Any]] = [
            {"window_id": w.window_id, "title": w.title, "bounds": w.bounds, "on_screen": True, "minimized": False}
            for w in self.windows()
        ]
        seen = {entry["window_id"] for entry in found}
        for ax in _attr(self.ax, "AXWindows") or ():
            if ax is None:
                continue
            identifier = identify_ax_window(ax, self.pid)
            if identifier is None or identifier in seen:
                continue
            seen.add(identifier)
            info = window_server_info(identifier)
            found.append({
                "window_id": identifier,
                "title": str(_attr(ax, "AXTitle") or (info or {}).get("title") or ""),
                "bounds": _ax_frame(ax) or (info or {}).get("bounds"),
                "on_screen": False,
                "minimized": _attr(ax, "AXMinimized") is True,
            })
        return found

    def exists(self, window_id: int) -> bool:
        """Whether the window still exists (open, minimized or hidden), not closed."""
        return window_server_info(window_id) is not None

    def on_display(self, window_id: int) -> bool:
        """Whether one of the app's windows is on a display (the user's, or the invisible one)."""
        return any(window.window_id == window_id for window in self.windows())

    def window(self, window_id: int) -> MacOSWindow:
        """One of the app's windows on a display. Raises TargetUnavailable when it is gone."""
        for window in self.windows():
            if window.window_id == window_id:
                return window
        self.check_running()
        if window_server_info(window_id) is None:
            raise TargetUnavailable(
                f"{self.name}'s window {window_id} is gone (closed, or replaced by a new one); "
                "find the app's window again."
            )
        raise TargetUnavailable(f"{self.name}'s window {window_id} is not on a display")

    def ax_window(self, window_id: int) -> Any:
        """The accessibility element of one of the app's windows, or None."""
        return find_ax_window(self.ax, window_id)

    def attached_windows(self, window_id: int) -> list[MacOSWindow]:
        """Sheets and drawers attached to a window that are on a display, front to back.
        They are separate windows to the window server."""
        ax = self.ax_window(window_id)
        if ax is None:
            return []
        attached = {
            background.window_id(child)
            for child in (_attr(ax, "AXChildren") or ())
            if _attr(child, "AXRole") in ("AXSheet", "AXDrawer")
        }
        attached.discard(None)
        return [window for window in self.windows() if window.window_id in attached]

    def _input_window(self, window_id: int | None, point: tuple[float, float] | None = None) -> MacOSWindow:
        """The window to address input to. With ``window_id``, that window or a sheet
        attached to it (the one under ``point``, else the frontmost sheet, since a
        sheet takes the window's input); without, as before: the window under the
        point, or the key window."""
        if window_id is None:
            if point is None:
                return self.key_window()
            window = self.window_at(point)
            if window is None:
                self.check_running()
                raise UnsupportedDesktopAction(f"The target is outside {self.name}'s visible windows")
            return window
        target = self.window(window_id)
        sheets = self.attached_windows(window_id)
        if point is None:
            return sheets[0] if sheets else target
        for window in (*sheets, target):
            if _contains(window.bounds, point):
                return window
        raise UnsupportedDesktopAction(f"The point is outside {self.name}'s window {window_id}")

    def window_at(self, point: tuple[float, float]) -> MacOSWindow | None:
        x, y = point
        for window in self.windows():
            bounds = window.bounds
            if bounds.x <= x <= bounds.x + bounds.width and bounds.y <= y <= bounds.y + bounds.height:
                return window
        return None

    def key_window(self) -> MacOSWindow:
        """The window keys go to: the app's focused window when it is on screen, else its frontmost."""
        windows = self.windows()
        if not windows:
            return self.require_window()
        focused = background.window_id(_attr(self.ax, "AXFocusedWindow"))
        return next((window for window in windows if window.window_id == focused), windows[0])

    def _frame(self, window_id: int) -> tuple[float, float, float, float] | None:
        """On-screen frame of one of the app's windows."""
        for window in self.windows():
            if window.window_id == window_id:
                bounds = window.bounds
                return (bounds.x, bounds.y, bounds.width, bounds.height)
        return None

    def _no_window_reason(self) -> str:
        if self._parking.needed():
            return (
                f"{self.name} has no window on screen (it is hidden or its windows are minimized), "
                "and accessibility lists none of its windows."
            )
        return f"{self.name} has no open window on this desktop."

    # ---- input -------------------------------------------------------------------

    def click(
        self, bounds: Bounds, *, count: int = 1, right: bool = False, flags: int = 0, window_id: int | None = None,
    ) -> None:
        point = bounds.center
        window = self._input_window(window_id, point)
        with self.input_scope():
            background.click(self.pid, window.window_id, point, count=count, right=right, flags=flags)

    def drag(self, source: Bounds, destination: Bounds, *, window_id: int | None = None) -> None:
        self.drag_path([source.center, destination.center], window_id=window_id)

    def drag_path(self, points: list[tuple[float, float]], *, window_id: int | None = None) -> None:
        """Drag through screen points; the window is the one under the first point."""
        window = self._input_window(window_id, points[0])
        with self.input_scope():
            background.drag_path(self.pid, window.window_id, points)

    def scroll(self, direction: str, *, amount: int = 450, window_id: int | None = None) -> None:
        deltas = {"UP": (0, amount), "DOWN": (0, -amount), "LEFT": (amount, 0), "RIGHT": (-amount, 0)}
        if direction not in deltas:
            raise UnsupportedDesktopAction(f"Unknown scroll direction: {direction}")
        window = self._input_window(window_id)
        dx, dy = deltas[direction]
        with self.input_scope():
            background.scroll(self.pid, window.window_id, window.bounds.center, dx=dx, dy=dy)

    def scroll_at(self, point: tuple[float, float], *, dx: int, dy: int, window_id: int | None = None) -> None:
        """Scroll the content under a screen point: by moving the scroll bars of the
        scroll area there, through accessibility, else with scroll events."""
        if scroll_area_by_bars(self.ax, point, dx=dx, dy=dy):
            return
        window = self._input_window(window_id, point)
        with self.input_scope():
            background.scroll(self.pid, window.window_id, point, dx=dx, dy=dy)

    def press(self, code: int, flags: int = 0, *, window_id: int | None = None) -> None:
        window = self._input_window(window_id)
        with self.input_scope():
            background.press(self.pid, window.window_id, code, flags)

    def shortcut(
        self, modifiers: tuple[str, ...], key: str, code: int, flags: int, *, window_id: int | None = None,
    ) -> None:
        """Press a chord. Command chords go through the app's menu when an item has them,
        since menu key equivalents only reach the front app."""
        if "MOD" in modifiers:
            if modifiers == ("MOD",) and key == "A" and self.select_all():
                logger.debug("background shortcut via=select_all")
                return
            character = _MENU_CHARACTERS.get(key, key.lower() if len(key) == 1 else None)
            if character is not None:
                with self.input_scope():
                    if background.menu_shortcut(self.ax, character, modifiers):
                        logger.debug("background shortcut via=menu")
                        return
        self.press(code, flags, window_id=window_id)

    def type_text(self, text: str, *, window_id: int | None = None) -> None:
        window = self._input_window(window_id)
        with self.input_scope():
            background.type_text(self.pid, window.window_id, text, check=self.check_running)

    def select_all(self) -> bool:
        """Select all text in the focused field through accessibility."""
        field = _attr(self.ax, "AXFocusedUIElement")
        return field is not None and background.select_all(field)

    # ---- keeping the user's app in front -----------------------------------------

    def input_scope(self) -> _InputScope:
        """Scope for one input: records the user's front app, and hands the front back if this app takes it."""
        return _InputScope(self)

    def keep_behind(self) -> None:
        """Hand the front back to the user's app if this app activated itself after an input.

        Some controls activate their app when used, even from the background.
        Switching to the app later is the user's choice and is left alone.
        """
        user = self._user_pid
        if user is None or time.monotonic() - self._last_input > _ACTIVATION_WINDOW_S:
            return
        if background.front_pid() != self.pid:
            return
        import AppKit  # type: ignore

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(user)
        if app is not None:
            logger.info("%s took the front after input; restoring the user's app", self.name)
            app.activateWithOptions_(0)


class _InputScope:
    def __init__(self, app: MacOSApp) -> None:
        self.app = app

    def __enter__(self) -> None:
        self.app.check_running()
        front = background.front_pid()
        if front is not None and front != self.app.pid:
            self.app._user_pid = front
        self.app._last_input = time.monotonic()

    def __exit__(self, *exc: Any) -> None:
        self.app._last_input = time.monotonic()
        self.app.keep_behind()


def scroll_area_by_bars(app_ax: Any, point: tuple[float, float], *, dx: float, dy: float) -> bool:
    """Scroll the scroll area under ``point`` by ``dx``/``dy`` points (positive ``dy``
    shows what is above) by setting its scroll bars. False when there is none to set."""
    import ApplicationServices as AS  # type: ignore

    error, element = AS.AXUIElementCopyElementAtPosition(app_ax, float(point[0]), float(point[1]), None)
    while error == 0 and element is not None and _attr(element, "AXRole") != "AXScrollArea":
        element = _attr(element, "AXParent")
    if element is None or error != 0:
        return False
    frame = _ax_frame(element)
    content = [_ax_frame(child) for child in (_attr(element, "AXChildren") or ())]
    content = [c for c in content if c is not None]
    if frame is None or not content:
        return False
    moved = False
    for delta, name, viewport, extent in (
        (dy, "AXVerticalScrollBar", frame.height, max(c.height for c in content)),
        (dx, "AXHorizontalScrollBar", frame.width, max(c.width for c in content)),
    ):
        bar = _attr(element, name)
        position = _attr(bar, "AXValue") if bar is not None else None
        if not delta or bar is None or position is None or extent <= viewport:
            continue
        # Scroll-bar values run 0..1 over the scrollable distance; positive deltas move up/left.
        target = min(1.0, max(0.0, float(position) - delta / (extent - viewport)))
        moved = AS.AXUIElementSetAttributeValue(bar, "AXValue", target) == 0 or moved
    return moved


def _contains(bounds: Bounds, point: tuple[float, float]) -> bool:
    x, y = point
    return bounds.x <= x <= bounds.x + bounds.width and bounds.y <= y <= bounds.y + bounds.height


def window_server_info(window_id: int) -> dict[str, Any] | None:
    """What the window server knows about a window, on screen or not: pid, title,
    bounds and whether it is on screen. None when the window no longer exists."""
    import Quartz  # type: ignore

    # Describes a window by id whether it is on screen, minimized or hidden. A window
    # counts as gone only when the full window list lacks it too.
    infos = Quartz.CGWindowListCreateDescriptionFromArray([window_id]) or []
    if not any(int(info.get(Quartz.kCGWindowNumber, 0)) == window_id for info in infos):
        infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    for info in infos:
        if int(info.get(Quartz.kCGWindowNumber, 0)) != window_id:
            continue
        rect = info.get(Quartz.kCGWindowBounds) or {}
        return {
            "pid": int(info.get(Quartz.kCGWindowOwnerPID, 0)),
            "title": str(info.get(Quartz.kCGWindowName) or ""),
            "bounds": Bounds(
                float(rect.get("X", 0)), float(rect.get("Y", 0)),
                float(rect.get("Width", 0)), float(rect.get("Height", 0)),
            ),
            "on_screen": bool(info.get(Quartz.kCGWindowIsOnscreen, False)),
        }
    return None


def find_ax_window(app_ax: Any, window_id: int, *, info: Callable[[int], dict[str, Any] | None] | None = None) -> Any:
    """The app's AX window for a window-server window id, or None.

    Matched by the id accessibility reports for each window. When a window reports
    none, it is matched by what the window server knows about the id instead: the
    same frame, and the same title when both have one."""
    windows = [w for w in (_attr(app_ax, "AXWindows") or ()) if w is not None]
    unidentified = []
    for window in windows:
        identifier = background.window_id(window)
        if identifier == window_id:
            return window
        if identifier is None:
            unidentified.append(window)
    if not unidentified:
        return None
    known = (info or window_server_info)(window_id)
    if known is None:
        return None
    for window in unidentified:
        frame = _ax_frame(window)
        title = str(_attr(window, "AXTitle") or "")
        if frame is not None and _same_frame(frame, known["bounds"]) and (
            not title or not known["title"] or title == known["title"]
        ):
            return window
    return None


def identify_ax_window(ax_window: Any, pid: int, shown: list[MacOSWindow] | None = None) -> int | None:
    """The window-server id of an AX window: the id accessibility reports, else the
    app's window with the same frame (and title, when both have one). ``shown`` limits
    the match to those windows; otherwise all of the app's windows, on screen or not."""
    identifier = background.window_id(ax_window)
    if identifier is not None:
        return identifier
    frame = _ax_frame(ax_window)
    if frame is None:
        return None
    title = str(_attr(ax_window, "AXTitle") or "")
    candidates = [(w.window_id, w.bounds, w.title) for w in shown] if shown is not None else _app_windows(pid)
    for candidate, bounds, candidate_title in candidates:
        if _same_frame(frame, bounds) and (not title or not candidate_title or title == candidate_title):
            return candidate
    return None


def _app_windows(pid: int) -> list[tuple[int, Bounds, str]]:
    """All of an app's normal windows the window server knows, on screen or not."""
    import Quartz  # type: ignore

    found = []
    for info in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []:
        if int(info.get(Quartz.kCGWindowOwnerPID, -1)) != pid or int(info.get(Quartz.kCGWindowLayer, 0) or 0) != 0:
            continue
        rect = info.get(Quartz.kCGWindowBounds) or {}
        found.append((
            int(info.get(Quartz.kCGWindowNumber, 0)),
            Bounds(float(rect.get("X", 0)), float(rect.get("Y", 0)), float(rect.get("Width", 0)),
                   float(rect.get("Height", 0))),
            str(info.get(Quartz.kCGWindowName) or ""),
        ))
    return found

def _same_frame(a: Bounds, b: Bounds) -> bool:
    return all(abs(x - y) <= 1 for x, y in ((a.x, b.x), (a.y, b.y), (a.width, b.width), (a.height, b.height)))


def _ax_frame(element: Any) -> Bounds | None:
    import ApplicationServices as AS  # type: ignore

    position, size = _attr(element, "AXPosition"), _attr(element, "AXSize")
    if position is None or size is None:
        return None
    ok_position, point = AS.AXValueGetValue(position, AS.kAXValueCGPointType, None)
    ok_size, dimensions = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not ok_position or not ok_size:
        return None
    return Bounds(float(point.x), float(point.y), float(dimensions.width), float(dimensions.height))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _attr(element: Any, name: str) -> Any:
    import ApplicationServices as AS  # type: ignore

    return background.copy_attribute(AS, element, name)[1]
