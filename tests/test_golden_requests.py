"""Golden files for the exact request TypeSafeJevPolicy sends.

The request is the decision model's prompt: rules, questions, candidates, element
table and redaction. These tests rebuild it from fixed observations and compare it
with the files in tests/golden/ character for character, so any change to it is
visible in review. After an intended change, regenerate them with

    ARC_UPDATE_GOLDEN=1 python -m pytest tests/test_golden_requests.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from arc_cua import ActionKind, Bounds, Decision, DesktopElement, DesktopSnapshot, Subtask
from arc_cua.models import ActionRecord, ExecutableAction
from arc_cua.policies import TypeSafeJevPolicy

GOLDEN = Path(__file__).parent / "golden"


def trip_form():
    snapshot = DesktopSnapshot(application="Chrome", window="Trip planner", revision="1", elements=(
        DesktopElement(id="w1", role="heading", name="Plan a trip", source="chrome_dom", metadata={"level": 1}),
        DesktopElement(id="w3", role="textbox", name="Destination", value="", source="chrome_dom",
                       actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT), bounds=Bounds(24, 80, 200, 24)),
        DesktopElement(id="w4", role="combobox", name="Cabin", value="Economy", source="chrome_dom",
                       actions=(ActionKind.SET_VALUE,),
                       metadata={"options": ["Economy", "Business", "First"], "value_type": "text"}),
        DesktopElement(id="w5", role="checkbox", name="Flexible dates", value=False, source="chrome_dom",
                       actions=(ActionKind.CLICK,)),
        DesktopElement(id="w6", role="button", name="Search", source="chrome_dom", actions=(ActionKind.CLICK,)),
    ), context={"backend": "chrome", "url": "http://127.0.0.1/index.html", "more_below": True})
    task = Subtask(
        goal="Search for trips to Zurich in Business class with flexible dates",
        inputs={"city": "Zurich", "cabin": "Business"},
        verification=("The page shows results for Zurich in Business with flexible dates",),
    )
    return task, snapshot, ()


def finder_list():
    rows = tuple(
        DesktopElement(id=f"ax_{10 + i}", role="TextField", name=name, value=name, source="macos_ax",
                       actions=(ActionKind.CLICK, ActionKind.DOUBLE_CLICK), selected=i == 1, parent_id="ax_2",
                       metadata={"url": f"file:///Users/example/Inbox/{name.replace(' ', '%20')}"})
        for i, name in enumerate(("Invoice 01.pdf", "Invoice 02.pdf", "Proposal 01.pdf"))
    )
    snapshot = DesktopSnapshot(application="Finder", window="Inbox", revision="1", elements=(
        DesktopElement(id="ax_1", role="Window", name="Inbox", source="macos_ax"),
        DesktopElement(id="ax_2", role="Outline", name="", source="macos_ax", parent_id="ax_1"),
        *rows,
    ), context={"backend": "macos_ax", "pid": 1})
    task = Subtask(
        goal="Move the invoices into the Invoices folder",
        verification=("Both invoices are in the Invoices folder",),
        inputs={"folder": "Invoices"},
        shortcuts={"MOD+SHIFT+N": "Create a new folder in the current Finder window"},
    )
    return task, snapshot, ()


def field_without_inputs():
    snapshot = DesktopSnapshot(application="Mail", window="New Message", revision="1", elements=(
        DesktopElement(id="to", role="TextField", name="To", value="", source="macos_ax",
                       actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT)),
        DesktopElement(id="send", role="Button", name="Send", source="macos_ax", actions=(ActionKind.CLICK,)),
    ))
    task = Subtask(goal="Address the message to Sam", verification=("The To field holds Sam's address",))
    return task, snapshot, ()


def secrets_and_risks():
    snapshot = DesktopSnapshot(application="Mail", window="Account", revision="2", elements=(
        DesktopElement(id="password", role="TextField", name="Password", value="hunter2", source="macos_ax",
                       actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT)),
        DesktopElement(id="delete", role="Button", name="Delete account", source="macos_ax",
                       actions=(ActionKind.CLICK,)),
        DesktopElement(id="save", role="Button", name="Save", source="macos_ax", actions=(ActionKind.CLICK,)),
    ))
    task = Subtask(goal="Update the account password", verification=("The password is saved",),
                   inputs={"new_password": "hunter2"}, secret_inputs=("new_password",))
    typed = ActionRecord(
        step=1, decision=Decision(kind=ActionKind.TYPE_TEXT, target_id="password", input_key="new_password"),
        action=ExecutableAction(kind=ActionKind.TYPE_TEXT, target_id="password", value="hunter2"),
        before_revision="1", after_revision="2", state_changed=True, elapsed_ms=420, target_name="Password",
    )
    return task, snapshot, (typed,)


CASES = {
    "trip_form": trip_form,
    "finder_list": finder_list,
    "field_without_inputs": field_without_inputs,
    "secrets_and_risks": secrets_and_risks,
}


def request_body(task, snapshot, history) -> str:
    """The exact JSON body TypeSafeJevPolicy posts, pretty-printed in wire order."""
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        choices = body["questions"]["operation"]["criteria"]
        return httpx.Response(200, json={"answers": {"operation": {
            "choice": "NEEDS_AGENT", "confidence": 1.0,
            "probabilities": {key: float(key == "NEEDS_AGENT") for key in choices},
        }}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        policy = TypeSafeJevPolicy(api_key="test", model="jev-golden", client=client)
        policy.decide(subtask=task, snapshot=snapshot, history=history)
    return json.dumps(captured[0], indent=2, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name", sorted(CASES))
def test_request_matches_golden_file(name):
    task, snapshot, history = CASES[name]()
    body = request_body(task, snapshot, history)
    path = GOLDEN / f"jev_request_{name}.json"
    if os.environ.get("ARC_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(body, encoding="utf-8")
    assert path.exists(), f"{path.name} is missing; run with ARC_UPDATE_GOLDEN=1 to create it"
    assert body == path.read_text(encoding="utf-8"), (
        f"The JEV request for {name} changed. If intended, regenerate with ARC_UPDATE_GOLDEN=1 and review the diff."
    )


def test_golden_files_never_contain_secrets():
    for path in GOLDEN.glob("jev_request_*.json"):
        assert "hunter2" not in path.read_text(encoding="utf-8"), path.name
