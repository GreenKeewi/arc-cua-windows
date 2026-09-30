"""Command line interface.

``arc-cua run`` executes one subtask against one macOS app and exits. It reads a
single JSON object from standard input, prints one JSON line to standard output
after every action and a final result line, and sends logs to standard error.
To stop a run, terminate the process; windows it moved out of sight are put back.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any

from .api import subtask_from_dict
from .errors import TargetUnavailable
from .models import ExecutionResult, StepEvent, Subtask
from .protocols import DecisionPolicy, DesktopBackend
from .runtime import DesktopExecutor, RuntimeConfig

logger = logging.getLogger("arc_cua.cli")

BACKENDS = ("hybrid", "ax")


def _jev_policy(api_key: str | None, model: str | None) -> DecisionPolicy:
    from .policies import TypeSafeJevPolicy

    return TypeSafeJevPolicy(api_key=api_key, model=model)


# Decision providers by name: (api_key, model) -> policy.
PROVIDERS: dict[str, Callable[[str | None, str | None], DecisionPolicy]] = {
    "jev": _jev_policy,
}


class InvalidRequest(ValueError):
    """The input is not a valid run request."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    app: Mapping[str, Any]
    subtask: Subtask
    provider: str
    api_key: str | None
    model: str | None
    backend: str
    config: RuntimeConfig


def parse_request(payload: Any) -> RunRequest:
    if not isinstance(payload, Mapping):
        raise InvalidRequest("Input must be one JSON object")
    allowed = {"app", "subtask", "provider", "backend", "timeout_s", "min_confidence", "min_margin"}
    unknown = set(payload) - allowed
    if unknown:
        raise InvalidRequest(f"Unknown fields: {sorted(map(str, unknown))}")
    missing = {"app", "subtask", "provider"} - set(payload)
    if missing:
        raise InvalidRequest(f"Missing required fields: {sorted(missing)}")

    app = payload["app"]
    if not isinstance(app, Mapping) or len(app) != 1 or not ({"pid", "bundle_id"} & set(app)):
        raise InvalidRequest('app must be {"pid": <process ID>} or {"bundle_id": "<bundle identifier>"}')
    if "pid" in app and (type(app["pid"]) is not int or app["pid"] <= 0):
        raise InvalidRequest("app.pid must be a positive integer")
    if "bundle_id" in app and (not isinstance(app["bundle_id"], str) or not app["bundle_id"].strip()):
        raise InvalidRequest("app.bundle_id must be a non-empty string")

    try:
        subtask = subtask_from_dict(payload["subtask"])
    except ValueError as exc:
        raise InvalidRequest(f"subtask: {exc}") from exc

    provider = payload["provider"]
    if not isinstance(provider, Mapping):
        raise InvalidRequest('provider must be an object such as {"name": "jev", "api_key": "..."}')
    unknown = set(provider) - {"name", "api_key", "model"}
    if unknown:
        raise InvalidRequest(f"Unknown provider fields: {sorted(map(str, unknown))}")
    name = provider.get("name")
    if name not in PROVIDERS:
        raise InvalidRequest(f"provider.name must be one of {sorted(PROVIDERS)}")
    for field in ("api_key", "model"):
        value = provider.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InvalidRequest(f"provider.{field} must be a non-empty string")

    backend = payload.get("backend", "hybrid")
    if backend not in BACKENDS:
        raise InvalidRequest(f"backend must be one of {list(BACKENDS)}")

    timeout_s = payload.get("timeout_s")
    if timeout_s is not None and (
        type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0
    ):
        raise InvalidRequest("timeout_s must be a positive number of seconds")
    thresholds = {}
    for field in ("min_confidence", "min_margin"):
        value = payload.get(field)
        if value is not None and type(value) not in (int, float):
            raise InvalidRequest(f"{field} must be a number between 0 and 1")
        thresholds[field] = value
    try:
        config = RuntimeConfig(timeout_s=timeout_s, **thresholds)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc

    return RunRequest(
        app=dict(app),
        subtask=subtask,
        provider=name,
        api_key=provider.get("api_key"),
        model=provider.get("model"),
        backend=backend,
        config=config,
    )


def make_backend(app: Mapping[str, Any], kind: str) -> DesktopBackend:
    from .backends.macos_app import MacOSApp

    if sys.platform != "darwin":
        raise RuntimeError("arc-cua run controls macOS apps and needs macOS")
    pid = app["pid"] if "pid" in app else MacOSApp.from_bundle_id(app["bundle_id"]).pid
    if kind == "ax":
        from .backends.macos_ax import MacOSAXBackend

        return MacOSAXBackend(pid)
    from .backends.macos_hybrid import MacOSHybridBackend

    return MacOSHybridBackend(pid)


def action_line(event: StepEvent) -> dict[str, Any]:
    assert event.record is not None
    decision = event.record.decision
    return {"type": "action", **event.record.compact(), "confidence": decision.confidence, "margin": decision.margin}


def result_line(result: ExecutionResult) -> dict[str, Any]:
    return {
        "type": "result",
        "status": result.status.value,
        "reason": result.reason,
        "actions_taken": result.actions_taken,
        "observations": list(result.observations),
        "application": result.final_snapshot.application,
        "window": result.final_snapshot.window,
    }


def run(stdin: IO[str], stdout: IO[str]) -> int:
    """Execute one run request. Returns the process exit code."""

    def emit(line: Mapping[str, Any]) -> None:
        stdout.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        stdout.flush()

    def fail(message: str, code: int) -> int:
        logger.error("%s", message)
        emit({"type": "error", "error": message})
        return code

    try:
        request = parse_request(json.load(stdin))
        policy = PROVIDERS[request.provider](request.api_key, request.model)
    except json.JSONDecodeError as exc:
        return fail(f"Input is not valid JSON: {exc}", 2)
    except ValueError as exc:
        return fail(f"Invalid input: {exc}", 2)

    backend = None
    try:
        backend = make_backend(request.app, request.backend)
        if (open_backend := getattr(backend, "open", None)) is not None:
            open_backend()
        logger.info("run app=%s backend=%s provider=%s", request.app, request.backend, request.provider)
        executor = DesktopExecutor(backend, policy, config=request.config)
        for event in executor.run_iter(request.subtask):
            if event.record is not None:
                emit(action_line(event))
            if event.result is not None:
                emit(result_line(event.result))
        return 0
    except (TargetUnavailable, PermissionError) as exc:
        return fail(str(exc), 1)
    except Exception as exc:
        logger.debug("run failed", exc_info=True)
        return fail(f"{type(exc).__name__}: {exc}", 1)
    finally:
        if backend is not None and (close_backend := getattr(backend, "close", None)) is not None:
            close_backend()


def _terminate(signum: int, frame: Any) -> None:
    # Unwind normally so the backend puts parked windows back and key focus is returned.
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arc-cua", description="Fast desktop subtask execution with decision models.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser(
        "run",
        help="run one subtask read as JSON from standard input",
        description=(
            "Read one JSON object from standard input with the target app, the subtask and the decision "
            "provider. Print one JSON line per action and a final result line to standard output, then exit. "
            "Logs go to standard error. Terminate the process to stop a run."
        ),
    )
    run_parser.add_argument("-v", "--verbose", action="store_true", help="log debug detail to standard error")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="arc-cua %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _terminate)

    # Only protocol lines reach standard output; anything else printed goes to standard error.
    stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        return run(sys.stdin, stdout)
    except KeyboardInterrupt:
        return 130
    finally:
        sys.stdout = stdout


if __name__ == "__main__":
    raise SystemExit(main())
