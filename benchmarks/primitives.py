"""Benchmark of arc's macOS driver on real apps and a native fixture.

Each scenario runs through arc's driver session in-process and through
``arc-cua mcp`` over stdio, the way an MCP client uses it. Success is checked by
oracles outside arc: the fixture form writes its own state to a file, and
Calculator's display is read with a separate accessibility call. Every action
also checks that the user's front app and pointer did not change.

    python benchmarks/run.py primitives [--quick] [--only SCENARIO ...]

Results print as Markdown and are saved to output/benchmarks/ with the arc version
and commit, so runs of different versions can be compared (benchmarks/run.py compare).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import AppKit  # noqa: E402
import ApplicationServices as AS  # noqa: E402
import common  # noqa: E402
import Quartz  # noqa: E402
from mcp_client import MCPClient, MCPError  # noqa: E402

from arc_cua.backends import MacOSAXBackend, MacOSHybridBackend  # noqa: E402
from arc_cua.backends.macos_ocr import on_screen_windows  # noqa: E402
from arc_cua.driver import Driver  # noqa: E402
from arc_cua.models import ActionKind, ExecutableAction  # noqa: E402
from arc_cua.runtime import DesktopExecutor  # noqa: E402

OUT = ROOT / "output" / "benchmarks"
FIXTURE = Path(__file__).resolve().parent / "fixture_form.py"
FINDER_DIR = OUT / "finder2000"


# ---- ground truth, outside arc ------------------------------------------------------

def front_app() -> int:
    return int(AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier())


def pointer() -> tuple[float, float]:
    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return (round(location.x, 1), round(location.y, 1))


def _ax(element: Any, name: str) -> Any:
    error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def calculator_display(pid: int) -> str | None:
    """Text of Calculator's result field, read straight from accessibility."""
    stack = [AS.AXUIElementCreateApplication(pid)]
    seen = 0
    while stack and seen < 400:
        element = stack.pop()
        seen += 1
        if _ax(element, "AXRole") == "AXScrollArea" and _ax(element, "AXDescription") == "Edit field":
            for child in _ax(element, "AXChildren") or ():
                value = _ax(child, "AXValue")
                if value is not None:
                    return str(value)
        stack.extend(_ax(element, "AXChildren") or ())
    return None


def press_raw(pid: int, title: str) -> None:
    """Press a button with a separate accessibility client, behind the driver's back."""
    stack = [AS.AXUIElementCreateApplication(pid)]
    while stack:
        element = stack.pop()
        if _ax(element, "AXRole") == "AXButton" and _ax(element, "AXTitle") == title:
            AS.AXUIElementPerformAction(element, "AXPress")
            return
        stack.extend(_ax(element, "AXChildren") or ())
    raise LookupError(f"no button {title!r}")


def wait_for(predicate: Callable[[], bool], timeout_s: float = 5.0) -> float | None:
    """Milliseconds until predicate holds, polling every millisecond; None on timeout."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout_s:
        if predicate():
            return (time.perf_counter() - started) * 1000
        time.sleep(0.001)
    return None


class EffectWatcher:
    """Polls a predicate on a thread from the moment it is created, so an effect is
    timed from the start of the action even when the driver returns later."""

    def __init__(self, predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
        self._seen: float | None = None
        self._started = time.perf_counter()

        def poll() -> None:
            while time.perf_counter() - self._started < timeout_s:
                if predicate():
                    self._seen = (time.perf_counter() - self._started) * 1000
                    return
                time.sleep(0.001)

        self._thread = threading.Thread(target=poll, daemon=True)
        self._thread.start()

    def result(self, timeout_s: float) -> float | None:
        self._thread.join(timeout_s)
        return self._seen


def seconds_since_physical_mouse() -> float:
    """Seconds since the last event from a real mouse or trackpad (not synthetic input)."""
    state = Quartz.kCGEventSourceStateHIDSystemState
    return min(
        Quartz.CGEventSourceSecondsSinceLastEventType(state, kind)
        for kind in (
            Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGEventLeftMouseDown, Quartz.kCGEventScrollWheel,
        )
    )


class Invariants:
    """Counts actions that moved the user's pointer or changed their front app.

    A pointer that moved while the user was touching their mouse is set aside as
    user input rather than blamed on the driver."""

    def __init__(self) -> None:
        self.checked = 0
        self.violations: list[str] = []
        self.user_input = 0

    def around(self, label: str, action: Callable[[], Any]) -> Any:
        front, cursor = front_app(), pointer()
        started = time.perf_counter()
        result = action()
        self.checked += 1
        after_front, after_cursor = front_app(), pointer()
        if after_front != front:
            self.violations.append(f"{label}: front app changed")
        if after_cursor != cursor:
            if seconds_since_physical_mouse() <= time.perf_counter() - started + 0.05:
                self.user_input += 1
            else:
                self.violations.append(f"{label}: pointer moved")
        return result


# ---- targets -----------------------------------------------------------------------

class Fixture:
    def __init__(self, rows: int = 0, start: str = "shown", *, second_window: bool = False) -> None:
        self.state_path = OUT / f"form-{rows}-{start}.json"
        self.state_path.unlink(missing_ok=True)
        args = [sys.executable, str(FIXTURE), str(self.state_path), "--start", start]
        if second_window:
            args.append("--second-window")
        if rows:
            args += ["--rows", str(rows)]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait_for(self.state_path.exists, 10) is None:
            raise RuntimeError("fixture form did not start")
        if start == "shown":
            wait_for(lambda: bool(on_screen_windows(self.pid)), 5)
        elif wait_for(lambda: self.state().get(start) is True, 5) is None:
            raise RuntimeError(f"fixture form did not become {start}")

    @property
    def pid(self) -> int:
        return self.process.pid

    def state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def close(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=5)


def running_pid(bundle_id: str, *, launch: list[str] | None = None, settle_s: float = 1.5) -> int:
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    if not apps:
        subprocess.run(launch or ["open", "-g", "-b", bundle_id], check=True)
        wait_for(lambda: bool(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)), 10)
        time.sleep(settle_s)
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    return int(apps[0].processIdentifier())


def borrowed_app(bundle_id: str, settle_s: float = 4.0):
    """A target app; quit afterwards only if this benchmark launched it."""
    def make():
        was_running = bool(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id))
        pid = running_pid(bundle_id, settle_s=settle_s)

        def cleanup() -> None:
            if not was_running:
                app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is not None:
                    app.terminate()
        return pid, cleanup
    return make


def finder_folder(count: int = 2000) -> int:
    FINDER_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        path = FINDER_DIR / f"file_{index:05d}.txt"
        if not path.exists():
            path.touch()
    subprocess.run(["open", "-g", str(FINDER_DIR)], check=True)
    time.sleep(1.5)
    return running_pid("com.apple.finder")


# ---- drivers -----------------------------------------------------------------------

@dataclass
class Observation:
    elements: int
    size: int
    raw: Any
    window: str = ""


class StaleScreen(RuntimeError):
    """arc refused an action because the app changed since the observation."""

    def __init__(self, result: Any) -> None:
        super().__init__(f"arc refused: {result.status} {', '.join(result.changes)}")
        self.result = result


class ArcDriver:
    def __init__(self, *, session: bool = True) -> None:
        """``session`` uses arc's driver session (with its change journal); without
        it, actions go straight to the backend as the runtime's executor does."""
        self.name = "arc" if session else "arc (backend only)"
        self.session = Driver() if session else None
        self.backends: dict[tuple[int, bool], Any] = {}

    def _backend(self, pid: int, screenshot: bool = False) -> Any:
        key = (pid, screenshot)
        if key not in self.backends:
            if screenshot:
                self.backends[key] = MacOSHybridBackend(pid, ocr="never", capture_screenshots=True)
            else:
                self.backends[key] = MacOSAXBackend(pid)
            # Opening lets arc reach minimized windows and hidden apps.
            self.backends[key].open()
        return self.backends[key]

    def forget(self, pid: int) -> None:
        for key in [key for key in self.backends if key[0] == pid]:
            try:
                self.backends.pop(key).close()
            except Exception:
                pass
        if self.session is not None:
            self.session.release(pid)

    def windows(self, pid: int) -> int:
        return len(on_screen_windows(pid))

    def observe(self, pid: int, screenshot: bool = False) -> Observation:
        if self.session is not None and not screenshot:
            snapshot = self.session.observe(pid)
        else:
            snapshot = self._backend(pid, screenshot).observe()
        size = len(json.dumps(snapshot.compact()))
        if screenshot:
            png = snapshot.screenshot() if snapshot.screenshot else None
            size += len(png or b"") * 4 // 3  # as base64, the way a tool would return it
        return Observation(len(snapshot.elements), size, snapshot, snapshot.window)

    def find(self, observation: Observation, role: str, name: str) -> Any:
        for element in observation.raw.elements:
            if element.role == role and (element.name == name or str(element.value) == name):
                return element
        raise LookupError(f"arc: no {role} {name!r}")

    def _act(self, pid: int, observation: Observation, element: Any, kind: ActionKind, value: Any = None) -> None:
        if self.session is not None:
            result = self.session.act(observation.raw, kind, element.id, value=value)
            if not result.done:
                raise StaleScreen(result)
            return
        action = ExecutableAction(kind=kind, target_id=element.id, target_guard=element.semantic_guard(), value=value)
        self._backend(pid).execute(observation.raw, action)

    def click(self, pid: int, observation: Observation, element: Any) -> None:
        self._act(pid, observation, element, ActionKind.CLICK)

    def set_text(self, pid: int, observation: Observation, element: Any, text: str) -> None:
        self._act(pid, observation, element, ActionKind.SET_VALUE, text)

    def type_text(self, pid: int, observation: Observation, element: Any, text: str) -> None:
        self._act(pid, observation, element, ActionKind.TYPE_TEXT, text)

    def click_and_settle(self, pid: int, observation: Observation, element: Any) -> Observation:
        """Click, then wait before the next observation: with the session, until the
        app's structure changes (up to 1 s); otherwise the way arc's runtime settles."""
        if self.session is not None:
            self.click(pid, observation, element)
            snapshot = self.session.wait(observation.raw, timeout_s=1.0)
            return Observation(len(snapshot.elements), len(json.dumps(snapshot.compact())), snapshot, snapshot.window)
        backend = self._backend(pid)
        executor = DesktopExecutor(backend, policy=None)  # type: ignore[arg-type]
        action = ExecutableAction(kind=ActionKind.CLICK, target_id=element.id, target_guard=element.semantic_guard())
        probe = executor._probe()
        backend.execute(observation.raw, action)
        snapshot = executor._observe_after_action(before=observation.raw, action=action, before_probe=probe)
        return Observation(len(snapshot.elements), len(json.dumps(snapshot.compact())), snapshot, snapshot.window)

    def click_settled(self, pid: int, observation: Observation, element: Any) -> Observation:
        """Click with ``settle=True``: the session waits for the app to react and go quiet."""
        assert self.session is not None
        result = self.session.act(observation.raw, ActionKind.CLICK, element.id, settle=True)
        if not result.done:
            raise StaleScreen(result)
        snapshot = result.snapshot
        return Observation(len(snapshot.elements), len(json.dumps(snapshot.compact())), snapshot, snapshot.window)

    def shows(self, observation: Observation, text: str) -> bool:
        return any(text in (e.name, str(e.value)) for e in observation.raw.elements)

    def run_menu(self, pid: int, path: tuple[str, ...]) -> None:
        assert self.session is not None
        self.session.run_command(pid, path)

    def choose(self, pid: int, observation: Observation, popup: Any, option: str) -> None:
        if ActionKind.SET_VALUE in popup.actions:
            self._act(pid, observation, popup, ActionKind.SET_VALUE, option)
            return
        # Open the menu, then pick the item from a fresh observation.
        self.click(pid, observation, popup)
        menu: Observation | None = None

        def item_visible() -> bool:
            nonlocal menu
            menu = self.observe(pid)
            return any(e.role == "MenuItem" and e.name == option for e in menu.raw.elements)

        if wait_for(item_visible, 2) is None:
            raise LookupError("arc: popup menu did not open")
        assert menu is not None
        self.click(pid, menu, self.find(menu, "MenuItem", option))

    def close(self) -> None:
        for pid in {key[0] for key in self.backends}:
            self.forget(pid)
        if self.session is not None:
            self.session.close()


class ArcMCPDriver:
    """arc through ``arc-cua mcp``, over stdio as an MCP client uses it."""

    name = "arc (MCP)"

    def __init__(self) -> None:
        self.client = MCPClient([sys.executable, "-m", "arc_cua", "mcp"])

    def forget(self, pid: int) -> None:
        pass

    def windows(self, pid: int) -> int:
        content, _, _ = self.client.call("windows", pid=pid)
        return len(content.get("windows", []))

    def observe(self, pid: int, screenshot: bool = False) -> Observation:
        content, size, _ = self.client.call("observe", pid=pid, screenshot=screenshot)
        return Observation(len(content.get("elements", [])), size, content, str(content.get("window", "")))

    def find(self, observation: Observation, role: str, name: str) -> Any:
        for element in observation.raw.get("elements", []):
            if element.get("role") == role and name in (element.get("name"), str(element.get("value"))):
                return element
        raise LookupError(f"arc (MCP): no {role} {name!r}")

    def _act(self, observation: Observation, element: Any, action: str, **fields: Any) -> dict[str, Any]:
        content, _, _ = self.client.call(
            "act", snapshot=observation.raw["snapshot"], action=action, element=element["id"], **fields,
        )
        if content.get("status") != "done":
            raise StaleScreen(SimpleNamespace(status=content.get("status"), changes=content.get("changes", [])))
        return content

    def click(self, pid: int, observation: Observation, element: Any) -> None:
        self._act(observation, element, "CLICK")

    def set_text(self, pid: int, observation: Observation, element: Any, text: str) -> None:
        self._act(observation, element, "SET_VALUE", value=text)

    def type_text(self, pid: int, observation: Observation, element: Any, text: str) -> None:
        self._act(observation, element, "TYPE_TEXT", value=text)

    def click_and_settle(self, pid: int, observation: Observation, element: Any) -> Observation:
        self.click(pid, observation, element)
        content, size, _ = self.client.call("wait", snapshot=observation.raw["snapshot"], timeout_s=1.0)
        return Observation(len(content.get("elements", [])), size, content, str(content.get("window", "")))

    def click_settled(self, pid: int, observation: Observation, element: Any) -> Observation:
        content = self._act(observation, element, "CLICK", settle=True)
        fresh = content["fresh"]
        return Observation(len(fresh.get("elements", [])), len(json.dumps(fresh)), fresh, str(fresh.get("window", "")))

    def shows(self, observation: Observation, text: str) -> bool:
        return any(text in (e.get("name"), str(e.get("value"))) for e in observation.raw.get("elements", []))

    def choose(self, pid: int, observation: Observation, popup: Any, option: str) -> None:
        if "SET_VALUE" in popup.get("actions", []):
            self.set_text(pid, observation, popup, option)
            return
        self.click(pid, observation, popup)
        menu: Observation | None = None

        def item_visible() -> bool:
            nonlocal menu
            menu = self.observe(pid)
            return any(e.get("role") == "MenuItem" and e.get("name") == option for e in menu.raw.get("elements", []))

        if wait_for(item_visible, 2) is None:
            raise LookupError("arc (MCP): popup menu did not open")
        assert menu is not None
        self.click(pid, menu, self.find(menu, "MenuItem", option))

    def run_menu(self, pid: int, path: tuple[str, ...]) -> None:
        self.client.call("run_command", pid=pid, path=list(path))

    def close(self) -> None:
        self.client.close()


# ---- measurement -------------------------------------------------------------------

@dataclass
class Series:
    samples: list[float] = field(default_factory=list)
    first: float | None = None

    def add(self, value: float) -> None:
        if self.first is None:
            self.first = value
        else:
            self.samples.append(value)

    def summary(self) -> dict[str, Any]:
        values = sorted(self.samples)
        if not values:
            return {"first_ms": self.first}
        p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
        return {
            "first_ms": round(self.first, 1) if self.first is not None else None,
            "median_ms": round(statistics.median(values), 1),
            "p90_ms": round(p90, 1),
            "n": len(values),
        }


def _elements(observation: Observation) -> list[Any]:
    raw = observation.raw
    return list(raw.get("elements", [])) if isinstance(raw, dict) else list(raw.elements)


def timed(action: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    result = action()
    return (time.perf_counter() - started) * 1000, result


# ---- scenarios ---------------------------------------------------------------------

def scenario_windows(drivers, reps, results) -> None:
    pid = running_pid("com.apple.calculator")
    for driver in drivers:
        series = Series()
        for _ in range(reps):
            elapsed, _ = timed(lambda: driver.windows(pid))
            series.add(elapsed)
        results.append({"scenario": "list windows", "target": "Calculator", "driver": driver.name, **series.summary()})


def scenario_observe(drivers, reps, results, *, targets, screenshot: bool) -> None:
    label = "observe + screenshot" if screenshot else "observe"
    for target_name, pid_factory in targets:
        pid, cleanup = pid_factory()
        try:
            for driver in drivers:
                driver.forget(pid)
                series = Series()
                observation = None
                error = None
                for _ in range(reps):
                    try:
                        elapsed, observation = timed(lambda: driver.observe(pid, screenshot))
                    except (MCPError, LookupError, RuntimeError) as exc:
                        error = str(exc)[:160]
                        break
                    series.add(elapsed)
                row = {"scenario": label, "target": target_name, "driver": driver.name, **series.summary()}
                if observation is not None:
                    row.update(elements=observation.elements, kb=round(observation.size / 1024, 1))
                    row["window"] = observation.window[:40]
                if error:
                    row["error"] = error
                results.append(row)
        finally:
            cleanup()


def scenario_click(drivers, reps, results, invariants) -> None:
    pid = running_pid("com.apple.calculator")
    for driver in drivers:
        driver.forget(pid)
        call, effect, turn = Series(), Series(), Series()
        failures = 0
        for index in range(reps):
            observation = driver.observe(pid)
            if index % 5 == 0:
                labels = {
                    e.get("name") if isinstance(e, dict) else e.name for e in _elements(observation)
                }
                clear = driver.find(observation, "Button", "Clear" if "Clear" in labels else "All Clear")
                invariants.around(f"{driver.name} clear", lambda: driver.click(pid, observation, clear))
                wait_for(lambda: (calculator_display(pid) or "").strip("‎") == "0", 2)
                observation = driver.observe(pid)
            before = calculator_display(pid)
            seven = driver.find(observation, "Button", "7")
            watcher = EffectWatcher(lambda: calculator_display(pid) != before)
            def click_seven() -> None:
                driver.click(pid, observation, seven)

            elapsed, _ = timed(lambda: invariants.around(f"{driver.name} click", click_seven))
            seen = watcher.result(timeout_s=3)
            if seen is None:
                failures += 1
                continue
            call.add(elapsed)
            effect.add(seen)

            # One agent turn: observe, act, observe again.
            def agent_turn() -> None:
                current = driver.observe(pid)
                driver.click(pid, current, driver.find(current, "Button", "7"))
                driver.observe(pid)

            elapsed, _ = timed(lambda: invariants.around(f"{driver.name} turn", agent_turn))
            turn.add(elapsed)
        for scenario, target, series in (
            ("click (call returns)", "Calculator 7", call),
            ("click (effect visible)", "Calculator 7", effect),
            ("observe→click→observe", "Calculator", turn),
        ):
            results.append({"scenario": scenario, "target": target, "driver": driver.name, **series.summary()})
        results[-3]["failures"] = failures


EXPECTED = {"name": "Ada Lovelace", "email": "ada@example.com", "subscribe": True, "plan": "Pro", "submitted": 1}


def scenario_form(drivers, reps, results, invariants) -> None:
    for driver in drivers:
        series = Series()
        failures: list[str] = []
        for _ in range(reps):
            fixture = Fixture()
            pid = fixture.pid
            try:
                def fill() -> None:
                    observation = driver.observe(pid)
                    name = driver.find(observation, "TextField", "Full name")
                    driver.set_text(pid, observation, name, EXPECTED["name"])
                    email = driver.find(observation, "TextField", "Email")
                    driver.set_text(pid, observation, email, EXPECTED["email"])
                    driver.click(pid, observation, driver.find(observation, "CheckBox", "Subscribe"))
                    driver.choose(pid, observation, driver.find(observation, "PopUpButton", "Plan"), EXPECTED["plan"])
                    current = driver.observe(pid)
                    driver.click(pid, current, driver.find(current, "Button", "Submit"))

                started = time.perf_counter()
                try:
                    invariants.around(f"{driver.name} form", fill)
                except (MCPError, LookupError, RuntimeError) as exc:
                    failures.append(str(exc)[:160])
                    continue
                done = wait_for(lambda: {k: fixture.state().get(k) for k in EXPECTED} == EXPECTED, 5)
                if done is None:
                    failures.append(f"final state {fixture.state()}")
                    continue
                series.add((time.perf_counter() - started) * 1000)
            finally:
                driver.forget(pid)
                fixture.close()
        row = {"scenario": "fill form (5 fields + submit)", "target": "fixture", "driver": driver.name}
        row.update(series.summary())
        row["success"] = f"{reps - len(failures)}/{reps}"
        if failures:
            row["error"] = failures[0]
        results.append(row)


TYPED = "The quick brown fox jumps over the lazy dog, 0123456789. " * 3 + "Pack my box with five dozen jugs."


def scenario_type(drivers, reps, results, invariants) -> None:
    for driver in drivers:
        series = Series()
        failures: list[str] = []
        fixture = Fixture()
        try:
            for index in range(reps):
                text = f"{index}: {TYPED}"
                observation = driver.observe(fixture.pid)
                field = driver.find(observation, "TextField", "Full name")
                # Start from an empty field: drivers differ on whether typing replaces or inserts.
                driver.set_text(fixture.pid, observation, field, "")
                wait_for(lambda: fixture.state().get("name") == "", 2)
                observation = driver.observe(fixture.pid)
                field = driver.find(observation, "TextField", "Full name")
                watcher = EffectWatcher(lambda: fixture.state().get("name") == text)
                try:
                    def type_name() -> None:
                        driver.type_text(fixture.pid, observation, field, text)

                    invariants.around(f"{driver.name} type", type_name)
                except (MCPError, LookupError, RuntimeError) as exc:
                    failures.append(str(exc)[:160])
                    continue
                seen = watcher.result(timeout_s=5)
                if seen is None:
                    failures.append(f"field holds {fixture.state().get('name')!r:.60}")
                    continue
                series.add(seen)
        finally:
            driver.forget(fixture.pid)
            fixture.close()
        row = {"scenario": f"type {len(TYPED)}+ chars (effect visible)", "target": "fixture", "driver": driver.name}
        row.update(series.summary())
        row["success"] = f"{reps - len(failures)}/{reps}"
        if failures:
            row["error"] = failures[0]
        results.append(row)


def scenario_out_of_sight(drivers, reps, results, invariants) -> None:
    """Observe and click in a window the user can't see, starting from a fresh session."""
    for start in ("minimized", "hidden"):
        for driver in drivers:
            series = Series()
            failures: list[str] = []
            restored = 0
            for _ in range(reps):
                fixture = Fixture(start=start)
                pid = fixture.pid
                try:
                    def act() -> None:
                        observation = driver.observe(pid)
                        driver.click(pid, observation, driver.find(observation, "CheckBox", "Subscribe"))

                    started = time.perf_counter()
                    try:
                        invariants.around(f"{driver.name} {start}", act)
                    except (MCPError, LookupError, RuntimeError) as exc:
                        failures.append(str(exc)[:160])
                        continue
                    if wait_for(lambda: fixture.state().get("subscribe") is True, 5) is None:
                        failures.append("checkbox did not change")
                        continue
                    series.add((time.perf_counter() - started) * 1000)
                finally:
                    driver.forget(pid)
                    # The user should find the window as they left it.
                    if wait_for(lambda: fixture.state().get(start) is True, 2) is not None:
                        restored += 1
                    fixture.close()
            row = {"scenario": f"{start} window: observe + click", "target": "fixture", "driver": driver.name}
            row.update(series.summary())
            row["success"] = f"{reps - len(failures)}/{reps}, left {start} {restored}/{reps}"
            if failures:
                row["error"] = failures[0]
            results.append(row)


def scenario_settle(drivers, reps, results, invariants) -> None:
    """After an action, does the driver's next observation show what the action caused?

    Each button opens a sheet after a delay; the subscribe checkbox changes at once.
    Time runs from the click until the next observation is in hand, including
    whatever waiting the driver does to let the UI settle."""
    cases = [("checkbox (changes at once)", "Subscribe", None)] + [
        (f"sheet after {delay} ms", f"Open {delay} ms", delay) for delay in (0, 300, 800, 1500)
    ]
    # Each driver as it waits by default, and arc's sessions also with settle: true.
    runs = [(driver, driver.name, driver.click_and_settle) for driver in drivers] + [
        (driver, f"{driver.name}, settle: true", driver.click_settled)
        for driver in drivers if getattr(driver, "session", True) is not None and hasattr(driver, "click_settled")
    ]
    for label, button, delay in cases:
        for driver, name, click_and_settle in runs:
            fixture = Fixture()
            pid = fixture.pid
            series, appeared = Series(), Series()
            caught = 0
            try:
                for _ in range(reps):
                    observation = driver.observe(pid)
                    before = fixture.state().get("subscribe")
                    if delay is None:
                        watcher = EffectWatcher(lambda: fixture.state().get("subscribe") != before)
                    else:
                        watcher = EffectWatcher(lambda: fixture.state().get("dialog") is True)
                    target = driver.find(observation, "CheckBox" if delay is None else "Button", button)
                    started = time.perf_counter()
                    after = invariants.around(
                        f"{name} settle", lambda: click_and_settle(pid, observation, target),
                    )
                    series.add((time.perf_counter() - started) * 1000)
                    seen = watcher.result(timeout_s=4)
                    if seen is not None:
                        appeared.add(seen)
                    if delay is None:
                        expected = 1 if not before else 0
                        wanted = (str(expected), str(bool(expected)))
                        shown = any(
                            (e.name if not isinstance(e, dict) else e.get("name")) == "Subscribe"
                            and str(e.value if not isinstance(e, dict) else e.get("value")) in wanted
                            for e in _elements(after)
                        )
                    else:
                        shown = driver.shows(after, "Dialog open")
                        # Close it for the next round, outside the timing.
                        wait_for(lambda: fixture.state().get("dialog") is True, 4)
                        current = driver.observe(pid)
                        driver.click(pid, current, driver.find(current, "Button", "Close"))
                        wait_for(lambda: fixture.state().get("dialog") is False, 3)
                        # The app announces the sheet closing a few hundred ms later.
                        time.sleep(0.5)
                    caught += shown
            except (MCPError, LookupError, RuntimeError) as exc:
                results.append({"scenario": "act, settle, observe", "target": label, "driver": name,
                                "error": str(exc)[:160]})
                continue
            finally:
                driver.forget(pid)
                fixture.close()
            row = {"scenario": "act, settle, observe", "target": label, "driver": name}
            row.update(series.summary())
            appeared_at = appeared.summary().get("median_ms")
            row["success"] = f"next observation shows it {caught}/{reps}" + (
                f"; it appeared at ~{appeared_at} ms" if appeared_at is not None else ""
            )
            results.append(row)


def scenario_stale(drivers, reps, results, invariants) -> None:
    """The app changes after the driver's observation: a sheet opens over the form.
    The driver is then asked to press Submit using the observation from before.

    Pressing Submit under a sheet acts on a screen the agent never saw. The right
    outcome is to refuse and hand back a fresh observation."""
    variants = list(drivers) + [ArcDriver(session=False)]
    try:
        for driver in variants:
            outcomes: dict[str, int] = {}
            series = Series()
            for _ in range(reps):
                fixture = Fixture()
                pid = fixture.pid
                try:
                    observation = driver.observe(pid)
                    submit = driver.find(observation, "Button", "Submit")
                    press_raw(pid, "Open 0 ms")
                    if wait_for(lambda: fixture.state().get("dialog") is True, 3) is None:
                        raise RuntimeError("the sheet did not open")
                    time.sleep(0.05)
                    started = time.perf_counter()
                    try:
                        driver.click(pid, observation, submit)
                        outcome = None
                    except StaleScreen:
                        outcome = "refused, fresh observation returned"
                    except MCPError as exc:
                        outcome = f"error: {str(exc)[:80]}"
                    series.add((time.perf_counter() - started) * 1000)
                    if outcome is None:
                        submitted = wait_for(lambda: fixture.state().get("submitted", 0) > 0, 1) is not None
                        outcome = "pressed Submit under the sheet" if submitted else "acted; the app ignored it"
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                finally:
                    driver.forget(pid)
                    fixture.close()
            row = {"scenario": "act on a screen that changed", "target": "sheet over form", "driver": driver.name}
            row.update(series.summary())
            row["success"] = "; ".join(f"{name} {count}/{reps}" for name, count in outcomes.items())
            results.append(row)
    finally:
        variants[-1].close()


def scenario_menu(drivers, reps, results, invariants) -> None:
    """Run a menu command (and one in a submenu) in a background app."""
    for start in ("shown", "hidden"):
        for driver in drivers:
            call, effect = Series(), Series()
            failures: list[str] = []
            fixture = Fixture(start=start)
            pid = fixture.pid
            try:
                for _ in range(reps):
                    before = fixture.state().get("counter", 0)
                    watcher = EffectWatcher(lambda: fixture.state().get("counter", 0) == before + 1)
                    try:
                        elapsed, _ = timed(lambda: invariants.around(
                            f"{driver.name} menu", lambda: driver.run_menu(pid, ("Bench", "Increment")),
                        ))
                    except (MCPError, LookupError, RuntimeError) as exc:
                        failures.append(str(exc)[:160])
                        continue
                    seen = watcher.result(timeout_s=3)
                    if seen is None:
                        failures.append("the counter did not change")
                        continue
                    call.add(elapsed)
                    effect.add(seen)
                    driver.run_menu(pid, ("Bench", "More", "Reset Counter"))
                    if wait_for(lambda: fixture.state().get("counter") == 0, 3) is None:
                        failures.append("the submenu command did not run")
            finally:
                driver.forget(pid)
                fixture.close()
            target = "fixture" if start == "shown" else f"fixture ({start})"
            for scenario, series in (("menu command (call returns)", call), ("menu command (effect visible)", effect)):
                row = {"scenario": scenario, "target": target, "driver": driver.name}
                row.update(series.summary())
                row["success"] = f"{reps - len(failures)}/{reps}"
                if failures:
                    row["error"] = failures[0]
                results.append(row)


# ---- report ------------------------------------------------------------------------

def markdown(results: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        f"arc {meta.get('arc_version')} @ {meta.get('commit')} · macOS {meta['macos']} · "
        f"{meta.get('chip') or meta['machine']} · {meta['reps']} repetitions",
        "",
        "| scenario | target | driver | median ms | p90 ms | first ms | elements | KB | notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in results:
        notes = []
        if row.get("success"):
            notes.append(f"success {row['success']}")
        if row.get("failures"):
            notes.append(f"{row['failures']} failed")
        if row.get("error"):
            notes.append(row["error"])
        if row.get("window"):
            notes.append(f"window “{row['window']}”")
        lines.append(
            f"| {row['scenario']} | {row['target']} | {row['driver']} | {row.get('median_ms', '–')} | "
            f"{row.get('p90_ms', '–')} | {row.get('first_ms', '–')} | {row.get('elements', '')} | "
            f"{row.get('kb', '')} | {'; '.join(notes)} |"
        )
    violations = meta["violations"] or "none"
    lines += [
        "",
        f"Background checks: {meta['invariants_checked']} actions, violations: {violations}, "
        f"set aside for user mouse input: {meta['user_input']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(prog="benchmarks/run.py primitives")
    parser.add_argument("--quick", action="store_true", help="fewer repetitions")
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    reps = 5 if args.quick else 20

    drivers: list[Any] = [ArcDriver(), ArcMCPDriver()]

    invariants = Invariants()
    results: list[dict[str, Any]] = []

    def fixture_target(rows: int = 0):
        def make():
            fixture = Fixture(rows)
            return fixture.pid, fixture.close
        return make

    observe_targets = [
        ("Calculator", lambda: (running_pid("com.apple.calculator"), lambda: None)),
        ("fixture form", fixture_target()),
        ("fixture + 2,000-row table", fixture_target(2000)),
        ("Finder, 2,000 files", lambda: (finder_folder(), lambda: None)),
        ("System Settings", lambda: (running_pid("com.apple.systempreferences"), lambda: None)),
        ("Obsidian (Electron)", borrowed_app("md.obsidian")),
    ]
    scenarios = {
        "windows": lambda: scenario_windows(drivers, reps, results),
        "observe": lambda: scenario_observe(drivers, reps, results, targets=observe_targets, screenshot=False),
        "screenshot": lambda: scenario_observe(
            drivers, max(3, reps // 2), results, targets=observe_targets[:2], screenshot=True,
        ),
        "click": lambda: scenario_click(drivers, max(5, reps // 2), results, invariants),
        "form": lambda: scenario_form(drivers, max(3, reps // 4), results, invariants),
        "type": lambda: scenario_type(drivers, max(3, reps // 4), results, invariants),
        "out-of-sight": lambda: scenario_out_of_sight(drivers, max(3, reps // 4), results, invariants),
        "settle": lambda: scenario_settle(drivers, max(3, reps // 4), results, invariants),
        "stale": lambda: scenario_stale(drivers, max(3, reps // 4), results, invariants),
        "menu": lambda: scenario_menu(drivers, max(3, reps // 2), results, invariants),
    }
    try:
        for name, run in scenarios.items():
            if args.only and name not in args.only:
                continue
            print(f"running {name}...", file=sys.stderr)
            run()
    finally:
        for driver in drivers:
            driver.close()

    run_meta = common.meta(
        suite="primitives",
        invariants_checked=invariants.checked,
        violations=invariants.violations,
        user_input=invariants.user_input,
        reps=reps,
    )
    report = markdown(results, run_meta)
    print(report)
    path = common.save("primitives", results, run_meta, report)
    print(f"saved {path}", file=sys.stderr)
    return path


if __name__ == "__main__":
    main()
