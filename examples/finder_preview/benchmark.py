"""Create isolated trials and measure them; file operations here are setup/evaluation only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO / "output/finder-preview"
DEFAULT_CORPUS = REPO / "output/pdf/finder-preview"
PYTHON = REPO / ".venv/bin/python"
HERE = Path(__file__).resolve().parent


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_new_json(path: Path, value: dict) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def now() -> dict:
    return {"utc": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns()}


def task_prompt(trial: Path, config: dict) -> str:
    workspace = Path(config["workspace"])
    filenames_only = config.get("workflow", "preview") == "filenames"
    apps = "Finder" if filenames_only else "Finder + Preview"
    assignment = f"""Organize the {config['count']} PDFs in `{workspace / 'Inbox'}` using Finder.

Everything needed is in the filenames: `YYYY-MM-DD - Client - Reference.pdf`.
Group by client and reference type: INV means Invoices, PROP means Proposals,
and MTG means Meeting Notes. Create this hierarchy next to Inbox:

`Organized/<Client>/<Invoices|Proposals|Meeting Notes>/<unchanged filename>`

Move all files into the appropriate folders, leaving Inbox empty. Keep every
filename and the file contents unchanged. Retain each document exactly once.
Finish with the organized folders visible in Finder. Do not open or read the PDFs.
""" if filenames_only else f"""Organize the {config['count']} PDFs in `{workspace / 'Inbox'}` using Finder and Preview.

Open every PDF in Preview. Read its client, document type, primary date, and reference.
Open PDFs explicitly in Preview (Open With in Finder or Preview's Open dialog); the
machine's default PDF handler may be a different app. Do not change that global preference.
Create this hierarchy next to Inbox:

`Organized/<Client>/<Invoices|Proposals|Meeting Notes>/YYYY-MM-DD - Client - Reference.pdf`

Use the issue date for invoices, proposal date for proposals, and meeting date for meeting notes.
Other dates in a document can describe payment deadlines or follow-up meetings.
Use the client name and reference exactly as printed. Create the folders you need.
Move all documents into the hierarchy, leaving Inbox empty. Keep their contents unchanged;
retain every document exactly once. Finish with the organized folders visible in Finder.
"""
    interaction = (
        "Read filenames and perform file operations through Finder UI." if filenames_only
        else "Read documents in Preview and perform file operations through Finder/Preview UI."
    )
    commands = (
        f'"{PYTHON}" "{HERE / "benchmark.py"}" start "{trial}"',
        f'"{PYTHON}" "{HERE / "benchmark.py"}" finish "{trial}"',
    )
    common = f"""# {apps} comparison: {config['mode']}

{assignment}

## Run conditions

This is a desktop computer-use comparison. {interaction}
Use normal keyboard shortcuts and batching when useful.
Do not read PDF files, extract text with code, use filesystem tools to organize files, or use
application scripting commands to rename/move them. Do not inspect the corpus, manifest,
fixture generator, other trials, or organizer's answer key.

Only work in this trial's workspace. You may use computer-use tools to observe and interact
with the apps, and use the benchmark commands below. Activating the task apps is allowed.
Keep the same model, reasoning setting, screen resolution, and initial app state in both runs.
The starting view is Finder showing Inbox in list view, with no trial PDF already open.
Close leftover rehearsal tabs before either run so similarly named folders cannot be confused.

Run the start command immediately before your first task observation or action:

```sh
{commands[0]}
```

Perform the task. When done, or if you cannot continue, run finish once:

```sh
{commands[1]}
```

Finish freezes the result, including failures, and performs the same independent content
and path checks for both modes. Do not continue editing after it. Report the result honestly.
The timer includes {'filename review' if filenames_only else 'document reading'}, planning,
UI execution, handoffs, retries, and verification.
Tool-request approval or human interruptions invalidate a timed trial; record them in your final reply.
If interrupted, append `--invalid-reason "description of interruption"` to the finish command.

## Shared shortcut reference

- Return on a selected Finder item: rename it.
- MOD+SHIFT+N: create a folder in Finder.
- MOD+SHIFT+G: open Go to Folder.
- MOD+C, then MOD+ALT+V at the destination: move the copied Finder item.
- MOD+ARROW_UP: go to the parent folder in Finder.
- MOD+2: list view in Finder.

These are available to both runs. MOD means Command on macOS.
"""
    if config["mode"] == "direct":
        return common + """
## Direct mode

Complete the workflow with your normal computer-use tools. You own the interpretation,
planning, and UI actions. Do not invoke arc-cua or the handoff helper.
"""
    return common + f"""
## arc-cua mode

You own classification, destinations, and overall verification.
After observing the {'filenames' if filenames_only else 'document'}, delegate a bounded UI outcome,
such as creating destination folders or moving selected files, to arc-cua.
Review the result before continuing.
Use normal computer-use tools for observation, app activation, or recovery when necessary;
include any fallback work in the timer and mention it in your final reply.

Write your own JSON subtask request under `{trial / 'requests'}`. Its fields are goal, inputs,
verification, constraints, max_actions, and optional shortcuts. Supply literal filenames,
paths, folder names, and descriptions of any extra chords in the request. Declare only the
shortcuts relevant to this subtask. JEV chooses the actual UI actions.
Pass along relevant interaction hints from the shared shortcut reference; JEV only sees
the subtask you supply. Use separate handoffs for independently verifiable outcomes
when a broader handoff needs planning or recovery.

Required JSON types: goal is a non-empty string; verification is a non-empty array of
non-empty strings; constraints is an array of non-empty strings (default []); inputs is
an object mapping names to literal strings, numbers, or booleans; max_actions is a
positive integer; shortcuts is an object mapping uppercase chords to descriptions.
Use MOD for Command, ALT for Option, and ARROW_UP for Up. Return/Enter is a built-in
PRESS_KEY named ENTER, so do not declare an unmodified Return in shortcuts.
Every folder name, filename, or path JEV needs to type must be its own literal input;
mentioning it only in the goal is insufficient. Invalid types fail before any provider call.

Example of the JSON shape (illustrative values only; author your own subtask):

```json
{{
  "goal": "Create a folder named Review in the current Finder folder",
  "inputs": {{"folder_name": "Review"}},
  "verification": ["Review exists in the current Finder folder and its name is committed"],
  "constraints": ["Preserve existing files and folders"],
  "max_actions": 12,
  "shortcuts": {{"MOD+SHIFT+N": "Create a new folder in Finder"}}
}}
```

Invoke it with:

```sh
"{PYTHON}" "{HERE / 'handoff.py'}" --trial "{trial}" --app Finder --request REQUEST_JSON
```

`--app` can be Finder or Preview. It only activates that app. The helper calls the public
arc-cua API, prints the result and final desktop snapshot, and writes a trace to the trial.
It contains no filename decisions or predetermined UI action sequence.
The private API credential is loaded by the helper; do not read or print it.
"""


def setup(args) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", args.name):
        raise ValueError("Trial name must use lowercase letters, digits, and hyphens")
    corpus = args.corpus.resolve()
    documents = read_json(corpus / "manifest.json")["documents"]
    if not 1 <= args.count <= len(documents):
        raise ValueError(f"count must be between 1 and {len(documents)}")
    chosen = documents[:args.count]
    for document in chosen:
        source = corpus / "source" / document["source"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != document["sha256"]:
            raise ValueError(f"Source fixture changed: {source.name}")
    trial = args.root.resolve() / "trials" / args.name
    trial.mkdir(parents=True, exist_ok=False)
    inbox = trial / "workspace/Inbox"
    inbox.mkdir(parents=True)
    (trial / "requests").mkdir()
    for document in chosen:
        name = Path(document["destination"]).name if args.workflow == "filenames" else document["source"]
        shutil.copy2(corpus / "source" / document["source"], inbox / name)
    config = {
        "mode": args.mode, "rehearsal": args.rehearsal, "count": args.count,
        "corpus": str(corpus), "workspace": str(trial / "workspace"),
        "sources": [doc["source"] for doc in chosen],
        "workflow": args.workflow,
        "input_names": {doc["source"]: (Path(doc["destination"]).name if args.workflow == "filenames"
                                         else doc["source"]) for doc in chosen},
    }
    write_new_json(trial / "trial.json", config)
    (trial / "task.md").write_text(task_prompt(trial, config))
    return {"trial": str(trial), "workspace": str(inbox), "prompt": str(trial / "task.md")}


def verify(workspace: Path, documents: list[dict]) -> dict:
    expected = {document["destination"]: document["sha256"] for document in documents}
    expected_dirs = {"Inbox"}
    for relative in expected:
        expected_dirs.update(parent.as_posix() for parent in Path(relative).parents if parent != Path("."))
    actual = {}
    actual_dirs = set()
    unsafe = []
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace).as_posix()
        if path.is_symlink():
            unsafe.append(relative)
        elif path.is_dir():
            actual_dirs.add(relative)
        elif path.is_file() and path.name != ".DS_Store":
            actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    changed = sorted(key for key in expected.keys() & actual.keys() if expected[key] != actual[key])
    extra_dirs = sorted(actual_dirs - expected_dirs)
    missing_dirs = sorted(expected_dirs - actual_dirs)
    return {
        "success": not (missing or unexpected or changed or unsafe or extra_dirs or missing_dirs),
        "correct_documents": sum(actual.get(key) == digest for key, digest in expected.items()),
        "total_documents": len(expected), "missing": missing, "unexpected": unexpected,
        "changed_contents": changed, "symlinks": unsafe,
        "unexpected_folders": extra_dirs, "missing_folders": missing_dirs,
    }


def start(trial: Path) -> dict:
    config = read_json(trial / "trial.json")
    corpus = read_json(Path(config["corpus"]) / "manifest.json")["documents"]
    chosen = [doc for doc in corpus if doc["source"] in config["sources"]]
    initial = [{**doc, "destination": f"Inbox/{config.get('input_names', {}).get(doc['source'], doc['source'])}"}
               for doc in chosen]
    if not verify(Path(config["workspace"]), initial)["success"]:
        raise ValueError("Trial workspace does not match the initial fixture; create a fresh trial")
    stamp = now()
    write_new_json(trial / "start.json", stamp)
    return {"status": "started", "mode": config["mode"], "started_at": stamp["utc"]}


def finish(trial: Path, invalid_reason: str | None = None) -> dict:
    if (trial / "result.json").exists():
        raise ValueError("This trial is already finished; create a fresh trial for another attempt")
    config = read_json(trial / "trial.json")
    started = read_json(trial / "start.json")
    corpus = read_json(Path(config["corpus"]) / "manifest.json")["documents"]
    documents = [doc for doc in corpus if doc["source"] in config["sources"]]
    result = verify(Path(config["workspace"]), documents)
    ended = now()
    result.update({
        "mode": config["mode"], "rehearsal": config["rehearsal"],
        "workflow": config.get("workflow", "preview"),
        "invalid_reason": invalid_reason,
        "elapsed_seconds": round((ended["monotonic_ns"] - started["monotonic_ns"]) / 1e9, 3),
        "started_at": started["utc"], "finished_at": ended["utc"],
    })
    write_new_json(trial / "result.json", result)
    return result


def summary(root: Path, workflow: str = "preview") -> dict:
    results = [read_json(path) for path in (root / "trials").glob("*/result.json")]
    results = [result for result in results if result.get("workflow", "preview") == workflow]
    report = {}
    for mode in ("direct", "arc"):
        trials = [result for result in results if result["mode"] == mode and not result["rehearsal"]]
        valid = [result for result in trials if not result.get("invalid_reason")]
        successful = [result["elapsed_seconds"] for result in valid if result["success"]]
        report[mode] = {"valid_trials": len(valid), "invalid_trials": len(trials) - len(valid),
                        "successful_trials": len(successful),
                        "median_success_seconds": statistics.median(successful) if successful else None}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("setup")
    create.add_argument("name")
    create.add_argument("--mode", choices=("direct", "arc"), required=True)
    create.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    create.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    create.add_argument("--count", type=int, default=18)
    create.add_argument("--rehearsal", action="store_true")
    create.add_argument("--workflow", choices=("preview", "filenames"), default="preview")
    commands.add_parser("start").add_argument("trial", type=Path)
    done = commands.add_parser("finish")
    done.add_argument("trial", type=Path)
    done.add_argument("--invalid-reason")
    report = commands.add_parser("summary")
    report.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    report.add_argument("--workflow", choices=("preview", "filenames"), default="preview")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            result = setup(args)
        elif args.command == "summary":
            result = summary(args.root, args.workflow)
        elif args.command == "start":
            result = start(args.trial.resolve())
        else:
            result = finish(args.trial.resolve(), args.invalid_reason)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, indent=2))
    if args.command == "finish" and not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
