"""Accessibility notification counter used as a settle signal.

A background thread subscribes to AX notifications (focus, value, creation,
destruction, selection, layout...) for one application at a time and counts
them. The count changes within milliseconds of an app reacting to input,
usually before its pixels do, and also for actions with no visible effect.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

NOTIFICATIONS = (
    "AXFocusedUIElementChanged",
    "AXFocusedWindowChanged",
    "AXMainWindowChanged",
    "AXWindowCreated",
    "AXCreated",
    "AXUIElementDestroyed",
    "AXValueChanged",
    "AXTitleChanged",
    "AXSelectedChildrenChanged",
    "AXSelectedRowsChanged",
    "AXSelectedTextChanged",
    "AXRowCountChanged",
    "AXLayoutChanged",
    "AXMenuOpened",
    "AXMenuClosed",
)


class AXEventMonitor:
    """Counts AX notifications posted by the currently watched application."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._pid: int | None = None
        self._requested_pid: int | None = None
        self._ready = threading.Event()
        self._failed = False
        self._thread: threading.Thread | None = None

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def watch(self, pid: int) -> bool:
        """Watch ``pid``; returns False when notifications are unavailable."""
        if self._failed:
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="arc-cua-ax-events", daemon=True)
            self._thread.start()
        with self._lock:
            self._requested_pid = pid
        if self._pid == pid:
            return True
        # The monitor thread switches apps on its next loop turn (<= ~20 ms).
        deadline = time.monotonic() + 0.2
        while self._pid != pid and time.monotonic() < deadline and not self._failed:
            time.sleep(0.005)
        return self._pid == pid

    def _record(self) -> None:
        with self._lock:
            self._count += 1

    def _run(self) -> None:
        try:
            import ApplicationServices as AS
            import objc
            from CoreFoundation import (
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFRunLoopRemoveSource,
                CFRunLoopRunInMode,
                kCFRunLoopDefaultMode,
            )
        except ImportError:
            logger.debug("AX notifications unavailable: PyObjC frameworks missing")
            self._failed = True
            return

        @objc.callbackFor(AS.AXObserverCreate)
        def callback(observer: Any, element: Any, notification: Any, refcon: Any) -> None:
            self._record()

        loop = CFRunLoopGetCurrent()
        observer = source = None
        while True:
            with self._lock:
                wanted = self._requested_pid
            if wanted is not None and wanted != self._pid:
                if source is not None:
                    CFRunLoopRemoveSource(loop, source, kCFRunLoopDefaultMode)
                observer = source = None
                error, created = AS.AXObserverCreate(wanted, callback, None)
                if error == 0 and created is not None:
                    app = AS.AXUIElementCreateApplication(wanted)
                    for name in NOTIFICATIONS:
                        AS.AXObserverAddNotification(created, app, name, None)
                    observer = created
                    source = AS.AXObserverGetRunLoopSource(observer)
                    CFRunLoopAddSource(loop, source, kCFRunLoopDefaultMode)
                else:
                    logger.debug("AXObserverCreate failed pid=%s error=%s", wanted, error)
                self._pid = wanted
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, False)
