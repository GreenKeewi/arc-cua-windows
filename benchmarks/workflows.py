"""Realistic multi-step workflows on macOS, timed end to end.

Each workflow sets up its own world (an app instance opened on temporary files, a
fixture app, a web page), carries out a fixed sequence of steps through an Agent,
and checks the outcome directly: the file on disk, the page's own record, the
fixture's state file, or a separate accessibility read. The check never goes
through the driver being measured.

Nothing touches the user's documents or settings: real apps are started as their
own instance with window restoration off, on files in a temporary folder, and quit
afterwards; Finder only ever acts in a window on a temporary folder.

The Agent is the driver-facing side: find a control by its accessibility label,
click it, type into it, set it, choose from it, press keys, run a menu command.
ArcAgent drives arc in-process; ArcMCPAgent drives `arc-cua mcp` over stdio.

    python benchmarks/run.py workflows [--reps N] [--only NAME ...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import common  # noqa: E402

LOOKS = 8  # Observations before a label counts as missing; from the third, scroll down in between.
LOOK_PAUSE_S = 0.1


class StepFailed(RuntimeError):
    pass


def _same(label: str | None, wanted: str) -> bool:
    return bool(label) and label.strip().casefold() == wanted.strip().casefold()


# ---- agents -----------------------------------------------------------------------

class Agent:
    """Finds controls by label and acts on them. Subclasses implement the driver calls."""

    name = "agent"

    def attach(self, pid: int, title: str | None = None) -> None:
        """Work in the app's window titled ``title``, else its focused window."""
        raise NotImplementedError

    # Driver calls, on elements as {"id", "role", "name", "value", "actions"}.
    def elements(self) -> list[dict]:
        raise NotImplementedError

    def act(self, element: dict, action: str, **fields: Any) -> str:
        raise NotImplementedError

    def scroll(self, direction: str = "DOWN") -> None:
        raise NotImplementedError

    def press(self, keys: str) -> None:
        raise NotImplementedError

    def type_text(self, text: str) -> None:
        raise NotImplementedError

    def menu(self, path: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # Steps, the same for every driver.
    def find(self, label: str, roles: tuple[str, ...] = (), *, scroll: bool = True) -> dict:
        """The element labeled ``label``; else one showing it as its value (a file name
        in a Finder list is the value of its cell)."""
        for attempt in range(LOOKS):
            elements = [e for e in self.elements() if not roles or e.get("role") in roles]
            for key in ("name", "value"):
                for element in elements:
                    if _same(str(element.get(key) or ""), label):
                        return element
            if scroll and attempt >= 2:
                self.scroll("DOWN")
            else:
                time.sleep(LOOK_PAUSE_S)
        raise StepFailed(f"no {'/'.join(roles) or 'element'} labeled {label!r}")

    def _do(self, label: str, roles: tuple[str, ...], action: str, **fields: Any) -> None:
        # Once more on a fresh observation if the app changed under the first one.
        for _ in range(2):
            status = self.act(self.find(label, roles), action, **fields)
            if status == "done":
                return
        raise StepFailed(f"{action} {label!r}: {status}")

    def click(self, label: str, roles: tuple[str, ...] = ()) -> None:
        self._do(label, roles, "CLICK")

    def type_into(self, label: str, text: str, roles: tuple[str, ...] = ("TextField", "TextArea")) -> None:
        self._do(label, roles, "TYPE_TEXT", value=text)

    def set_value(self, label: str, value: Any, roles: tuple[str, ...] = ()) -> None:
        self._do(label, roles, "SET_VALUE", value=value)

    def choose(self, label: str, option: str) -> None:
        """A popup or dropdown: open it and pick the option."""
        self.click(label, ("PopUpButton", "ComboBox"))
        self.click(option, ("MenuItem",))

    def wait_for(self, label: str, roles: tuple[str, ...] = (), timeout_s: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for element in self.elements():
                if _same(element.get("name"), label) and (not roles or element.get("role") in roles):
                    return element
            time.sleep(0.03)
        raise StepFailed(f"{label!r} did not appear")


class ArcAgent(Agent):
    name = "arc"

    def __init__(self) -> None:
        from arc_cua import Driver

        self.driver = Driver()
        self.snapshot = None

    def attach(self, pid: int, title: str | None = None) -> None:
        from arc_cua import WindowTarget

        if title is None:
            self.target = self.driver.target(pid)
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for window in self.driver.windows(pid):
                if window.title == title:
                    self.target = WindowTarget(pid, window.window_id)
                    return
            time.sleep(0.1)
        raise StepFailed(f"no window titled {title!r}")

    def elements(self) -> list[dict]:
        self.snapshot = self.driver.observe(self.target)
        return [
            {"id": e.id, "role": e.role, "name": e.name, "value": e.value, "actions": [a.value for a in e.actions]}
            for e in self.snapshot.elements
        ]

    def act(self, element: dict, action: str, **fields: Any) -> str:
        return self.driver.act(self.snapshot, action, element["id"], **fields).status

    def scroll(self, direction: str = "DOWN") -> None:
        self.driver.act(self.snapshot, "SCROLL", scroll_direction=direction)

    def press(self, keys: str) -> None:
        self.driver.press(self.target, keys)

    def type_text(self, text: str) -> None:
        self.driver.type_text(self.target, text)

    def menu(self, path: str) -> None:
        self.driver.run_command(self.target.pid, path)

    def release(self, pid: int) -> None:
        self.driver.release(pid)

    def close(self) -> None:
        self.driver.close()


class ArcMCPAgent(Agent):
    """arc through `arc-cua mcp`, over stdio as an MCP client uses it."""

    name = "arc (MCP)"

    def __init__(self) -> None:
        from mcp_client import MCPClient

        self.client = MCPClient([sys.executable, "-m", "arc_cua", "mcp"])
        self.snapshot: dict = {}

    def attach(self, pid: int, title: str | None = None) -> None:
        self.pid, self.window_id = pid, None
        if title is None:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            listed, _, _ = self.client.call("windows", pid=pid)
            for window in listed["windows"]:
                if window["title"] == title:
                    self.window_id = window["window_id"]
                    return
            time.sleep(0.1)
        raise StepFailed(f"no window titled {title!r}")

    def _where(self) -> dict:
        return {"pid": self.pid, **({"window_id": self.window_id} if self.window_id else {})}

    def elements(self) -> list[dict]:
        self.snapshot, _, _ = self.client.call("observe", **self._where())
        if self.window_id is None:
            self.window_id = self.snapshot.get("window_id")
        return self.snapshot["elements"]

    def act(self, element: dict, action: str, **fields: Any) -> str:
        result, _, _ = self.client.call("act", snapshot=self.snapshot["snapshot"], action=action,
                                        element=element["id"], **fields)
        return result["status"]

    def scroll(self, direction: str = "DOWN") -> None:
        self.client.call("act", snapshot=self.snapshot["snapshot"], action="SCROLL", direction=direction)

    def press(self, keys: str) -> None:
        self.client.call("press", keys=keys, **self._where())

    def type_text(self, text: str) -> None:
        self.client.call("type_text", text=text, **self._where())

    def menu(self, path: str) -> None:
        self.client.call("run_command", pid=self.pid, path=path)

    def close(self) -> None:
        self.client.close()


# ---- isolated apps and fixtures -----------------------------------------------------

def launch_isolated(bundle_id: str, *files: str, wait_s: float = 8.0):
    """Start a new instance of an app with window restoration off, so it opens only
    ``files`` and none of the user's documents. Returns its NSRunningApplication."""
    import AppKit  # type: ignore

    def running() -> list:
        return list(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id) or [])

    before = {int(app.processIdentifier()) for app in running()}
    subprocess.run(["open", "-n", "-g", "-b", bundle_id, *files, "--args", "-ApplePersistenceIgnoreState", "YES"],
                   check=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        new = [app for app in running() if int(app.processIdentifier()) not in before]
        if new and _has_window(int(new[0].processIdentifier())):
            return new[0]
        time.sleep(0.1)
    raise RuntimeError(f"{bundle_id} did not open a window")


def _has_window(pid: int) -> bool:
    from arc_cua.backends.macos_ocr import on_screen_windows

    return bool(on_screen_windows(pid))


def quit_app(app, timeout_s: float = 5.0) -> None:
    app.terminate()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not app.isTerminated():
        time.sleep(0.1)
    if not app.isTerminated():
        app.forceTerminate()


def _ax(element: Any, name: str) -> Any:
    import ApplicationServices as AS  # type: ignore

    error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def calculator_display(pid: int) -> str:
    """Calculator's result, read with a separate accessibility call."""
    import ApplicationServices as AS  # type: ignore

    stack = [AS.AXUIElementCreateApplication(pid)]
    while stack:
        element = stack.pop()
        if _ax(element, "AXRole") == "AXScrollArea" and _ax(element, "AXDescription") == "Edit field":
            for child in _ax(element, "AXChildren") or ():
                if _ax(child, "AXValue") is not None:
                    return str(_ax(child, "AXValue")).strip("‎")
        stack.extend(_ax(element, "AXChildren") or ())
    return ""


class FixtureApp:
    """The native fixture form (fixture_form.py), with its state file as ground truth."""

    def __init__(self, start: str = "shown", second_window: bool = False) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="arc-bench-"))
        self.state_path = self.folder / "state.json"
        args = [sys.executable, str(HERE / "fixture_form.py"), str(self.state_path), "--start", start]
        if second_window:
            args.append("--second-window")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.state()
            if state and (start == "shown" and _has_window(self.pid) or state.get(start) is True):
                return
            time.sleep(0.05)
        raise RuntimeError("the fixture app did not start")

    @property
    def pid(self) -> int:
        return self.process.pid

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.folder, ignore_errors=True)


class WebPage:
    """One of pages/*.html in a native WKWebView window (web_host.py)."""

    def __init__(self, page: str, title: str, width: int = 520, height: int = 520) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="arc-bench-"))
        self.sock = str(self.folder / "eval.sock")
        self.process = subprocess.Popen(
            [sys.executable, str(HERE / "web_host.py"), str(HERE / "pages" / page), title, str(width), str(height),
             self.sock],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if os.path.exists(self.sock) and self.js("document.readyState") == "complete":
                return
            time.sleep(0.05)
        raise RuntimeError(f"{page} did not load")

    @property
    def pid(self) -> int:
        return self.process.pid

    def js(self, script: str) -> Any:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(self.sock)
                stream = connection.makefile("rw")
                stream.write(json.dumps({"js": script}) + "\n")
                stream.flush()
                reply = json.loads(stream.readline())
        except OSError:
            return None
        return reply.get("value")

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.folder, ignore_errors=True)


# ---- workflows --------------------------------------------------------------------

@dataclass
class Workflow:
    name: str
    target: str  # The app, as shown in reports.
    setup: Callable[[], dict]  # Returns a context for the steps and the check.
    steps: Callable[[Agent, dict], None]
    check: Callable[[dict], str | None]  # None when the outcome is right, else what is wrong.
    cleanup: Callable[[dict], None]
    description: str = ""
    step_count: int = 0


def _calculator_setup() -> dict:
    app = launch_isolated("com.apple.calculator")
    return {"app": app, "pid": int(app.processIdentifier())}


def _calculator_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"])
    # (12 + 30) × 4. Calculator applies precedence, so the sum is closed with = first.
    for key in ("1", "2", "Add", "3", "0", "Equals", "Multiply", "4", "Equals"):
        agent.click(key, ("Button",))


def _textedit_setup() -> dict:
    folder = Path(tempfile.mkdtemp(prefix="arc-bench-"))
    document = folder / "notes.txt"
    document.write_text("draft\n")
    app = launch_isolated("com.apple.TextEdit", str(document))
    return {"app": app, "pid": int(app.processIdentifier()), "folder": folder, "document": document}


_NOTES = "Weekly notes. Revenue grew twelve percent. Next review on Monday."


def _textedit_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"], "notes.txt")
    area = next(e for e in agent.elements() if e.get("role") == "TextArea")
    if agent.act(area, "TYPE_TEXT", value=_NOTES) != "done":
        raise StepFailed("typing into the document was refused")
    agent.menu("File > Save")


def _textedit_check(ctx: dict) -> str | None:
    text = ctx["document"].read_text().strip()
    return None if text == _NOTES else f"file holds {text[:60]!r}"


def _quit_and_remove(ctx: dict) -> None:
    quit_app(ctx["app"])
    if ctx.get("folder"):
        shutil.rmtree(ctx["folder"], ignore_errors=True)


def _finder_setup() -> dict:
    folder = Path(tempfile.mkdtemp(prefix="arc-bench-"))
    nested = folder / "Projects" / "2026"
    nested.mkdir(parents=True)
    for name in ("budget.txt", "report.txt", "roadmap.txt"):
        (nested / name).write_text(name + "\n")
    (folder / "Projects" / "notes.txt").write_text("notes\n")
    subprocess.run(["open", "-g", str(folder)], check=True)
    import AppKit  # type: ignore

    finder = int(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")[0]
                 .processIdentifier())
    return {"pid": finder, "folder": folder, "title": folder.name}


def _finder_steps(agent: Agent, ctx: dict) -> None:
    # Only reads and selects: nothing in the folder changes.
    agent.attach(ctx["pid"], ctx["title"])
    agent.act(agent.find("Projects"), "DOUBLE_CLICK")
    agent.act(agent.find("2026"), "DOUBLE_CLICK")
    agent.click("report.txt")


def _finder_window(ctx: dict, title: str) -> Any:
    import ApplicationServices as AS  # type: ignore

    for window in _ax(AS.AXUIElementCreateApplication(ctx["pid"]), "AXWindows") or ():
        if _ax(window, "AXTitle") == title:
            return window
    return None


def _finder_check(ctx: dict) -> str | None:
    window = _finder_window(ctx, "2026")
    if window is None:
        return "the window did not reach Projects/2026"
    rows, stack = [], [window]
    while stack:
        element = stack.pop()
        if _ax(element, "AXRole") == "AXRow" and _ax(element, "AXSelected"):
            texts, inner = [], [element]
            while inner:
                part = inner.pop()
                inner.extend(_ax(part, "AXChildren") or ())
                if _ax(part, "AXValue") is not None:
                    texts.append(str(_ax(part, "AXValue")))
            rows.append(texts)
        stack.extend(_ax(element, "AXChildren") or ())
    return None if any("report.txt" in texts for texts in rows) else f"selected: {rows}"


def _finder_cleanup(ctx: dict) -> None:
    import ApplicationServices as AS  # type: ignore

    # Close only the window on our folder (wherever it navigated), then remove the folder.
    for title in (ctx["title"], "Projects", "2026"):
        window = _finder_window(ctx, title)
        if window is not None and str(_ax(window, "AXDocument") or "").find(ctx["folder"].name) >= 0:
            close = _ax(window, "AXCloseButton")
            if close is not None:
                AS.AXUIElementPerformAction(close, "AXPress")
    shutil.rmtree(ctx["folder"], ignore_errors=True)


def _fixture(**options):
    def setup() -> dict:
        fixture = FixtureApp(**options)
        return {"fixture": fixture, "pid": fixture.pid}
    return setup


def _native_form_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"], "Arc Bench Form")
    agent.type_into("Full name", "Ada Lovelace")
    agent.type_into("Email", "ada@example.com")
    agent.click("Subscribe", ("CheckBox",))
    agent.choose("Plan", "Pro")
    agent.click("Open 0 ms", ("Button",))
    agent.wait_for("Close", ("Button",))
    agent.click("Close", ("Button",))
    agent.click("Submit", ("Button",))
    agent.menu("Bench > Increment")


def _native_form_check(ctx: dict) -> str | None:
    state = ctx["fixture"].state()
    wanted = {"name": "Ada Lovelace", "email": "ada@example.com", "subscribe": True, "plan": "Pro",
              "submitted": 1, "counter": 1, "dialog": False}
    wrong = {k: state.get(k) for k, v in wanted.items() if state.get(k) != v}
    return None if not wrong else f"wrong: {wrong}"


def _minimized_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"], "Arc Bench Form")
    agent.type_into("Full name", "Grace Hopper")
    agent.click("Subscribe", ("CheckBox",))
    agent.click("Submit", ("Button",))


def _minimized_check(ctx: dict) -> str | None:
    state = ctx["fixture"].state()
    if (state.get("name"), state.get("subscribe"), state.get("submitted")) != ("Grace Hopper", True, 1):
        return f"form: {state.get('name')!r}, subscribe {state.get('subscribe')}, submitted {state.get('submitted')}"
    return None if state.get("minimized") else "the window was brought out of the Dock"


def _covered_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"], "Arc Bench Form")  # Covered by "Arc Bench Form 2", at the same place.
    agent.type_into("Full name", "Katherine Johnson")
    agent.click("Subscribe", ("CheckBox",))


def _covered_check(ctx: dict) -> str | None:
    state = ctx["fixture"].state()
    if state.get("name") != "Katherine Johnson" or not state.get("subscribe"):
        return f"covered window: {state.get('name')!r}, subscribe {state.get('subscribe')}"
    if state.get("notes") or state.get("agree"):
        return "the window on top was changed"
    return None


def _close_fixture(ctx: dict) -> None:
    ctx["fixture"].close()


def _web(page: str, title: str):
    def setup() -> dict:
        web = WebPage(page, title)
        return {"web": web, "pid": web.pid}
    return setup


_SIGNUP = {"name": "Mary Jackson", "email": "mary@example.com", "team": "Engineering", "start": "2026-11-02",
           "experience": 7, "plan": "team", "updates": True}


def _signup_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"], "Team Signup")
    agent.type_into("Full name", _SIGNUP["name"])
    agent.type_into("Work email", _SIGNUP["email"])
    agent.choose("Team", _SIGNUP["team"])
    agent.set_value("Start date", _SIGNUP["start"], ("DateTimeArea",))
    agent.set_value("Years of experience", _SIGNUP["experience"], ("Slider",))
    agent.click("Team", ("RadioButton",))
    agent.click("Send me product updates", ("CheckBox",))
    agent.click("Create account", ("Button",))


def _signup_check(ctx: dict) -> str | None:
    got = ctx["web"].js("window.__submitted")
    if not got:
        return "the form was not submitted"
    wrong = {k: got.get(k) for k, v in _SIGNUP.items() if got.get(k) != v}
    return None if not wrong else f"wrong: {wrong}"


def _inventory_steps(agent: Agent, ctx: dict) -> None:
    agent.attach(ctx["pid"], "Inventory")
    agent.type_into("Count for item 45", "120")  # Below the fold: found by scrolling.
    agent.click("Save counts", ("Button",))


def _inventory_check(ctx: dict) -> str | None:
    saved = ctx["web"].js("window.__saved")
    if not saved:
        return "not saved"
    return None if saved.get("45") == "120" and saved.get("44") == "44" else f"item 45 is {saved.get('45')!r}"


def _close_web(ctx: dict) -> None:
    ctx["web"].close()


WORKFLOWS = [
    Workflow("calculate", "Calculator", _calculator_setup, _calculator_steps,
             lambda ctx: None if calculator_display(ctx["pid"]) == "168" else f"shows {calculator_display(ctx['pid'])}",
             _quit_and_remove, "(12 + 30) × 4 with Calculator's buttons", 9),
    Workflow("edit and save a document", "TextEdit", _textedit_setup, _textedit_steps, _textedit_check,
             _quit_and_remove, "Replace a text file's contents and save it with File > Save", 2),
    Workflow("open folders and select a file", "Finder", _finder_setup, _finder_steps, _finder_check,
             _finder_cleanup, "Open a folder, then one inside it, then select a file, in one Finder window", 3),
    Workflow("fill, confirm and submit a form", "fixture app", _fixture(), _native_form_steps, _native_form_check,
             _close_fixture, "Two fields, a checkbox, a popup, a sheet, submit and a menu command", 9),
    Workflow("form in a minimized window", "fixture app", _fixture(start="minimized"), _minimized_steps,
             _minimized_check, _close_fixture, "Fill and submit while the window stays in the Dock", 3),
    Workflow("form in a covered window", "fixture app", _fixture(second_window=True), _covered_steps,
             _covered_check, _close_fixture, "Act in a window another window covers exactly", 2),
    Workflow("web signup", "web page", _web("signup.html", "Team Signup"), _signup_steps, _signup_check,
             _close_web, "Text, email, dropdown, date, slider, radio, checkbox and submit", 8),
    Workflow("edit a row below the fold", "web page", _web("inventory.html", "Inventory"), _inventory_steps,
             _inventory_check, _close_web, "Scroll to a field 45 rows down, change it and save", 2),
]


# ---- runner -----------------------------------------------------------------------

@dataclass
class Outcome:
    times: common.Series = field(default_factory=common.Series)
    landed: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _front_and_pointer() -> tuple[int, tuple[float, float]]:
    import AppKit  # type: ignore
    import Quartz  # type: ignore

    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(front.processIdentifier()) if front else 0, (round(location.x, 1), round(location.y, 1))


def _physical_mouse_within(seconds: float) -> bool:
    import Quartz  # type: ignore

    state = Quartz.kCGEventSourceStateHIDSystemState
    return min(
        Quartz.CGEventSourceSecondsSinceLastEventType(state, kind)
        for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventScrollWheel)
    ) <= seconds


def run_workflow(workflow: Workflow, agent: Agent, reps: int) -> dict:
    outcome = Outcome()
    for _ in range(reps):
        ctx = workflow.setup()
        try:
            before = _front_and_pointer()
            started = time.perf_counter()
            try:
                workflow.steps(agent, ctx)
            except Exception as exc:  # A step that cannot be done fails the run.
                outcome.failures.append(f"{type(exc).__name__}: {str(exc)[:140]}")
                continue
            elapsed = (time.perf_counter() - started) * 1000
            after = _front_and_pointer()
            if after[0] != before[0]:
                outcome.violations.append("front app changed")
            if after[1] != before[1] and not _physical_mouse_within(elapsed / 1000 + 0.05):
                outcome.violations.append("pointer moved")
            # The outcome may land a moment after the last step returns; wait for it (untimed).
            ended = time.perf_counter()
            problem = workflow.check(ctx)
            while problem and time.perf_counter() - ended < 3.0:
                time.sleep(0.02)
                problem = workflow.check(ctx)
            if problem:
                outcome.failures.append(problem)
                continue
            outcome.landed.append((time.perf_counter() - ended) * 1000)
            outcome.times.add(elapsed)
        finally:
            if isinstance(agent, ArcAgent) and ctx.get("pid"):
                agent.release(ctx["pid"])
            workflow.cleanup(ctx)
    succeeded = reps - len(outcome.failures)
    row: dict[str, Any] = {
        "suite": "workflows", "scenario": workflow.name, "target": workflow.target, "driver": agent.name,
        "steps": workflow.step_count, **outcome.times.summary(), "success": f"{succeeded}/{reps}",
    }
    if outcome.landed:
        row["landed_after_ms"] = round(sorted(outcome.landed)[len(outcome.landed) // 2], 1)
    if outcome.failures:
        row["error"] = outcome.failures[0]
    if outcome.violations:
        row["violations"] = outcome.violations
    return row


def markdown(rows: list[dict], run_meta: dict) -> str:
    lines = [
        f"arc {run_meta.get('arc_version')} @ {run_meta.get('commit')} · macOS {run_meta.get('macos')} · "
        f"{run_meta.get('chip') or run_meta.get('machine')} · {run_meta.get('reps')} runs each",
        "",
        "| Workflow | App | Driver | Steps | Success | Median ms | p90 ms | First ms | Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        notes = "; ".join(filter(None, [row.get("error", ""), ", ".join(row.get("violations", []))]))
        lines.append(
            f"| {row['scenario']} | {row['target']} | {row['driver']} | {row['steps']} | {row['success']} | "
            f"{row.get('median_ms', '–')} | {row.get('p90_ms', '–')} | {row.get('first_ms', '–')} | {notes} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None, agents: list[Agent] | None = None) -> Path:
    parser = argparse.ArgumentParser(prog="benchmarks/run.py workflows")
    parser.add_argument("--reps", type=int, default=5,
                        help="runs per workflow and driver (the first is reported apart)")
    parser.add_argument("--only", nargs="*", help="workflow names, or parts of them")
    parser.add_argument("--no-mcp", action="store_true", help="only arc in-process")
    args = parser.parse_args(argv)
    if agents is None:
        agents = [ArcAgent()] + ([] if args.no_mcp else [ArcMCPAgent()])
    chosen = [w for w in WORKFLOWS if not args.only or any(part in w.name for part in args.only)]
    rows = []
    try:
        for workflow in chosen:
            for agent in agents:
                print(f"{workflow.name} · {agent.name}...", file=sys.stderr, flush=True)
                rows.append(run_workflow(workflow, agent, args.reps))
    finally:
        for agent in agents:
            agent.close()
    run_meta = common.meta(suite="workflows", reps=args.reps)
    report = markdown(rows, run_meta)
    print(report)
    path = common.save("workflows", rows, run_meta, report)
    print(f"saved {path}", file=sys.stderr)
    return path


if __name__ == "__main__":
    main()
