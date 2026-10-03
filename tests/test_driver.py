from __future__ import annotations

import threading
import time

import pytest

from arc_cua.backends.macos_changes import Change
from arc_cua.driver import Driver
from arc_cua.errors import StaleDesktopState, UnsupportedDesktopAction
from arc_cua.models import ActionKind, DesktopElement, DesktopSnapshot

PID = 4242


class FakeJournal:
    def __init__(self) -> None:
        self.sequence = 0
        self.changes: list[Change] = []

    def add(self, notification: str, role: str = "AXSheet") -> None:
        self.sequence += 1
        self.changes.append(Change(self.sequence, notification, role, time.monotonic()))

    def since(self, sequence: int) -> list[Change]:
        return [c for c in self.changes if c.sequence > sequence]

    def wait_after(self, sequence: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.sequence > sequence:
                return True
            time.sleep(0.001)
        return self.sequence > sequence

    def close(self) -> None:
        pass


class FakeBackend:
    """A form with a Submit button; a sheet can be shown over it."""

    def __init__(self) -> None:
        self.sheet = False
        self.executed: list[tuple[ActionKind, str | None]] = []
        self.stale = False

    def observe(self) -> DesktopSnapshot:
        elements = [DesktopElement(id="submit", role="Button", name="Submit", actions=(ActionKind.CLICK,))]
        if self.sheet:
            elements.append(DesktopElement(id="close", role="Button", name="Close", actions=(ActionKind.CLICK,)))
        return DesktopSnapshot(
            application="Form", window="Form", revision=str(self.sheet), elements=tuple(elements),
            context={"pid": PID},
        )

    def execute(self, snapshot: DesktopSnapshot, action) -> None:
        if self.stale:
            raise StaleDesktopState("target changed")
        self.executed.append((action.kind, action.target_id))


class FakeApp:
    def __init__(self, pid: int) -> None:
        self.journal = FakeJournal()
        self.backend = FakeBackend()

    def close(self) -> None:
        pass


@pytest.fixture
def driver():
    with Driver(app_factory=FakeApp) as session:
        yield session


def app(driver: Driver) -> FakeApp:
    return driver._app(PID)


def test_act_runs_when_nothing_changed(driver):
    snapshot = driver.observe(PID)
    result = driver.act(snapshot, "CLICK", "submit")
    assert result.done and result.snapshot is None
    assert app(driver).backend.executed == [(ActionKind.CLICK, "submit")]


def test_act_refuses_when_a_sheet_appeared_after_the_snapshot(driver):
    snapshot = driver.observe(PID)
    app(driver).backend.sheet = True
    app(driver).journal.add("AXSheetCreated")

    result = driver.act(snapshot, "CLICK", "submit")

    assert result.status == "changed"
    assert result.changes == ("AXSheetCreated AXSheet",)
    assert {e.id for e in result.snapshot.elements} == {"submit", "close"}
    assert app(driver).backend.executed == []


def test_act_proceeds_when_the_snapshot_already_showed_the_change(driver):
    app(driver).backend.sheet = True
    snapshot = driver.observe(PID)
    app(driver).journal.add("AXSheetCreated")  # announced after the observation

    result = driver.act(snapshot, "CLICK", "close")

    assert result.done
    assert app(driver).backend.executed == [(ActionKind.CLICK, "close")]


def test_changes_before_the_snapshot_do_not_count(driver):
    app(driver).journal.add("AXSheetCreated")
    snapshot = driver.observe(PID)
    assert driver.act(snapshot, "CLICK", "submit").done


def test_stale_target_returns_a_fresh_snapshot(driver):
    snapshot = driver.observe(PID)
    app(driver).backend.stale = True
    result = driver.act(snapshot, "CLICK", "submit")
    assert result.status == "stale" and result.snapshot is not None


def test_actions_must_be_offered_by_the_target(driver):
    snapshot = driver.observe(PID)
    with pytest.raises(UnsupportedDesktopAction):
        driver.act(snapshot, "SET_VALUE", "submit", value="x")
    with pytest.raises(UnsupportedDesktopAction):
        driver.act(snapshot, "CLICK", "missing")
    with pytest.raises(UnsupportedDesktopAction):
        driver.act(snapshot, "CLICK")


def test_wait_returns_soon_after_a_structural_change(driver):
    snapshot = driver.observe(PID)

    def later() -> None:
        time.sleep(0.05)
        app(driver).backend.sheet = True
        app(driver).journal.add("AXSheetCreated")

    threading.Thread(target=later).start()
    started = time.monotonic()
    fresh = driver.wait(snapshot, timeout_s=2.0, quiet_s=0.02)
    assert time.monotonic() - started < 0.5
    assert "close" in {e.id for e in fresh.elements}


def test_wait_gives_up_after_its_timeout(driver):
    snapshot = driver.observe(PID)
    started = time.monotonic()
    driver.wait(snapshot, timeout_s=0.05)
    assert 0.04 < time.monotonic() - started < 0.5
