"""Give background control a window when the target app has none on screen.

A minimized window, or the windows of a hidden app, are moved onto an invisible
display where the app treats them as visible: they render, expose their
accessibility tree and take input, while the user never sees them. ``restore``
puts everything back as it was. Windows move only while out of sight (minimized
or hidden), so the user never sees them travel.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import TargetUnavailable

logger = logging.getLogger(__name__)

_CLASSES = ("CGVirtualDisplayDescriptor", "CGVirtualDisplay", "CGVirtualDisplaySettings", "CGVirtualDisplayMode")


class VirtualDisplay:
    """An invisible display, the mechanism behind Screen Sharing's headless sessions.

    Private API, resolved at runtime. The display disappears when ``close()`` is
    called or this object is released.
    """

    def __init__(self, name: str = "arc-cua", width: int = 1920, height: int = 1080) -> None:
        import AppKit  # type: ignore
        import objc  # type: ignore

        descriptor_class, display_class, settings_class, mode_class = (objc.lookUpClass(name) for name in _CLASSES)
        descriptor = descriptor_class.alloc().init()
        descriptor.setValue_forKey_(name, "name")
        descriptor.setValue_forKey_(width, "maxPixelsWide")
        descriptor.setValue_forKey_(height, "maxPixelsHigh")
        descriptor.setValue_forKey_(AppKit.NSValue.valueWithSize_((width * 0.26, height * 0.26)), "sizeInMillimeters")
        descriptor.setValue_forKey_(0x7448, "productID")
        descriptor.setValue_forKey_(0x7448, "vendorID")
        descriptor.setValue_forKey_(1, "serialNum")
        try:
            import libdispatch  # type: ignore

            descriptor.setValue_forKey_(libdispatch.dispatch_get_main_queue(), "queue")
        except ImportError:
            pass
        display = display_class.alloc().initWithDescriptor_(descriptor)
        mode = mode_class.alloc().initWithWidth_height_refreshRate_(width, height, 60.0)
        if display is None or mode is None:
            raise RuntimeError("Could not create a virtual display")
        settings = settings_class.alloc().init()
        settings.setValue_forKey_(0, "hiDPI")
        settings.setValue_forKey_([mode], "modes")
        if not display.applySettings_(settings) or not display.displayID():
            raise RuntimeError("Could not configure a virtual display")
        self._display: Any = display
        self.id = int(display.displayID())

    @staticmethod
    def supported() -> bool:
        try:
            import objc  # type: ignore

            return all(objc.lookUpClass(name) for name in _CLASSES)
        except Exception:
            return False

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        import Quartz  # type: ignore

        rect = Quartz.CGDisplayBounds(self.id)
        return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height)

    def close(self) -> None:
        self._display = None


@dataclass(frozen=True, slots=True)
class _Parked:
    window: Any
    id: int
    position: tuple[float, float]
    parked_at: tuple[float, float]


def wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    """Poll while running the main run loop, which delivers display and window updates to this process."""
    from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode  # type: ignore

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
    return condition()


class WindowParking:
    """Moves a hidden app's windows, or one minimized window, onto a virtual display and back."""

    def __init__(
        self,
        pid: int,
        app_name: str,
        *,
        frame: Callable[[int], tuple[float, float, float, float] | None],
    ) -> None:
        import ApplicationServices as AS  # type: ignore

        self.pid = pid
        self.app_name = app_name
        self._frame = frame
        self._app = AS.AXUIElementCreateApplication(pid)
        self._display: VirtualDisplay | None = None
        self._parked: list[_Parked] = []
        self._unminimized: Any = None
        self._unhid = False

    @property
    def active(self) -> bool:
        return bool(self._parked)

    @property
    def unminimized(self) -> Any:
        """The minimized window brought back for background control, if any."""
        return self._unminimized

    def needed(self) -> bool:
        """True when the app is hidden, or none of its windows is on screen but one is minimized."""
        if self.hidden():
            return True
        return any(_bool(window, "AXMinimized") for window in self.windows())

    def windows(self) -> list[Any]:
        return list(_attr(self._app, "AXWindows") or [])

    def hidden(self) -> bool:
        return _bool(self._app, "AXHidden")

    def park(self, wanted: int | None = None) -> None:
        """Park the windows background control will use; ``wanted`` is the window id to
        bring when it is minimized. Raises TargetUnavailable when that is not possible."""
        from .macos_background import window_id

        windows = self.windows()
        hidden = self.hidden()
        # A hidden app shows all its open windows when unhidden, so all of them move;
        # otherwise one minimized window is brought back: the wanted one, else the first.
        open_windows = [window for window in windows if not _bool(window, "AXMinimized")] if hidden else []
        minimized_windows = [w for w in windows if _bool(w, "AXMinimized")]
        if wanted is not None:
            minimized_windows.sort(key=lambda w: window_id(w) != wanted)
        minimized = None if open_windows else next(iter(minimized_windows), None)
        moving = open_windows or ([minimized] if minimized is not None else [])
        if not moving:
            raise TargetUnavailable(
                f"{self.app_name} has no window arc-cua can reach. Its windows may be closed or on another desktop."
            )
        if not VirtualDisplay.supported():
            raise TargetUnavailable(
                f"{self.app_name}'s window is minimized or hidden, and this Mac can't create a display to use it "
                "in the background."
            )
        display = VirtualDisplay()
        self._display = display
        if not wait_until(lambda: display.bounds[2] > 0, 3):
            self._release_display()
            raise TargetUnavailable("The background display didn't become ready.")
        x, y, _, _ = display.bounds
        for index, window in enumerate(moving):
            position = _position(window)
            identifier = window_id(window)
            if identifier is None or position is None:
                continue
            target = (x + 40 + index * 30, y + 40 + index * 30)
            _move(window, target)
            self._parked.append(_Parked(window, identifier, position, target))
        if not self._parked:
            self._release_display()
            raise TargetUnavailable(f"{self.app_name}'s window couldn't be moved for background control.")
        if hidden:
            _running_app(self.pid).unhide()
            self._unhid = True
        if minimized is not None:
            _set(minimized, "AXMinimized", False)
            self._unminimized = minimized
        # Unhiding can place windows back near the main screen; move them again until all are out of sight.
        bounds = display.bounds

        def landed_all() -> bool:
            pending = [entry for entry in self._parked if not _on_display(self._frame(entry.id), bounds)]
            for entry in pending:
                _move(entry.window, entry.parked_at)
            return not pending

        landed = wait_until(landed_all, 3)
        logger.info("parked windows=%d hidden=%s landed=%s", len(self._parked), hidden, landed)
        if not landed:
            self.restore()
            raise TargetUnavailable(f"{self.app_name}'s window didn't open for background control.")

    def restore(self) -> None:
        """Minimize or hide again, then move the windows back while they're out of sight."""
        if not self._parked:
            self._release_display()
            return
        if (window := self._unminimized) is not None:
            _set(window, "AXMinimized", True)
            wait_until(lambda: _bool(window, "AXMinimized"), 2)
        if self._unhid:
            _running_app(self.pid).hide()
            wait_until(self.hidden, 2)
        for entry in self._parked:
            _move(entry.window, entry.position)
        logger.info("restored parked windows=%d", len(self._parked))
        self._parked = []
        self._unminimized = None
        self._unhid = False
        self._release_display()

    def _release_display(self) -> None:
        if self._display is not None:
            self._display.close()
            self._display = None


def _on_display(frame: tuple[float, float, float, float] | None, display: tuple[float, float, float, float]) -> bool:
    """True when the window's top-left corner is on the display, so it doesn't reach onto the user's screens."""
    if frame is None:
        return False
    x, y, _, _ = frame
    dx, dy, width, height = display
    return dx <= x < dx + width and dy <= y < dy + height


def _running_app(pid: int) -> Any:
    import AppKit  # type: ignore

    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        raise TargetUnavailable(f"The app with process ID {pid} is no longer running.")
    return app


def _attr(element: Any, name: str) -> Any:
    import ApplicationServices as AS  # type: ignore

    from .macos_background import copy_attribute

    return copy_attribute(AS, element, name)[1]


def _bool(element: Any, name: str) -> bool:
    return _attr(element, name) is True


def _set(element: Any, name: str, value: Any) -> bool:
    import ApplicationServices as AS  # type: ignore

    return AS.AXUIElementSetAttributeValue(element, name, value) == 0


def _position(window: Any) -> tuple[float, float] | None:
    import ApplicationServices as AS  # type: ignore

    value = _attr(window, "AXPosition")
    if value is None:
        return None
    ok, point = AS.AXValueGetValue(value, AS.kAXValueCGPointType, None)
    return (float(point.x), float(point.y)) if ok else None


def _move(window: Any, point: tuple[float, float]) -> bool:
    import ApplicationServices as AS  # type: ignore

    value = AS.AXValueCreate(AS.kAXValueCGPointType, point)
    return value is not None and _set(window, "AXPosition", value)
