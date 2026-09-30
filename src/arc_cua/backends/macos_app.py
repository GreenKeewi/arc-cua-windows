"""The macOS app a backend controls, addressed by process ID.

All input goes to this app in the background (see ``macos_background``): the
user's pointer, front app and key window are left alone, so they can keep
working while a subtask runs.
"""

from __future__ import annotations

import logging
import os
import time
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

    def open(self, *, wait_s: float = 2.0) -> None:
        """Make sure there is a window to work in.

        Waits briefly for a window that is still opening, then brings a minimized
        window, or a hidden app's windows, onto an invisible display. ``close()``
        puts them back.
        """
        background.ensure_available()
        self.check_running()
        if self._parking.active:
            return
        if not wait_until(lambda: bool(self.windows()) or self._parking.needed(), wait_s):
            raise TargetUnavailable(self._no_window_reason())
        if not self.windows():
            self._parking.park()

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
                f"{self.name} has no window on screen: it is hidden or its window is minimized. "
                "Open the backend first (`with backend:`) to use such windows out of sight."
            )
        return f"{self.name} has no open window on this desktop."

    # ---- input -------------------------------------------------------------------

    def click(self, bounds: Bounds, *, count: int = 1, right: bool = False, flags: int = 0) -> None:
        point = bounds.center
        window = self.window_at(point)
        if window is None:
            self.check_running()
            raise UnsupportedDesktopAction(f"The target is outside {self.name}'s visible windows")
        with self.input_scope():
            background.click(self.pid, window.window_id, point, count=count, right=right, flags=flags)

    def drag(self, source: Bounds, destination: Bounds) -> None:
        window = self.window_at(source.center)
        if window is None:
            self.check_running()
            raise UnsupportedDesktopAction(f"The drag source is outside {self.name}'s visible windows")
        with self.input_scope():
            background.drag(self.pid, window.window_id, source.center, destination.center)

    def scroll(self, direction: str, *, amount: int = 450) -> None:
        deltas = {"UP": (0, amount), "DOWN": (0, -amount), "LEFT": (amount, 0), "RIGHT": (-amount, 0)}
        if direction not in deltas:
            raise UnsupportedDesktopAction(f"Unknown scroll direction: {direction}")
        window = self.key_window()
        dx, dy = deltas[direction]
        with self.input_scope():
            background.scroll(self.pid, window.window_id, window.bounds.center, dx=dx, dy=dy)

    def press(self, code: int, flags: int = 0) -> None:
        window = self.key_window()
        with self.input_scope():
            background.press(self.pid, window.window_id, code, flags)

    def shortcut(self, modifiers: tuple[str, ...], key: str, code: int, flags: int) -> None:
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
        self.press(code, flags)

    def type_text(self, text: str) -> None:
        window = self.key_window()
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

    try:
        error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if error == 0 else None
