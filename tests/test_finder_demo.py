from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc_cua import subtask_from_dict


def load_demo(name):
    path = Path(__file__).parents[1] / "examples/finder_preview" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_handoff_validates_before_adding_constraints():
    helper = load_demo("handoff")
    with pytest.raises(ValueError, match="constraints must be an array"):
        helper.prepare_subtask({"goal": "Move", "verification": ["Moved"],
                                "constraints": "Preserve files"}, "/trial")
    task = helper.prepare_subtask({"goal": "Move", "verification": ["Moved"],
                                   "constraints": ["Preserve files"]}, "/trial")
    assert task.constraints == ("Preserve files", "Only modify files and folders inside /trial.")


@pytest.mark.parametrize("already_frontmost", [True, False])
def test_handoff_activates_running_app_without_sending_reopen(monkeypatch, already_frontmost):
    helper = load_demo("handoff")
    activated = []
    finder = SimpleNamespace(bundleIdentifier=lambda: "com.apple.finder")
    current = [finder if already_frontmost else None]

    def activate(options):
        activated.append(options)

    def pump_run_loop(deadline):
        current[0] = finder

    finder.activateWithOptions_ = activate
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(
            frontmostApplication=lambda: current[0],
        )),
        NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda bundle: [finder]),
        NSApplicationActivateIgnoringOtherApps=2,
        NSRunLoop=SimpleNamespace(currentRunLoop=lambda: SimpleNamespace(runUntilDate_=pump_run_loop)),
        NSDate=SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda seconds: seconds),
    ))
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected reopen event"))
    helper.activate_app("Finder")
    assert activated == ([] if already_frontmost else [2])


def test_filename_trial_setup_start_and_verification(tmp_path):
    benchmark = load_demo("benchmark")
    corpus = tmp_path / "corpus"
    (corpus / "source").mkdir(parents=True)
    content = b"fixture contents unchanged"
    (corpus / "source/scan.pdf").write_bytes(content)
    name = "2026-09-01 - Sample Client - SC-INV-001.pdf"
    destination = f"Organized/Sample Client/Invoices/{name}"
    (corpus / "manifest.json").write_text(json.dumps({"documents": [{
        "source": "scan.pdf", "destination": destination,
        "sha256": hashlib.sha256(content).hexdigest(),
    }]}))
    setup = benchmark.setup(Namespace(name="files", mode="arc", root=tmp_path, corpus=corpus,
                                      count=1, rehearsal=True, workflow="filenames"))
    trial = Path(setup["trial"])
    workspace = trial / "workspace"
    assert (workspace / "Inbox" / name).read_bytes() == content
    prompt = (trial / "task.md").read_text()
    assert "Do not open or read the PDFs" in prompt
    example = json.loads(re.search(r"```json\n(.*?)\n```", prompt, re.S)[1])
    assert subtask_from_dict(example).verification == tuple(example["verification"])
    assert benchmark.start(trial)["status"] == "started"
    target = workspace / destination
    target.parent.mkdir(parents=True)
    (workspace / "Inbox" / name).rename(target)
    result = benchmark.finish(trial)
    assert result["success"]
    assert result["workflow"] == "filenames"


def test_summary_keeps_workflows_separate(tmp_path):
    benchmark = load_demo("benchmark")
    for name, extra in (("legacy", {}), ("filenames", {"workflow": "filenames"})):
        trial = tmp_path / "trials" / name
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({
            "mode": "direct", "rehearsal": False, "success": True,
            "invalid_reason": None, "elapsed_seconds": 10 if extra else 100, **extra,
        }))
    assert benchmark.summary(tmp_path, "filenames")["direct"]["median_success_seconds"] == 10
    assert benchmark.summary(tmp_path, "preview")["direct"]["median_success_seconds"] == 100
