"""Input addressed to one window of one process, without the user noticing.

Events are posted to the target process through private WindowServer (SkyLight)
calls, so the user's pointer, front app and key window are left alone. Key focus
is borrowed only for the milliseconds an event batch takes and then handed back.
Every symbol is resolved at runtime; when one is missing, input fails closed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..errors import UnsupportedDesktopAction

logger = logging.getLogger(__name__)

_LIBRARIES = (
    "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
)

# (name, restype, argtypes)
_SIGNATURES: dict[str, tuple[Any, list[Any]]] = {
    "SLEventPostToPid": (None, [ctypes.c_int32, ctypes.c_void_p]),
    "SLEventSetIntegerValueField": (None, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]),
    "CGEventSetWindowLocation": (None, [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]),
    "SLPSPostEventRecordTo": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p]),
    "_SLPSGetFrontProcess": (ctypes.c_int32, [ctypes.c_void_p]),
    "GetProcessForPID": (ctypes.c_int32, [ctypes.c_int32, ctypes.c_void_p]),
    "GetProcessPID": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]),
    "CGSMainConnectionID": (ctypes.c_uint32, []),
    "SLSGetWindowOwner": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]),
    "SLSGetConnectionPSN": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_void_p]),
    "SLEventSetAuthenticationMessage": (None, [ctypes.c_void_p, ctypes.c_void_p]),
    "_AXUIElementGetWindow": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
}

# Symbols background input cannot work without.
REQUIRED = (
    "SLEventPostToPid",
    "SLEventSetIntegerValueField",
    "CGEventSetWindowLocation",
    "SLPSPostEventRecordTo",
    "_SLPSGetFrontProcess",
    "GetProcessForPID",
)

# CGEvent integer fields set on window-addressed mouse events.
_FIELD_PHASE = 0
_FIELD_CLICK_STATE = 1
_FIELD_BUTTON = 3
_FIELD_SUBTYPE = 7
_FIELD_TARGET_PID = 40
_FIELD_WINDOW_IDS = (51, 91, 92)
_FIELD_GROUP = 58

_PSN = ctypes.c_uint8 * 8


class BackgroundInputUnavailable(RuntimeError):
    """This macOS lacks a call background input needs."""

    code = "background_unavailable"


class _SkyLight:
    def __init__(self) -> None:
        handles = []
        for path in _LIBRARIES:
            try:
                handles.append(ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL))
            except OSError:
                continue
        self.functions: dict[str, Any] = {}
        for name, (restype, argtypes) in _SIGNATURES.items():
            for handle in handles:
                try:
                    function = getattr(handle, name)
                except AttributeError:
                    continue
                function.restype = restype
                function.argtypes = argtypes
                self.functions[name] = function
                break
        self._auth = _authentication_factory()

    @property
    def missing(self) -> list[str]:
        return [name for name in REQUIRED if name not in self.functions]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["functions"][name]
        except KeyError:
            raise AttributeError(name) from None

    def attach_authentication(self, event_pointer: int, pid: int) -> None:
        """Chromium-family apps accept background keys only with this message."""
        setter = self.functions.get("SLEventSetAuthenticationMessage")
        if self._auth is None or setter is None:
            return
        # __CGEvent is {CFRuntimeBase, uint32_t, SLSEventRecord *}.
        for offset in (24, 32, 16):
            record = ctypes.c_void_p.from_address(event_pointer + offset).value
            if not record:
                continue
            message = self._auth(record, pid)
            if message:
                setter(event_pointer, message)
            return


def _authentication_factory() -> Callable[[int, int], int | None] | None:
    path = ctypes.util.find_library("objc")
    if path is None:
        return None
    runtime = ctypes.CDLL(path)
    runtime.objc_getClass.restype = ctypes.c_void_p
    runtime.objc_getClass.argtypes = [ctypes.c_char_p]
    runtime.sel_registerName.restype = ctypes.c_void_p
    runtime.sel_registerName.argtypes = [ctypes.c_char_p]
    runtime.class_getClassMethod.restype = ctypes.c_void_p
    runtime.class_getClassMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cls = runtime.objc_getClass(b"SLSEventAuthenticationMessage")
    selector = runtime.sel_registerName(b"messageWithEventRecord:pid:version:")
    if not cls or not runtime.class_getClassMethod(cls, selector):
        return None
    send = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32,
    )(ctypes.cast(runtime.objc_msgSend, ctypes.c_void_p).value)
    return lambda record, pid: send(cls, selector, record, pid, 0)


_skylight: _SkyLight | None = None


def _sl() -> _SkyLight:
    global _skylight
    if _skylight is None:
        _skylight = _SkyLight()
    return _skylight


def ensure_available() -> None:
    missing = _sl().missing
    if missing:
        raise BackgroundInputUnavailable(
            f"Background input is unavailable on this macOS (missing {', '.join(missing)})"
        )


def _pointer(obj: Any) -> int:
    """Address of the CF object behind a PyObjC wrapper (CGEvent, AXUIElement)."""
    return obj.__c_void_p__().value


# kAXErrorNoValue and kAXErrorAttributeUnsupported: the element has no such value, which is no failure.
_AX_ABSENT = (-25212, -25205)
_AX_CANNOT_COMPLETE = -25204
# A refusal faster than this is a passing one; a slower one is the messaging timeout, not worth waiting out twice.
_AX_QUICK_S = 0.25


def copy_attribute(AS: Any, element: Any, name: str) -> tuple[int, Any]:
    """An accessibility attribute as (AX error, value); the value is None on an error.
    A request the app refuses at once is tried a second time, since apps refuse
    briefly while busy (launching, unhiding, changing windows)."""
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
        except Exception:
            return -25200, None  # kAXErrorFailure
        if error == 0:
            return 0, value
        if error in _AX_ABSENT:
            return error, None
        logger.debug("accessibility read failed attribute=%s error=%d attempt=%d", name, error, attempt)
        if error != _AX_CANNOT_COMPLETE or time.monotonic() - started > _AX_QUICK_S:
            break
        time.sleep(0.05)
    return error, None


def window_id(ax_window: Any) -> int | None:
    """CGWindowID of an AX window element, or None."""
    function = _sl().functions.get("_AXUIElementGetWindow")
    if function is None or ax_window is None:
        return None
    value = ctypes.c_uint32(0)
    try:
        error = function(_pointer(ax_window), ctypes.byref(value))
    except (AttributeError, ctypes.ArgumentError):
        return None
    return int(value.value) if error == 0 and value.value else None


# ---- focus without raise ----------------------------------------------------------


def front_process() -> bytes | None:
    psn = _PSN()
    return bytes(psn) if _sl()._SLPSGetFrontProcess(psn) == 0 else None


def process_pid(psn: bytes) -> int | None:
    function = _sl().functions.get("GetProcessPID")
    if function is None:
        return None
    pid = ctypes.c_int32(0)
    return int(pid.value) if function(_PSN.from_buffer_copy(psn), ctypes.byref(pid)) == 0 else None


def front_pid() -> int | None:
    """Process ID of the frontmost app, read from WindowServer rather than AppKit's cache."""
    psn = front_process()
    return process_pid(psn) if psn is not None else None


def _process_owning(window: int, pid: int) -> bytes | None:
    sl = _sl()
    psn = _PSN()
    connection, owner_of, psn_of = (
        sl.functions.get(name) for name in ("CGSMainConnectionID", "SLSGetWindowOwner", "SLSGetConnectionPSN")
    )
    if connection and owner_of and psn_of:
        owner = ctypes.c_uint32(0)
        if owner_of(connection(), window, ctypes.byref(owner)) == 0 and owner.value and psn_of(owner.value, psn) == 0:
            return bytes(psn)
    return bytes(psn) if sl.GetProcessForPID(pid, psn) == 0 else None


def focus_record(window: int, focused: bool) -> bytes:
    record = bytearray(0xF8)
    record[0x04] = 0xF8
    record[0x08] = 0x0D
    record[0x3C:0x40] = int(window).to_bytes(4, "little")
    record[0x8A] = 0x01 if focused else 0x02
    return bytes(record)


def _send(record: bytes, psn: bytes) -> bool:
    buffer = (ctypes.c_uint8 * len(record)).from_buffer_copy(record)
    return _sl().SLPSPostEventRecordTo(_PSN.from_buffer_copy(psn), buffer) == 0


@dataclass(frozen=True, slots=True)
class _Borrowed:
    user_psn: bytes
    user_window: int | None
    target_psn: bytes
    target_window: int


def _borrow_focus(pid: int, window: int, user_window: Callable[[int], int | None]) -> _Borrowed | None:
    user = front_process()
    target = _process_owning(window, pid)
    if user is None or target is None:
        return None
    user_pid = process_pid(user)
    previous = user_window(user_pid) if user_pid is not None and user != target else None
    if user != target:
        _send(focus_record(window, False), user)
    if not _send(focus_record(window, True), target):
        return None
    return _Borrowed(user, previous, target, window)


def _restore(borrowed: _Borrowed) -> None:
    """Return key focus to the user's window; otherwise their app silently stops getting keys."""
    if borrowed.user_psn == borrowed.target_psn:
        return
    _send(focus_record(borrowed.target_window, False), borrowed.target_psn)
    if borrowed.user_window is not None:
        _send(focus_record(borrowed.user_window, True), borrowed.user_psn)


@contextmanager
def borrowed_focus(pid: int, window: int) -> Iterator[None]:
    """Make ``window`` key inside its app while the user's app stays frontmost and nothing is raised."""
    ensure_available()
    borrowed = _borrow_focus(pid, window, key_window)
    if borrowed is None:
        raise UnsupportedDesktopAction("The target window could not take background key focus")
    try:
        time.sleep(0.04)
        yield
    finally:
        _restore(borrowed)


def key_window(pid: int) -> int | None:
    """The key window of ``pid``: its AX focused window, else its frontmost normal window."""
    AS, Q = _ax(), _quartz()
    try:
        error, focused = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFocusedWindow", None)
    except Exception:
        error, focused = 1, None
    if error == 0 and (identifier := window_id(focused)) is not None:
        return identifier
    options = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
    for info in Q.CGWindowListCopyWindowInfo(options, Q.kCGNullWindowID) or []:
        if int(info.get(Q.kCGWindowOwnerPID, -1)) == pid and int(info.get(Q.kCGWindowLayer, 0) or 0) == 0:
            return int(info.get(Q.kCGWindowNumber, 0)) or None
    return None


# ---- events ------------------------------------------------------------------------


def _post(event: Any, pid: int, *, authenticate: bool = False) -> None:
    sl = _sl()
    pointer = _pointer(event)
    if authenticate:
        sl.attach_authentication(pointer, pid)
    sl.SLEventPostToPid(pid, pointer)


def _address_to_window(event: Any, window: int, point: tuple[float, float]) -> None:
    sl = _sl()
    pointer = _pointer(event)
    for field in _FIELD_WINDOW_IDS:
        sl.SLEventSetIntegerValueField(pointer, field, window)
    sl.CGEventSetWindowLocation(pointer, point[0], point[1])


def click(
    pid: int,
    window: int,
    point: tuple[float, float],
    *,
    count: int = 1,
    right: bool = False,
    flags: int = 0,
) -> None:
    """Click at a screen point inside ``window``.

    The event stream matches the one Chromium accepts from a trusted source: a
    stamped move, an off-screen primer click for its user-activation gate, then
    the target.
    """
    Q = _quartz()
    with borrowed_focus(pid, window):
        source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        group = time.monotonic_ns() & 0x7FFF_FFFF
        button = Q.kCGMouseButtonRight if right else Q.kCGMouseButtonLeft
        down = Q.kCGEventRightMouseDown if right else Q.kCGEventLeftMouseDown
        up = Q.kCGEventRightMouseUp if right else Q.kCGEventLeftMouseUp

        def emit(kind: int, location: tuple[float, float], *, phase: int, clicks: int, delay: float,
                 event_flags: int = 0) -> None:
            event = Q.CGEventCreateMouseEvent(source, kind, location, button)
            if event is None:
                raise UnsupportedDesktopAction("Cannot create mouse event")
            Q.CGEventSetFlags(event, event_flags)
            pointer = _pointer(event)
            sl = _sl()
            sl.SLEventSetIntegerValueField(pointer, _FIELD_PHASE, phase)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_CLICK_STATE, clicks)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_BUTTON, 1 if right else 0)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_SUBTYPE, 3)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_TARGET_PID, pid)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_GROUP, group)
            _address_to_window(event, window, location)
            _post(event, pid)
            if delay:
                time.sleep(delay)

        offscreen = (-1.0, -1.0)
        emit(Q.kCGEventMouseMoved, point, phase=2, clicks=0, delay=0.015)
        if not right:
            emit(Q.kCGEventLeftMouseDown, offscreen, phase=1, clicks=1, delay=0.001)
            emit(Q.kCGEventLeftMouseUp, offscreen, phase=2, clicks=1, delay=0.1)
        count = max(1, min(count, 3))
        for n in range(1, count + 1):
            emit(down, point, phase=3, clicks=n, delay=0.001, event_flags=flags)
            emit(up, point, phase=3, clicks=n, delay=0.08 if n < count else 0.03, event_flags=flags)


def drag(pid: int, window: int, start: tuple[float, float], end: tuple[float, float]) -> None:
    """Press at ``start``, move in steps to ``end`` and release, addressed to ``window``."""
    drag_path(pid, window, [start, end])


def drag_path(pid: int, window: int, points: list[tuple[float, float]], *, step_px: float = 12.0) -> None:
    """Press at the first point, move through the others and release at the last.

    Moves come at most ``step_px`` apart: apps and drag-and-drop only recognize a
    drag from a stream of drag events past a small threshold, not a jump."""
    if len(points) < 2:
        raise UnsupportedDesktopAction("A drag needs at least two points")
    start = points[0]
    end = points[-1]
    Q = _quartz()
    with borrowed_focus(pid, window):
        source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        group = time.monotonic_ns() & 0x7FFF_FFFF
        button = Q.kCGMouseButtonLeft

        def emit(kind: int, location: tuple[float, float], *, phase: int, clicks: int, delay: float) -> None:
            event = Q.CGEventCreateMouseEvent(source, kind, location, button)
            if event is None:
                raise UnsupportedDesktopAction("Cannot create mouse event")
            pointer = _pointer(event)
            sl = _sl()
            sl.SLEventSetIntegerValueField(pointer, _FIELD_PHASE, phase)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_CLICK_STATE, clicks)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_SUBTYPE, 3)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_TARGET_PID, pid)
            sl.SLEventSetIntegerValueField(pointer, _FIELD_GROUP, group)
            _address_to_window(event, window, location)
            _post(event, pid)
            time.sleep(delay)

        emit(Q.kCGEventMouseMoved, start, phase=2, clicks=0, delay=0.015)
        emit(Q.kCGEventLeftMouseDown, start, phase=3, clicks=1, delay=0.05)
        for previous, point in zip(points, points[1:]):
            distance = math.hypot(point[0] - previous[0], point[1] - previous[1])
            steps = max(1, math.ceil(distance / step_px))
            for step in range(1, steps + 1):
                t = step / steps
                at = (previous[0] + (point[0] - previous[0]) * t, previous[1] + (point[1] - previous[1]) * t)
                emit(Q.kCGEventLeftMouseDragged, at, phase=3, clicks=1, delay=0.008)
        emit(Q.kCGEventLeftMouseUp, end, phase=3, clicks=1, delay=0.03)


def scroll(pid: int, window: int, point: tuple[float, float], *, dx: int = 0, dy: int = 0) -> None:
    """Scroll by pixels at ``point``; positive ``dy`` scrolls up, positive ``dx`` left."""
    ensure_available()
    Q = _quartz()
    event = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 2, dy, dx)
    if event is None:
        raise UnsupportedDesktopAction("Cannot create scroll event")
    Q.CGEventSetLocation(event, point)
    _address_to_window(event, window, point)
    _post(event, pid)


def _post_key(pid: int, code: int, flags: int = 0, text: str | None = None) -> None:
    Q = _quartz()
    source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        event = Q.CGEventCreateKeyboardEvent(source, code, down)
        if event is None:
            raise UnsupportedDesktopAction("Cannot create key event")
        # Explicit flags, so a held modifier never turns typed text into shortcuts.
        Q.CGEventSetFlags(event, flags)
        if text is not None:
            Q.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
        _post(event, pid, authenticate=True)
        time.sleep(0.008)


def press(pid: int, window: int, code: int, flags: int = 0) -> None:
    with borrowed_focus(pid, window):
        _post_key(pid, code, flags)
        time.sleep(0.03)


def type_text(pid: int, window: int, text: str, *, check: Callable[[], None] | None = None) -> None:
    """Type in short bursts so key focus returns to the user's window between them."""
    characters = list(text)
    for start in range(0, len(characters), 16):
        if check is not None:
            check()
        with borrowed_focus(pid, window):
            for character in characters[start:start + 16]:
                _post_key(pid, 0, 0, character)
            time.sleep(0.02)


# ---- accessibility helpers ---------------------------------------------------------


def select_all(field: Any) -> bool:
    """Select all text in a field without Cmd+A, which AppKit only honours in the front app."""
    AS = _ax()
    try:
        error, value = AS.AXUIElementCopyAttributeValue(field, "AXValue", None)
    except Exception:
        return False
    text = value if error == 0 and isinstance(value, str) else ""
    selection = AS.AXValueCreate(AS.kAXValueCFRangeType, (0, len(text.encode("utf-16-le")) // 2))
    if selection is None:
        return False
    return AS.AXUIElementSetAttributeValue(field, "AXSelectedTextRange", selection) == 0


def menu_shortcut(app: Any, key: str, modifiers: tuple[str, ...]) -> bool:
    """Run a Command shortcut through the app's menu, since key equivalents only reach the front app.

    ``key`` is the character the menu shows (``"s"``, ``","``); ``modifiers`` are
    arc-cua names and must include ``MOD``. Returns False when no enabled menu item
    has that shortcut.
    """
    from .macos_ax import copy_attributes

    AS = _ax()
    if "MOD" not in modifiers:
        return False
    # AXMenuItemCmdModifiers: shift 1, option 2, control 4; command is implied.
    wanted = (1 if "SHIFT" in modifiers else 0) | (2 if "ALT" in modifiers else 0) | (4 if "CTRL" in modifiers else 0)
    try:
        error, bar = AS.AXUIElementCopyAttributeValue(app, "AXMenuBar", None)
    except Exception:
        return False
    if error != 0 or bar is None:
        return False
    names = ("AXMenuItemCmdChar", "AXMenuItemCmdModifiers", "AXEnabled", "AXChildren")

    def find(element: Any, depth: int) -> Any:
        attributes = copy_attributes(AS, element, names)
        if attributes is None:
            return None
        char = attributes["AXMenuItemCmdChar"]
        if (
            isinstance(char, str)
            and char.lower() == key.lower()
            and attributes["AXMenuItemCmdModifiers"] == wanted
            and attributes["AXEnabled"] is True
        ):
            return element
        if depth >= 4:
            return None
        for child in attributes["AXChildren"] or ():
            if (hit := find(child, depth + 1)) is not None:
                return hit
        return None

    item = find(bar, 0)
    return item is not None and AS.AXUIElementPerformAction(item, "AXPress") == 0


def _ax() -> Any:
    try:
        import ApplicationServices as AS  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install the macOS extra: pip install 'arc-cua[macos]'") from exc
    return AS


def _quartz() -> Any:
    try:
        import Quartz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install the macOS extra: pip install 'arc-cua[macos]'") from exc
    return Quartz
