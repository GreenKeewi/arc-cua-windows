"""Foreground Windows driver implementing the semantic subset of the MCP driver."""
from __future__ import annotations

import threading
import time
from typing import Any

from .backends.windows_uia import WindowsUIAAdapter, WindowsUIABackend
from .driver import ActResult, SettleReport, WindowInfo, WindowTarget, package_version
from .errors import Cancelled, StaleDesktopState
from .models import ActionKind, DesktopSnapshot, ExecutableAction
from .settling import SettleTiming, wait_for_quiet

WINDOWS_INSTRUCTIONS = (
    "Windows foreground MVP: requires an unlocked interactive desktop and restored visible windows. "
    "Actions can move the real pointer and keyboard focus. MOD means Ctrl. Call windows(pid), "
    "observe(pid, window_id), then act(snapshot, action, element). A stale result means nothing ran; "
    "use its fresh snapshot and retry. UI Automation only: no OCR, screenshot, coordinate actions, "
    "dragging, background/minimized control or menu-path tools. Password values are not observed. "
    "Do not use the desktop concurrently with native input."
)
WINDOWS_TOOL_NAMES = {"status", "apps", "windows", "observe", "act", "settle", "wait", "release"}


class WindowsDriver:
    def __init__(self, *, adapter: Any = None) -> None:
        self.adapter = adapter or WindowsUIAAdapter()
        self.cancelled = threading.Event()
        self._backends: dict[WindowTarget, WindowsUIABackend] = {}

    def status(self) -> dict[str, Any]:
        return {"version": package_version(), "backend": "windows_uia", "foreground_required": True,
                "background_input": False, "minimized_control": False, "ocr": False,
                "desktop_validation": "Run windows-smoke on this machine to validate live controls"}

    def apps(self) -> list[dict[str, Any]]:
        apps = {}
        for window in self.adapter.windows():
            pid = window["pid"]
            apps.setdefault(pid, {"pid": pid, "name": window["title"], "frontmost": False})
            apps[pid]["frontmost"] |= window["frontmost"]
        return list(apps.values())

    def windows(self, pid: int) -> list[WindowInfo]:
        return [WindowInfo(**{k: w[k] for k in ("window_id", "title", "bounds", "on_screen", "minimized")})
                for w in self.adapter.windows(pid)]

    def _backend(self, where: WindowTarget | int) -> WindowsUIABackend:
        if isinstance(where, int):
            where = WindowTarget(where, self.adapter.resolve(where, None))
        if where not in self._backends:
            self._backends[where] = WindowsUIABackend(where.pid, window_id=where.window_id, adapter=self.adapter)
        return self._backends[where]

    def observe(self, where: WindowTarget | int) -> DesktopSnapshot:
        return self._backend(where).observe()

    def _of(self, snapshot: DesktopSnapshot) -> WindowsUIABackend:
        return self._backend(WindowTarget(snapshot.context["pid"], snapshot.context["window_id"]))

    def _stop(self) -> None:
        if self.cancelled.is_set():
            self.cancelled.clear()
            raise Cancelled("Cancelled; no further Windows action performed")

    def act(self, snapshot: DesktopSnapshot, kind: ActionKind | str, target: str | None = None,
            *, value: Any = None, key: str | None = None, hotkey: str | None = None,
            scroll_direction: str | None = None, click_modifier: str | None = None,
            settle: bool = False) -> ActResult:
        self._stop()
        backend = self._of(snapshot)
        action = ExecutableAction(kind=ActionKind(kind), target_id=target, value=value, key=key,
                                  hotkey=hotkey, scroll_direction=scroll_direction, click_modifier=click_modifier)
        started = time.perf_counter()
        try:
            backend.execute(snapshot, action)
        except StaleDesktopState:
            return ActResult("stale", backend.observe())
        fresh, report = self.settle(snapshot) if settle else (None, None)
        return ActResult("done", fresh, elapsed_ms=(time.perf_counter() - started) * 1000, settled=report)

    def settle(self, snapshot: DesktopSnapshot, *, reaction_s: float = 0.6, quiet_s: float = 0.15,
               timeout_s: float = 2.0) -> tuple[DesktopSnapshot, SettleReport]:
        backend = self._of(snapshot)
        report = wait_for_quiet(lambda: backend.observe().revision, snapshot.revision,
                                SettleTiming(reaction_s, quiet_s, timeout_s, 0.05), stop=self.cancelled.is_set)
        self._stop()
        return backend.observe(), SettleReport(report.reacted, report.timed_out, report.elapsed_s * 1000)

    def wait(self, snapshot: DesktopSnapshot, *, timeout_s: float = 1.0) -> DesktopSnapshot:
        return self.settle(snapshot, reaction_s=timeout_s, quiet_s=0.05, timeout_s=timeout_s)[0]

    def parked(self, pid: int) -> bool:
        return False

    def release(self, pid: int) -> bool:
        targets = [target for target in self._backends if target.pid == pid]
        for target in targets:
            self._backends.pop(target).close()
        return bool(targets)

    def release_all(self) -> list[int]:
        pids = sorted({target.pid for target in self._backends})
        for pid in pids:
            self.release(pid)
        return pids

    def close(self) -> None:
        self.release_all()
