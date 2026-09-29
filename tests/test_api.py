from __future__ import annotations

import pytest

from arc_cua import (
    ActionKind,
    DesktopElement,
    DesktopSnapshot,
    ExecutionResult,
    Subtask,
    TerminalKind,
    result_to_dict,
    subtask_from_dict,
)


def test_json_boundary_requires_agent_verification() -> None:
    task = subtask_from_dict(
        {
            "goal": "Type a title",
            "verification": ["Title field contains Sydney 2026"],
            "inputs": {"title": "Sydney 2026"},
        }
    )
    assert task.inputs["title"] == "Sydney 2026"
    assert task.verification == ("Title field contains Sydney 2026",)


def test_subtask_from_dict_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Unknown subtask fields"):
        subtask_from_dict({"goal": "x", "verification": ["y"], "fake_field": True})


def test_subtask_from_dict_defaults() -> None:
    task = subtask_from_dict({"goal": "x", "verification": ["y"]})
    assert task.max_actions == 30
    assert task.inputs == {}
    assert task.constraints == ()


@pytest.mark.parametrize("field", ["verification", "constraints"])
@pytest.mark.parametrize("value", ["One complete sentence", None, {"criterion": "done"}, [""], [1]])
def test_criteria_require_arrays_of_nonempty_strings(field, value) -> None:
    payload = {"goal": "Do the task", "verification": ["Done"], field: value}
    for parse in (subtask_from_dict, lambda data: Subtask(**data)):
        with pytest.raises(ValueError, match=field):
            parse(payload)


@pytest.mark.parametrize(("field", "value"), [
    ("goal", None), ("goal", 123), ("goal", " "),
    ("verification", []), ("max_actions", True), ("max_actions", "3"),
    ("max_actions", 1.5), ("max_actions", 0), ("inputs", []),
    ("inputs", {"name": None}), ("inputs", {"name": ["not a literal"]}),
    ("inputs", {"number": float("inf")}), ("inputs", {"number": float("nan")}),
    ("inputs", {1: "name"}), ("metadata", []),
])
def test_json_boundary_does_not_coerce_invalid_field_types(field, value) -> None:
    with pytest.raises(ValueError, match=field):
        subtask_from_dict({"goal": "Do", "verification": ["Done"], field: value})


@pytest.mark.parametrize("payload", [None, [], "goal", {}, {"goal": "Do"}])
def test_json_boundary_rejects_nonobjects_and_missing_fields(payload) -> None:
    with pytest.raises(ValueError):
        subtask_from_dict(payload)


def test_valid_inputs_and_criteria_are_copied_without_changing_literals() -> None:
    criteria = ["Done"]
    constraints = ["Preserve contents"]
    inputs = {"text": "", "count": 2, "ratio": 0.5, "enabled": False}
    task = subtask_from_dict({"goal": "Do", "verification": criteria,
                              "constraints": constraints, "inputs": inputs})
    criteria.append("Unexpected")
    constraints.clear()
    inputs["text"] = "Changed"
    assert task.verification == ("Done",)
    assert task.constraints == ("Preserve contents",)
    assert task.compact()["inputs"] == {"text": "", "count": 2, "ratio": 0.5, "enabled": False}


def test_result_to_dict_round_trip() -> None:
    snap = DesktopSnapshot(
        application="App",
        window="Win",
        revision="r1",
        elements=(
            DesktopElement(id="e1", role="button", name="OK", actions=(ActionKind.CLICK,), source="test"),
        ),
    )
    task = Subtask(goal="Do", verification=("Done",))
    result = ExecutionResult(
        status=TerminalKind.SUBTASK_COMPLETE,
        subtask=task,
        final_snapshot=snap,
        history=(),
        observations=("Policy judged criterion observable/satisfied: Done",),
    )
    d = result_to_dict(result)
    assert d["status"] == "SUBTASK_COMPLETE"
    assert d["actions_taken"] == 0
    assert len(d["observations"]) == 1
    assert d["final_snapshot"]["application"] == "App"
    assert len(d["final_snapshot"]["elements"]) == 1
