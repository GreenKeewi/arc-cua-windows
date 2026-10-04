from __future__ import annotations

import threading
import time

import pytest

from arc_cua.backends.macos_changes import Change
from arc_cua.driver import Driver, WindowTarget
from arc_cua.errors import Cancelled, StaleDesktopState, TargetUnavailable, UnsupportedDesktopAction
from arc_cua.models import ActionKind, Bounds, DesktopElement, DesktopSnapshot

PID = 4242
A, B = 11, 22  # Two windows of the app.


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


class Desktop:
    """What the fake app shows: which windows exist, which is focused, a sheet on A."""

    def __init__(self) -> None:
        self.windows = {A: "Form A", B: "Form B"}
        self.focused = A
        self.sheet = False
        self.executed: list[tuple[int, ActionKind, str | None]] = []


class FakeBackend:
    """One window's backend: a Submit button, and Close when a sheet is shown on A."""

    def __init__(self, desktop: Desktop) -> None:
        self.desktop = desktop
        self.stale = False
        self.observed: int | None = None

    def observe(self, window_id: int) -> DesktopSnapshot:
        if window_id not in self.desktop.windows:
            raise TargetUnavailable(f"window {window_id} is gone")
        self.observed = window_id
        prefix = "a" if window_id == A else "b"
        elements = [DesktopElement(id=f"{prefix}_submit", role="Button", name="Submit", actions=(ActionKind.CLICK,))]
        if window_id == A and self.desktop.sheet:
            elements.append(DesktopElement(id="a_close", role="Button", name="Close", actions=(ActionKind.CLICK,)))
        return DesktopSnapshot(
            application="Form", window=self.desktop.windows[window_id], revision=str(self.desktop.sheet),
            elements=tuple(elements), context={"pid": PID, "window_id": window_id},
        )

    def execute(self, snapshot: DesktopSnapshot, action) -> None:
        if self.stale:
            raise StaleDesktopState("target changed")
        # A backend acts on the elements of its own last observation.
        assert self.observed == snapshot.context["window_id"]
        self.desktop.executed.append((snapshot.context["window_id"], action.kind, action.target_id))


class FakeMacOSApp:
    """Records raw input, with the window each input was addressed to."""

    def __init__(self, desktop: Desktop) -> None:
        self.desktop = desktop
        self.inputs: list[tuple] = []
        self.parked: list[int] = []
        self.name = "Form"
        self.embeds_chromium = False

    def exists(self, window_id: int) -> bool:
        return window_id in self.desktop.windows

    def on_display(self, window_id: int) -> bool:
        return window_id in self.desktop.windows

    def open(self, window_id: int | None = None) -> None:
        self.parked.append(window_id)

    def window(self, window_id: int):
        if window_id not in self.desktop.windows:
            raise TargetUnavailable(f"window {window_id} is gone")
        x = 100 if window_id == A else 500
        return type("W", (), {"window_id": window_id, "title": self.desktop.windows[window_id],
                              "bounds": Bounds(x, 50, 300, 200)})()

    def click(self, bounds, *, count=1, right=False, flags=0, window_id=None):
        self.inputs.append(("click", window_id, bounds.x, bounds.y))

    def type_text(self, text, *, window_id=None):
        self.inputs.append(("type", window_id, text))

    def input_scope(self):
        from contextlib import nullcontext

        return nullcontext()


class FakeApp:
    def __init__(self, pid: int) -> None:
        self.desktop = Desktop()
        self.journal = FakeJournal()
        self.app = FakeMacOSApp(self.desktop)
        self.backends: dict[int, FakeBackend] = {}
        self.resolved = 0
        self.closed = False

    def resolve_window(self) -> int:
        self.resolved += 1
        return self.desktop.focused

    def backend(self, window_id: int) -> FakeBackend:
        return self.backends.setdefault(window_id, FakeBackend(self.desktop))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def driver():
    with Driver(app_factory=FakeApp) as session:
        yield session


def app(driver: Driver) -> FakeApp:
    return driver._app(PID)


# ---- the single-window guarantees, unchanged --------------------------------------

def test_act_runs_when_nothing_changed(driver):
    snapshot = driver.observe(PID)
    result = driver.act(snapshot, "CLICK", "a_submit")
    assert result.done and result.snapshot is None
    assert app(driver).desktop.executed == [(A, ActionKind.CLICK, "a_submit")]


def test_act_refuses_when_a_sheet_appeared_after_the_snapshot(driver):
    snapshot = driver.observe(PID)
    app(driver).desktop.sheet = True
    app(driver).journal.add("AXSheetCreated")

    result = driver.act(snapshot, "CLICK", "a_submit")

    assert result.status == "changed"
    assert result.changes == ("AXSheetCreated AXSheet",)
    assert {e.id for e in result.snapshot.elements} == {"a_submit", "a_close"}
    assert app(driver).desktop.executed == []


def test_act_proceeds_when_the_snapshot_already_showed_the_change(driver):
    app(driver).desktop.sheet = True
    snapshot = driver.observe(PID)
    app(driver).journal.add("AXSheetCreated")  # announced after the observation

    result = driver.act(snapshot, "CLICK", "a_close")

    assert result.done
    assert app(driver).desktop.executed == [(A, ActionKind.CLICK, "a_close")]


def test_changes_before_the_snapshot_do_not_count(driver):
    app(driver).journal.add("AXSheetCreated")
    snapshot = driver.observe(PID)
    assert driver.act(snapshot, "CLICK", "a_submit").done


def test_stale_target_returns_a_fresh_snapshot(driver):
    snapshot = driver.observe(PID)
    app(driver).backend(A).stale = True
    result = driver.act(snapshot, "CLICK", "a_submit")
    assert result.status == "stale" and result.snapshot is not None


def test_actions_must_be_offered_by_the_target(driver):
    snapshot = driver.observe(PID)
    with pytest.raises(UnsupportedDesktopAction):
        driver.act(snapshot, "SET_VALUE", "a_submit", value="x")
    with pytest.raises(UnsupportedDesktopAction):
        driver.act(snapshot, "CLICK", "missing")
    with pytest.raises(UnsupportedDesktopAction):
        driver.act(snapshot, "CLICK")


def test_wait_returns_soon_after_a_structural_change(driver):
    snapshot = driver.observe(PID)

    def later() -> None:
        time.sleep(0.05)
        app(driver).desktop.sheet = True
        app(driver).journal.add("AXSheetCreated")

    threading.Thread(target=later).start()
    started = time.monotonic()
    fresh = driver.wait(snapshot, timeout_s=2.0, quiet_s=0.02)
    assert time.monotonic() - started < 0.5
    assert "a_close" in {e.id for e in fresh.elements}


def test_wait_gives_up_after_its_timeout(driver):
    snapshot = driver.observe(PID)
    started = time.monotonic()
    driver.wait(snapshot, timeout_s=0.05)
    assert 0.04 < time.monotonic() - started < 0.5


# ---- exact window targeting ---------------------------------------------------------

def test_a_pid_resolves_once_and_the_snapshot_names_its_window(driver):
    snapshot = driver.observe(PID)
    assert driver.target_of(snapshot) == WindowTarget(PID, A)
    assert app(driver).resolved == 1
    # Acting does not resolve the app's window again.
    app(driver).desktop.focused = B
    driver.act(snapshot, "CLICK", "a_submit")
    assert app(driver).resolved == 1


def test_an_exact_window_is_observed_whatever_has_focus(driver):
    snapshot = driver.observe(WindowTarget(PID, B))
    assert snapshot.window == "Form B" and driver.target_of(snapshot) == WindowTarget(PID, B)
    assert app(driver).resolved == 0


def test_focus_moving_to_another_window_refuses_with_a_fresh_snapshot_of_the_same_window(driver):
    snapshot = driver.observe(PID)  # Window A has focus.
    app(driver).desktop.focused = B
    app(driver).journal.add("AXFocusedWindowChanged", "AXWindow")
    app(driver).desktop.sheet = True  # A changed meanwhile, so the refusal stands.

    result = driver.act(snapshot, "CLICK", "a_submit")

    assert result.status == "changed"
    assert driver.target_of(result.snapshot) == WindowTarget(PID, A)
    assert app(driver).desktop.executed == []


def test_when_the_other_window_takes_focus_unannounced_the_action_still_goes_to_the_observed_one(driver):
    snapshot = driver.observe(PID)
    app(driver).desktop.focused = B  # e.g. the stacking order changed with no notification
    assert driver.act(snapshot, "CLICK", "a_submit").done
    assert app(driver).desktop.executed == [(A, ActionKind.CLICK, "a_submit")]


def test_observing_another_window_keeps_the_first_snapshot_actionable(driver):
    first = driver.observe(WindowTarget(PID, A))
    driver.observe(WindowTarget(PID, B))
    assert driver.act(first, "CLICK", "a_submit").done
    assert app(driver).desktop.executed == [(A, ActionKind.CLICK, "a_submit")]


def test_a_window_that_is_gone_is_reported_not_replaced(driver):
    snapshot = driver.observe(PID)
    del app(driver).desktop.windows[A]  # Closed in the background: nothing is announced.
    with pytest.raises(TargetUnavailable, match="gone"):
        driver.act(snapshot, "CLICK", "a_submit")
    assert app(driver).desktop.executed == []


def test_raw_input_with_a_snapshot_goes_to_the_snapshot_window(driver):
    snapshot = driver.observe(PID)  # Window A, at x=100.
    app(driver).desktop.focused = B
    driver.click_at(PID, 10, 20, snapshot=snapshot)
    driver.type_text(PID, "hi", snapshot=snapshot)
    assert app(driver).app.inputs == [("click", A, 110, 70), ("type", A, "hi")]


def test_raw_input_without_a_snapshot_resolves_the_window_once(driver):
    app(driver).desktop.focused = B
    driver.click_at(PID, 10, 20)
    assert app(driver).app.inputs == [("click", B, 510, 70)]
    driver.click_at(WindowTarget(PID, A), 1, 2)
    assert app(driver).app.inputs[-1] == ("click", A, 101, 52)


def test_raw_input_refuses_a_target_that_contradicts_the_snapshot(driver):
    snapshot = driver.observe(WindowTarget(PID, A))
    with pytest.raises(UnsupportedDesktopAction, match="snapshot is of window"):
        driver.click_at(WindowTarget(PID, B), 1, 1, snapshot=snapshot)
    assert app(driver).app.inputs == []


def test_raw_input_is_refused_when_the_snapshot_window_changed(driver):
    snapshot = driver.observe(PID)
    app(driver).desktop.sheet = True
    app(driver).journal.add("AXSheetCreated")
    result = driver.click_at(PID, 10, 20, snapshot=snapshot)
    assert result.status == "changed" and app(driver).app.inputs == []


# ---- releasing apps ---------------------------------------------------------------


def test_release_puts_one_app_back_and_forgets_it(driver):
    first = app(driver)
    driver.observe(PID)
    assert driver.release(PID) is True
    assert first.closed
    assert driver.release(PID) is False
    assert app(driver) is not first  # Working with it again starts afresh.


def test_release_all_releases_every_app(driver):
    apps = [driver._app(pid) for pid in (PID, PID + 1)]
    assert driver.release_all() == [PID, PID + 1]
    assert all(a.closed for a in apps)
    assert driver.release_all() == []


def test_parked_reports_windows_moved_out_of_sight(driver):
    assert driver.parked(PID) is False  # An app the driver never touched.
    app(driver).app.parked.append(A)
    assert driver.parked(PID) is True
    driver.release(PID)
    assert driver.parked(PID) is False


# ---- cancelling -------------------------------------------------------------------


def test_a_cancelled_wait_returns_early(driver):
    snapshot = driver.observe(PID)
    threading.Timer(0.05, driver.cancelled.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        driver.wait(snapshot, timeout_s=5)
    assert time.monotonic() - started < 0.5
    assert not driver.cancelled.is_set()  # Spent; the next call runs.
    assert driver.act(snapshot, "CLICK", "a_submit").done


def test_a_cancelled_action_is_not_performed(driver):
    snapshot = driver.observe(PID)
    driver.cancelled.set()
    with pytest.raises(Cancelled):
        driver.act(snapshot, "CLICK", "a_submit")
    driver.cancelled.set()
    with pytest.raises(Cancelled):
        driver.type_text(PID, "hi", snapshot=snapshot)
    assert app(driver).desktop.executed == [] and app(driver).app.inputs == []


# ---- errors -----------------------------------------------------------------------


def test_permission_messages_name_the_app_that_started_arc_cua(monkeypatch):
    from arc_cua.backends import macos_permissions
    from arc_cua.backends.macos_ax import MacOSAXBackend

    monkeypatch.setattr(macos_permissions, "accessibility_trusted", lambda: False)
    with pytest.raises(PermissionError) as raised:
        MacOSAXBackend._require_accessibility()
    assert "the app that started arc-cua" in str(raised.value)
    assert "terminal" not in str(raised.value)


def test_an_app_that_cannot_be_opened_stops_its_journal(monkeypatch):
    from arc_cua import driver as driver_module
    from arc_cua.backends import macos_app, macos_ax, macos_changes

    journals = []

    class Journal:
        def __init__(self, pid):
            self.closed = False
            journals.append(self)

        def close(self):
            self.closed = True

    def refuse(*args, **kwargs):
        raise PermissionError("no access")

    monkeypatch.setattr(macos_changes, "ChangeJournal", Journal)
    monkeypatch.setattr(macos_app, "MacOSApp", lambda pid: object())
    monkeypatch.setattr(macos_ax, "MacOSAXBackend", refuse)
    with pytest.raises(PermissionError):
        driver_module._App(PID)
    assert journals and journals[0].closed


# ---- status -----------------------------------------------------------------------


def test_status_reports_permissions_and_capabilities(monkeypatch):
    from arc_cua.backends import macos_background, macos_permissions
    from arc_cua.backends.macos_parking import VirtualDisplay

    def unavailable():
        raise macos_background.BackgroundInputUnavailable("missing")

    monkeypatch.setattr(macos_permissions, "accessibility_trusted", lambda: False)
    monkeypatch.setattr(macos_permissions, "screen_recording_allowed", lambda: True)
    monkeypatch.setattr(macos_background, "ensure_available", unavailable)
    monkeypatch.setattr(VirtualDisplay, "supported", staticmethod(lambda: True))
    status = Driver.status()
    assert status["permissions"] == {"accessibility": False, "screen_recording": True}
    assert status["background_input"] is False and status["virtual_display"] is True
    assert status["version"] and status["python"].count(".") == 2


# ---- hints ------------------------------------------------------------------------


def test_a_chromium_app_showing_no_web_content_is_told_how_to_turn_it_on(driver):
    assert "hint" not in driver.observe(PID).context  # Not built on Chromium.
    app(driver).app.embeds_chromium = True
    hint = driver.observe(PID).context["hint"]
    assert hint["code"] == "relaunch_for_accessibility"
    assert hint["args"] == ["--force-renderer-accessibility"] and "Form" in hint["message"]


def test_a_chromium_app_showing_its_web_content_gets_no_hint(driver, monkeypatch):
    app(driver).app.embeds_chromium = True
    observe = FakeBackend.observe

    def with_page(self, window_id):
        snapshot = observe(self, window_id)
        page = DesktopElement(id="page", role="WebArea", name="Player")
        return DesktopSnapshot(application=snapshot.application, window=snapshot.window, revision=snapshot.revision,
                               elements=(*snapshot.elements, page), context=snapshot.context)

    monkeypatch.setattr(FakeBackend, "observe", with_page)
    assert "hint" not in driver.observe(PID).context


def test_chromium_apps_are_recognised_by_their_bundled_framework(tmp_path):
    from arc_cua.backends.macos_app import MacOSApp

    def app_at(framework):
        bundle = tmp_path / framework / "App.app"
        if framework:
            (bundle / "Contents" / "Frameworks" / framework).mkdir(parents=True)
        found = MacOSApp.__new__(MacOSApp)
        found.bundle_path = str(bundle)
        return found.embeds_chromium

    assert app_at("Chromium Embedded Framework.framework")
    assert app_at("Electron Framework.framework")
    assert not app_at("")
