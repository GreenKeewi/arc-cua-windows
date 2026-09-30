"""Execute one Codex-authored subtask through the public arc-cua API and record a trace."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from arc_cua import DesktopExecutor, RuntimeConfig, result_to_dict, subtask_from_dict
from arc_cua.backends import MacOSApp, MacOSHybridBackend
from arc_cua.policies import TypeSafeJevPolicy

REPO = Path(__file__).resolve().parents[2]


def prepare_subtask(payload, workspace: str):
    """Validate caller fields before appending the workspace constraint."""
    task = subtask_from_dict(payload)
    return replace(task, constraints=(
        *task.constraints, f"Only modify files and folders inside {workspace}.",
    ))


def error_details(exc: Exception) -> dict:
    """Keep diagnostic context without including credentials or local variables."""
    message = str(exc)
    credential = os.environ.get("TYPESAFE_API_KEY")
    if credential:
        message = message.replace(credential, "[REDACTED]")
    message = re.sub(r"apikey_[A-Za-z0-9_]+", "[REDACTED]", message)
    return {
        "error_type": type(exc).__name__,
        "error_message": message,
        "error_frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ],
    }


BUNDLE_IDS = {"Finder": "com.apple.finder", "Preview": "com.apple.Preview"}


def activate_app(name: str) -> None:
    import AppKit

    expected = BUNDLE_IDS[name]
    current = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if current is not None and current.bundleIdentifier() == expected:
        return
    running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(expected)
    if running:
        # `open -a Finder` sends a reopen event and can create/navigate a window.
        # Activate an existing app without changing the participant's UI state.
        running[0].activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    else:
        subprocess.run(["open", "-a", name], check=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        # Cocoa delivers activation/NSWorkspace updates on the run loop. Sleeping
        # alone can keep returning the previous foreground app in a CLI process.
        AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
            AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05),
        )
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is not None and app.bundleIdentifier() == expected:
            return
    raise RuntimeError(f"Could not bring {name} to the foreground")


def load_credentials(path: Path) -> None:
    """Read only TypeSafe settings; never execute a shell or log credential values."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        name, separator, value = line.partition("=")
        name = name.strip()
        if separator and name in {"TYPESAFE_API_KEY", "TYPESAFE_MODEL"}:
            parts = shlex.split(value.strip(), comments=True)
            if len(parts) != 1:
                raise ValueError(f"Expected one quoted or unquoted value for {name}")
            if not os.environ.get(name):
                os.environ[name] = parts[0]


class TracedPolicy:
    def __init__(self, policy, log):
        self.policy = policy
        self.log = log
        self.decisions = 0

    def decide(self, **kwargs):
        self.decisions += 1
        self.log({"event": "observation", "decision_number": self.decisions,
                  "snapshot": kwargs["snapshot"].compact()})
        decision = self.policy.decide(**kwargs)
        self.log({"event": "decision", "decision_number": self.decisions,
                  "kind": decision.kind, "terminal": decision.terminal,
                  "hotkey": decision.hotkey, "key": decision.key, "click_modifier": decision.click_modifier,
                  "input_key": decision.input_key, "target_id": decision.target_id,
                  "latency_ms": decision.latency_ms})
        return decision


class HandoffSession:
    """One policy client, and one desktop backend per app, reused across handoffs.

    The CLI runs a single handoff per process. A caller that plans several
    subtasks in-process can keep one session to avoid per-handoff startup.
    """

    def __init__(self) -> None:
        self.policy = TypeSafeJevPolicy()
        self.backends: dict[int, MacOSHybridBackend] = {}
        self._log = None
        self.policy.client.event_hooks["response"].append(self._log_provider_error)

    def backend_for(self, app: str) -> MacOSHybridBackend:
        pid = MacOSApp.from_bundle_id(BUNDLE_IDS[app]).pid
        if pid not in self.backends:
            self.backends[pid] = MacOSHybridBackend(pid)
        return self.backends[pid]

    def _log_provider_error(self, response) -> None:
        if response.is_error and self._log is not None:
            response.read()
            message = response.text.replace(os.environ["TYPESAFE_API_KEY"], "[REDACTED]")
            message = re.sub(r"apikey_[A-Za-z0-9_]+", "[REDACTED]", message)
            self._log({"event": "provider_error", "status": response.status_code, "message": message[:2000]})

    def run(self, trial: Path, task, *, app: str, timeout: float = 120, started: float | None = None) -> dict:
        """Execute one prepared subtask, write its trace under the trial, and return the result."""
        started = time.perf_counter() if started is None else started
        payload = {**task.compact(), "max_actions": task.max_actions}
        folder = trial / "handoffs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
        folder.mkdir(parents=True)
        (folder / "request.json").write_text(json.dumps(payload, indent=2) + "\n")

        with (folder / "events.jsonl").open("w") as trace:
            def log(event):
                event["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                trace.write(json.dumps(event) + "\n")
                trace.flush()

            self._log = log
            traced = TracedPolicy(self.policy, log)
            try:
                activate_app(app)
                executor = DesktopExecutor(self.backend_for(app), traced, config=RuntimeConfig(timeout_s=timeout))
                result = None
                recorded_steps = set()
                for event in executor.run_iter(task):
                    if event.record is not None and event.record.step not in recorded_steps:
                        recorded_steps.add(event.record.step)
                        record = event.record.compact()
                        record.update(hotkey=event.action.hotkey, key=event.action.key)
                        log({"event": "action", "record": record})
                        print(f"Step {event.step}: {event.action.kind.value}", file=sys.stderr, flush=True)
                    if event.result is not None:
                        result = event.result
                if result is None:
                    raise RuntimeError("arc-cua returned no terminal result")
                output = result_to_dict(result)
            except Exception as exc:
                details = error_details(exc)
                log({"event": "error", **details})
                output = {"status": "ERROR", **details}
            finally:
                self._log = None
        output.update(decision_cycles=traced.decisions,
                      handoff_seconds=round(time.perf_counter() - started, 3),
                      trace_directory=str(folder))
        (folder / "result.json").write_text(json.dumps(output, indent=2) + "\n")
        return output

    def close(self) -> None:
        self.policy.client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--app", choices=("Finder", "Preview"), required=True)
    parser.add_argument("--env-file", type=Path, default=REPO / ".env")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    started = time.perf_counter()
    trial = args.trial.resolve()
    config = json.loads((trial / "trial.json").read_text())
    if config["mode"] != "arc":
        parser.error("The handoff helper is only for arc trials")
    if not (trial / "start.json").exists() or (trial / "result.json").exists():
        parser.error("Start an unfinished trial before invoking a handoff")
    payload = json.loads(args.request.read_text())
    try:
        task = prepare_subtask(payload, config["workspace"])
    except ValueError as exc:
        parser.error(str(exc))
    load_credentials(args.env_file)
    if not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("TYPESAFE_API_KEY is missing. Set it in the environment or the ignored .env file.")
    session = HandoffSession()
    try:
        output = session.run(trial, task, app=args.app, timeout=args.timeout, started=started)
    finally:
        session.close()
    print(json.dumps(output, indent=2))
    if output["status"] == "ERROR":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
