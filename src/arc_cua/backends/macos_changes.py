"""Journal of structural changes in one app, from accessibility notifications.

A snapshot is a picture of a moment. Apps keep changing after it: a sheet slides
in half a second after a click, a menu opens, the focused window changes. The
journal numbers each such change as it arrives, so code holding a snapshot can
ask, just before acting on it, whether the app's structure has changed since.

Only structural notifications are journaled. Value and selection changes leave
the structure alone, and the target's own freshness check catches them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Notifications that mean a window, sheet or menu came or went, or focus moved to another window.
STRUCTURAL = (
    "AXWindowCreated",
    "AXSheetCreated",
    "AXDrawerCreated",
    "AXFocusedWindowChanged",
    "AXMainWindowChanged",
    "AXMenuOpened",
    "AXMenuClosed",
    "AXWindowMiniaturized",
    "AXWindowDeminiaturized",
    "AXApplicationHidden",
    "AXApplicationShown",
    "AXCreated",
    "AXUIElementDestroyed",
)

# AXCreated and AXUIElementDestroyed also fire for small parts of a window (rows,
# web nodes). Of those, only these roles change the structure.
_CONTAINER_ROLES = frozenset({"AXWindow", "AXSheet", "AXDrawer", "AXDialog", "AXPopover", "AXMenu"})
_FILTERED = frozenset({"AXCreated", "AXUIElementDestroyed"})

_HISTORY = 256


@dataclass(frozen=True, slots=True)
class Change:
    sequence: int
    notification: str
    role: str
    at: float  # time.monotonic()


class ChangeJournal:
    """Numbers the structural changes one app reports, on a background run loop."""

    def __init__(self, pid: int, *, ready_timeout_s: float = 1.0) -> None:
        self.pid = pid
        self._lock = threading.Lock()
        self._sequence = 0
        self._changes: deque[Change] = deque(maxlen=_HISTORY)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.available = False
        self._thread = threading.Thread(target=self._run, name=f"arc-cua-changes-{pid}", daemon=True)
        self._thread.start()
        self._ready.wait(ready_timeout_s)

    @property
    def sequence(self) -> int:
        """Number of the latest change; a snapshot records it to compare later."""
        with self._lock:
            return self._sequence

    def since(self, sequence: int) -> list[Change]:
        """Changes after ``sequence``, oldest first."""
        with self._lock:
            return [change for change in self._changes if change.sequence > sequence]

    def wait_after(self, sequence: int, timeout_s: float) -> bool:
        """Wait until a change after ``sequence`` arrives; False on timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.sequence > sequence:
                return True
            time.sleep(0.002)
        return self.sequence > sequence

    def close(self) -> None:
        self._stop.set()

    def _record(self, notification: str, role: str) -> None:
        with self._lock:
            self._sequence += 1
            self._changes.append(Change(self._sequence, notification, role, time.monotonic()))

    def _run(self) -> None:
        try:
            import ApplicationServices as AS
            import objc
            from CoreFoundation import (
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFRunLoopRunInMode,
                kCFRunLoopDefaultMode,
            )

            @objc.callbackFor(AS.AXObserverCreate)
            def callback(observer: Any, element: Any, notification: Any, refcon: Any) -> None:
                name = str(notification)
                role = ""
                if element is not None:
                    error, value = AS.AXUIElementCopyAttributeValue(element, "AXRole", None)
                    role = str(value) if error == 0 and value is not None else ""
                if name in _FILTERED and role not in _CONTAINER_ROLES:
                    # A destroyed element no longer reports its role; one of a window's
                    # parts going away is not a structural change on its own.
                    return
                self._record(name, role)

            error, observer = AS.AXObserverCreate(self.pid, callback, None)
            if error != 0 or observer is None:
                logger.debug("change journal unavailable pid=%s error=%s", self.pid, error)
                return
            app = AS.AXUIElementCreateApplication(self.pid)
            for name in STRUCTURAL:
                AS.AXObserverAddNotification(observer, app, name, None)
            CFRunLoopAddSource(CFRunLoopGetCurrent(), AS.AXObserverGetRunLoopSource(observer), kCFRunLoopDefaultMode)
            self.available = True
        except Exception as exc:  # The journal is an optimization; never fail the caller.
            logger.debug("change journal failed pid=%s: %s", self.pid, exc)
            return
        finally:
            self._ready.set()
        while not self._stop.is_set():
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
