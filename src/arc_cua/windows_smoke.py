"""Model-free live desktop demo, available as ``arc-cua windows-smoke``."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from importlib.resources import as_file, files

from .backends.windows_uia import WindowsUIABackend
from .models import ActionKind, Decision, Subtask, TerminalKind
from .runtime import DesktopExecutor, RuntimeConfig

MESSAGE = "Hello from arc-cua + {literal} café"


class SmokePolicy:
    def decide(self, *, subtask, snapshot, history):
        index = len(history)
        if index == 0:
            target = next(e for e in snapshot.elements if e.name == "Message" and ActionKind.TYPE_TEXT in e.actions)
            return Decision(kind=ActionKind.TYPE_TEXT, target_id=target.id, input_key="message")
        if index == 1:
            return Decision(kind=ActionKind.HOTKEY, hotkey="MOD+A")
        if index == 2:
            return Decision(kind=ActionKind.PRESS_KEY, key="END")
        if index == 3:
            target = next(e for e in snapshot.elements if e.name == "Items" and ActionKind.SCROLL in e.actions)
            return Decision(kind=ActionKind.SCROLL, target_id=target.id, scroll_direction="DOWN")
        if index == 4:
            target = next(e for e in snapshot.elements if e.name == "Apply message" and ActionKind.CLICK in e.actions)
            return Decision(kind=ActionKind.CLICK, target_id=target.id)
        return Decision(terminal=TerminalKind.SUBTASK_COMPLETE)


def main() -> int:
    if sys.platform != "win32":
        print(json.dumps({"status": "unsupported_platform", "live_windows_validated": False,
                          "reason": "Run this demo on Windows with an unlocked interactive desktop"}))
        return 2
    child = None
    backend = None
    try:
        with as_file(files("arc_cua").joinpath("windows_fixture.ps1")) as fixture:
            child = subprocess.Popen(["powershell.exe", "-NoProfile", "-STA", "-File", str(fixture)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            backend = WindowsUIABackend(child.pid)
            deadline = time.monotonic() + 20
            while True:
                if child.poll() is not None:
                    raise RuntimeError("Smoke fixture exited; check PowerShell script policy on this machine")
                try:
                    snapshot = backend.observe()
                    if any(e.name == "Message" for e in snapshot.elements):
                        break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("Smoke form controls did not appear within 20 seconds")
                time.sleep(0.2)
            task = Subtask(goal="Fill the smoke form and apply its message", inputs={"message": MESSAGE},
                           verification=("Result label contains the exact supplied message",), max_actions=8)
            executor = DesktopExecutor(backend, SmokePolicy(), config=RuntimeConfig(
                timeout_s=40, verify=lambda snapshot, task: any(
                    e.name == "Received: " + MESSAGE for e in snapshot.elements)))
            result = executor.run(task)
            passed = result.status == TerminalKind.SUBTASK_COMPLETE and result.actions_taken == 5
            print(json.dumps({"status": result.status.value, "live_windows_validated": passed,
                              "actions": [r.action.kind.value for r in result.history], "reason": result.reason,
                              "fixture_controls": [{"role": e.role, "name": e.name, "value": e.value}
                                                   for e in result.final_snapshot.elements
                                                   if e.role in ("TextField", "Text", "Button") and e.visible],
                              "scope": "Disposable WinForms fixture only; other apps need manual testing"}))
            return 0 if passed else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "live_windows_validated": False,
                          "reason": f"{type(exc).__name__}: {exc}"}))
        return 1
    finally:
        if backend is not None:
            backend.close()
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
