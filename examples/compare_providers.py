"""Run the same browser tasks with each decision provider and compare them.

    pip install -e '.[browser]'
    python examples/compare_providers.py --runs 3 [--headful]

Providers run when their key is set in the environment or the repository's .env
(TYPESAFE_API_KEY for JEV). Success is judged by an independent check of the
page, not by the model's completion claim.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arc_cua import DesktopExecutor, Subtask, TerminalKind
from arc_cua.backends import ChromeBackend
from arc_cua.policies import TypeSafeJevPolicy

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "browser"


@dataclass(frozen=True)
class Task:
    name: str
    url: str
    subtask: Subtask
    check: Callable[[ChromeBackend], bool]


def page_value(backend: ChromeBackend, expression: str):
    result = backend._call("Runtime.evaluate", {"expression": expression, "returnByValue": True})  # noqa: SLF001
    return result["result"].get("value")


def tasks(local: str) -> list[Task]:
    return [
        Task(
            "Trip form (local)",
            f"{local}/index.html",
            Subtask(
                goal="Search for trips to Zurich in Business class with flexible dates",
                inputs={"city": "Zurich", "cabin": "Business"},
                verification=("The page shows results for Zurich in Business with flexible dates",),
                max_actions=12,
            ),
            lambda b: page_value(b, "result.textContent") == "Results for Zurich in Business (flexible)",
        ),
        Task(
            "Covered button (local)",
            f"{local}/index.html",
            Subtask(
                goal="Press the Bottom button at the end of the page",
                verification=("The page says Bottom reached",),
                constraints=("Dismiss anything covering the button first",),
                max_actions=12,
            ),
            lambda b: page_value(b, "result.textContent") == "Bottom reached",
        ),
        Task(
            "Wikipedia search (live)",
            "https://en.wikipedia.org/wiki/Main_Page",
            Subtask(
                goal="Open the Wikipedia article about Gödel's incompleteness theorems",
                inputs={"query": "Gödel's incompleteness theorems"},
                verification=("The article titled Gödel's incompleteness theorems is open",),
                max_actions=10,
            ),
            lambda b: page_value(b, "location.pathname") == "/wiki/G%C3%B6del%27s_incompleteness_theorems",
        ),
    ]


def providers() -> dict[str, tuple[Callable[[], object], bool]]:
    """Name -> (policy factory, whether each decision gets a screenshot)."""
    found = {}
    if os.environ.get("TYPESAFE_API_KEY"):
        found["JEV (TypeSafe)"] = (TypeSafeJevPolicy, False)
    return found


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?(TYPESAFE_API_KEY)\s*=\s*['\"]?([^'\"\s]+)", line)
        if match and not os.environ.get(match.group(1)):
            os.environ[match.group(1)] = match.group(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()
    load_env()
    available = providers()
    if not available:
        raise SystemExit("Set TYPESAFE_API_KEY")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    local = f"http://127.0.0.1:{server.server_port}"

    rows = []
    with ChromeBackend.launch(headless=not args.headful) as backend:
        for task in tasks(local):
            for provider, (make_policy, screenshots) in available.items():
                backend.capture_screenshots = screenshots
                times, decisions, latencies, passed = [], [], [], 0
                for _ in range(args.runs):
                    backend.navigate(task.url)
                    executor = DesktopExecutor(backend, make_policy())
                    started = time.perf_counter()
                    steps = []
                    try:
                        for event in executor.run_iter(task.subtask):
                            steps.append(event.decision)
                            result = event.result
                    except Exception as exc:  # A provider error is a failed run, not a crash.
                        print(f"  {provider} / {task.name}: {type(exc).__name__}: {exc}")
                        continue
                    elapsed = time.perf_counter() - started
                    ok = result.status == TerminalKind.SUBTASK_COMPLETE and task.check(backend)
                    passed += ok
                    if ok:
                        times.append(elapsed)
                        decisions.append(len(steps))
                    latencies += [d.latency_ms for d in steps if d.latency_ms]
                    outcome = "pass" if ok else "FAIL"
                    print(f"  {provider} / {task.name}: {outcome} {elapsed:.2f}s {len(steps)} decisions")
                rows.append((
                    task.name, provider, f"{passed}/{args.runs}",
                    f"{statistics.median(times):.1f} s" if times else "–",
                    f"{statistics.median(decisions):g}" if decisions else "–",
                    f"{statistics.median(latencies):.0f} ms" if latencies else "–",
                ))
    server.shutdown()

    print("\n| Task | Provider | Verified | Median time | Decisions | Median decision latency |")
    print("|---|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")


if __name__ == "__main__":
    main()
