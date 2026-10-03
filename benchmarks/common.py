"""What every benchmark run records, and how two runs are compared.

A result file is JSON: {"meta": {...}, "results": [rows]}. ``meta`` says which arc
(version and git commit), which machine and which macOS produced it, so runs of
different versions can be compared. Each row has "suite", "scenario", "target" and
"driver", which together identify it across runs, plus timings and outcomes.
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "benchmarks"


def meta(**extra: Any) -> dict[str, Any]:
    """Which arc, machine and system produced a run."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    try:
        from importlib.metadata import version

        arc_version = version("arc-cua")
    except Exception:
        arc_version = "unknown"
    try:
        chip = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        chip = ""
    return {
        "arc_version": arc_version,
        "commit": git("rev-parse", "--short", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "macos": platform.mac_ver()[0],
        "machine": platform.machine(),
        "chip": chip,
        "python": platform.python_version(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **extra,
    }


class Series:
    """Timings of one measurement; the first sample is kept apart as the cold run."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self.first: float | None = None

    def add(self, value: float) -> None:
        if self.first is None:
            self.first = value
        else:
            self.samples.append(value)

    def summary(self) -> dict[str, Any]:
        values = sorted(self.samples)
        if not values:
            return {"first_ms": None if self.first is None else round(self.first, 1)}
        p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
        return {
            "first_ms": None if self.first is None else round(self.first, 1),
            "median_ms": round(statistics.median(values), 1),
            "p90_ms": round(p90, 1),
            "n": len(values),
        }


def save(suite: str, results: list[dict[str, Any]], run_meta: dict[str, Any], report: str) -> Path:
    """Write a run's JSON and Markdown report; returns the JSON path."""
    OUT.mkdir(parents=True, exist_ok=True)
    for row in results:
        row.setdefault("suite", suite)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{suite}-{run_meta.get('commit') or 'nocommit'}-{stamp}"
    path = OUT / f"{name}.json"
    path.write_text(json.dumps({"meta": run_meta, "results": results}, indent=2))
    (OUT / f"{name}.md").write_text(report + "\n")
    return path


def _key(row: dict[str, Any]) -> tuple:
    return (row.get("suite", ""), row.get("scenario", ""), row.get("target", ""), row.get("driver", ""))


def compare(old_path: Path, new_path: Path, *, slower: float = 1.2) -> str:
    """Markdown comparison of two runs: timing changes, and success that got worse.
    A measurement more than ``slower`` times its old median is flagged as a regression."""
    old, new = (json.loads(Path(p).read_text()) for p in (old_path, new_path))
    before = {_key(r): r for r in old["results"]}
    lines = [
        f"Old: {old['meta'].get('arc_version')} @ {old['meta'].get('commit')} ({old['meta'].get('started')})",
        f"New: {new['meta'].get('arc_version')} @ {new['meta'].get('commit')} ({new['meta'].get('started')})",
        "",
        "| Suite | Scenario | Target | Driver | Old median ms | New median ms | Change | Success old → new |",
        "|---|---|---|---|---|---|---|---|",
    ]
    regressions = 0
    for row in new["results"]:
        prior = before.get(_key(row))
        if prior is None:
            continue
        a, b = prior.get("median_ms"), row.get("median_ms")
        change = ""
        if a and b:
            ratio = b / a
            change = f"{ratio:.2f}×"
            if ratio > slower:
                change += " ⚠ slower"
                regressions += 1
        success = ""
        if prior.get("success") != row.get("success") and (prior.get("success") or row.get("success")):
            success = f"{prior.get('success', '–')} → {row.get('success', '–')}"
            if _rate(row.get("success")) < _rate(prior.get("success")):
                success += " ⚠"
                regressions += 1
        if change or success:
            lines.append(
                f"| {row.get('suite', '')} | {row['scenario']} | {row['target']} | {row['driver']} | "
                f"{a if a is not None else '–'} | {b if b is not None else '–'} | {change} | {success} |"
            )
    lines += ["", f"{regressions} regression(s) flagged (slower than {slower}× or lower success)."]
    return "\n".join(lines)


def _rate(text: Any) -> float:
    """"4/5" (optionally followed by notes) as a rate; 1.0 when there is none."""
    try:
        done, total = str(text).split()[0].split("/")[:2]
        return int(done) / int(total)
    except Exception:
        return 1.0
