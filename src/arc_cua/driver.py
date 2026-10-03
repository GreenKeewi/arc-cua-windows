"""A macOS driver session: observe apps and act on them in the background, no policy.

Anything can drive it: a decision model, a frontier agent, a script. It keeps one
backend per app and a journal of each app's structural changes, so an action is
checked against the app as it is when the action runs, not as it was observed:

* An action on a snapshot first looks for structural changes since that snapshot
  (a sheet, window or menu that came or went). If there are any, it does not act;
  it returns status ``"changed"`` with a fresh snapshot to decide on again.
* The target itself must still be the element that was observed (the backend's
  freshness check); otherwise the result is ``"stale"``, also with a fresh snapshot.

Nothing waits after an action by default. Use ``wait`` when the caller expects
the app to take a while to react.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from .errors import StaleDesktopState, UnsupportedDesktopAction
from .models import ActionKind, DesktopSnapshot, ExecutableAction

_TARGETED = frozenset({
    ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK, ActionKind.TYPE_TEXT,
    ActionKind.SET_VALUE, ActionKind.DRAG_TO, ActionKind.DRAG_BY,
})
_MARKER = "changes_seen"


@dataclass(frozen=True, slots=True)
class ActResult:
    status: str  # "done", "changed" (structure changed since the snapshot) or "stale" (target changed)
    snapshot: DesktopSnapshot | None = None  # A fresh observation when the action did not run.
    changes: tuple[str, ...] = ()
    elapsed_ms: float = 0.0

    @property
    def done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True, slots=True)
class Screenshot:
    png: bytes
    width: int  # Image size in pixels.
    height: int
    scale: float  # Image pixels per window point.
    window_id: int
    title: str


class _App:
    def __init__(self, pid: int) -> None:
        from .backends.macos_ax import MacOSAXBackend
        from .backends.macos_changes import ChangeJournal

        # Journal first, so nothing that happens during the first observation is missed.
        self.journal = ChangeJournal(pid)
        self.backend = MacOSAXBackend(pid)
        self.backend.open()

    def close(self) -> None:
        self.journal.close()
        self.backend.close()


class Driver:
    """Policy-free session over macOS apps. Use as a context manager, or call ``close``."""

    def __init__(self, *, app_factory: Callable[[int], Any] | None = None) -> None:
        """``app_factory`` builds the per-app backend and journal; tests pass fakes."""
        if app_factory is None and sys.platform != "darwin":
            raise RuntimeError("arc_cua.Driver controls macOS apps and needs macOS")
        self._factory = app_factory or _App
        self._apps: dict[int, Any] = {}

    def __enter__(self) -> Driver:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        for app in self._apps.values():
            app.close()
        self._apps.clear()

    def release(self, pid: int) -> None:
        """Stop working with one app and put its windows back as they were."""
        app = self._apps.pop(pid, None)
        if app is not None:
            app.close()

    def _app(self, pid: int) -> Any:
        app = self._apps.get(pid)
        if app is None:
            app = self._apps[pid] = self._factory(pid)
        return app

    # ---- reading ---------------------------------------------------------------

    @staticmethod
    def apps() -> list[dict[str, Any]]:
        """Running apps with a user interface: pid, name, bundle_id, frontmost, hidden."""
        import AppKit  # type: ignore

        workspace = AppKit.NSWorkspace.sharedWorkspace()
        front = workspace.frontmostApplication()
        front_pid = int(front.processIdentifier()) if front is not None else None
        return [
            {
                "pid": int(app.processIdentifier()),
                "name": str(app.localizedName() or ""),
                "bundle_id": str(app.bundleIdentifier() or ""),
                "frontmost": int(app.processIdentifier()) == front_pid,
                "hidden": bool(app.isHidden()),
            }
            for app in workspace.runningApplications()
            if app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular
        ]

    @staticmethod
    def windows(pid: int) -> list[Any]:
        """The app's normal windows on screen, front to back."""
        from .backends.macos_ocr import on_screen_windows

        return on_screen_windows(pid)

    def observe(self, pid: int) -> DesktopSnapshot:
        app = self._app(pid)
        # Read the journal before the walk: a change during the walk counts as after it.
        marker = app.journal.sequence
        snapshot = app.backend.observe()
        return replace(snapshot, context={**snapshot.context, _MARKER: marker})

    def wait(self, snapshot: DesktopSnapshot, *, timeout_s: float = 1.0, quiet_s: float = 0.05) -> DesktopSnapshot:
        """Observe again once the app's structure changes after ``snapshot``, or after
        ``timeout_s``. After a change, waits until ``quiet_s`` pass with no further change."""
        pid = snapshot.context["pid"]
        app = self._app(pid)
        seen = snapshot.context.get(_MARKER, app.journal.sequence)
        if app.journal.wait_after(seen, timeout_s):
            last = app.journal.sequence
            while app.journal.wait_after(last, quiet_s):
                last = app.journal.sequence
        return self.observe(pid)

    @staticmethod
    def commands(pid: int, *, query: str | None = None) -> list[Any]:
        """The app's menu bar as commands (path, shortcut, enabled, checked), read
        as it is now; ``query`` keeps commands whose path contains it."""
        from .backends.macos_menus import read_commands

        return read_commands(pid, query=query)

    # ---- acting ----------------------------------------------------------------

    def run_command(self, pid: int, path: tuple[str, ...] | list[str] | str) -> ActResult:
        """Run a menu command by path, such as ``"File > Export > PDF…"``. The menu is
        read when the command runs, so there is no snapshot to go stale."""
        from .backends.macos_menus import run_command

        started = time.perf_counter()
        app = self._app(pid)
        with app.backend.app.input_scope():
            run_command(pid, path)
        return ActResult("done", None, (), (time.perf_counter() - started) * 1000)

    def act(
        self,
        snapshot: DesktopSnapshot,
        kind: ActionKind | str,
        target: str | None = None,
        *,
        value: str | int | float | bool | None = None,
        key: str | None = None,
        hotkey: str | None = None,
        scroll_direction: str | None = None,
        click_modifier: str | None = None,
    ) -> ActResult:
        """Perform one action on an element of ``snapshot``, if the app has not changed under it."""
        started = time.perf_counter()
        kind = ActionKind(kind)
        pid = snapshot.context["pid"]
        app = self._app(pid)
        action = _action(snapshot, kind, target, value=value, key=key, hotkey=hotkey,
                         scroll_direction=scroll_direction, click_modifier=click_modifier)

        refused, snapshot = self._check(snapshot, started)
        if refused is not None:
            return refused
        try:
            app.backend.execute(snapshot, action)
        except StaleDesktopState:
            return ActResult("stale", self.observe(pid), (), (time.perf_counter() - started) * 1000)
        return ActResult("done", None, (), (time.perf_counter() - started) * 1000)


    def _check(self, snapshot: DesktopSnapshot, started: float) -> tuple[ActResult | None, DesktopSnapshot]:
        """A "changed" result when the app's structure changed under ``snapshot``;
        otherwise the snapshot to act on (a fresh one when a change was announced late)."""
        pid = snapshot.context["pid"]
        app = self._app(pid)
        changes = app.journal.since(snapshot.context.get(_MARKER, app.journal.sequence))
        if not changes:
            return None, snapshot
        fresh = self.observe(pid)
        if {e.id for e in fresh.elements} != {e.id for e in snapshot.elements}:
            return ActResult(
                "changed", fresh, tuple(f"{c.notification} {c.role}".strip() for c in changes),
                (time.perf_counter() - started) * 1000,
            ), fresh
        # Announced late: the snapshot already showed the change. Act on the fresh one,
        # which the backend's references now belong to.
        return None, fresh

    # ---- pixels and raw input --------------------------------------------------
    #
    # For what accessibility does not expose: canvases, custom-drawn controls, drags.
    # Points are relative to the top-left corner of the window, in points (not pixels);
    # a screenshot reports its scale to convert. Pass the snapshot the point was chosen
    # from to refuse the input when the app's structure changed since.

    def _window(self, pid: int, window_id: int | None) -> Any:
        """The window to address input to, on a display: an out-of-sight one is brought
        onto the invisible display first."""
        app = self._app(pid).backend.app
        if app.out_of_sight():
            app.open()
        windows = app.windows()
        if window_id is not None:
            windows = [w for w in windows if w.window_id == window_id]
        if not windows:
            app.check_running()
            raise UnsupportedDesktopAction(f"No window {window_id} of process {pid} on a display")
        return windows[0]

    def _raw(self, pid: int, snapshot: DesktopSnapshot | None, send: Callable[[], None]) -> ActResult:
        started = time.perf_counter()
        if snapshot is not None:
            refused, _ = self._check(snapshot, started)
            if refused is not None:
                return refused
        app = self._app(pid).backend.app
        with app.input_scope():
            send()
        return ActResult("done", None, (), (time.perf_counter() - started) * 1000)

    def click_at(
        self, pid: int, x: float, y: float, *, button: str = "left", count: int = 1,
        modifiers: tuple[str, ...] | list[str] = (), window_id: int | None = None,
        snapshot: DesktopSnapshot | None = None,
    ) -> ActResult:
        """Click at a point in the window: ``button`` "left" or "right", ``count`` up to 3,
        ``modifiers`` from MOD, SHIFT, ALT, CTRL."""
        from .backends import macos_background as background
        from .backends.macos_ax import modifier_flags

        if button not in ("left", "right"):
            raise UnsupportedDesktopAction(f"Unsupported button {button!r}; use left or right")
        window = self._window(pid, window_id)
        point = (window.bounds.x + x, window.bounds.y + y)
        flags = modifier_flags(tuple(modifiers)) if modifiers else 0
        return self._raw(pid, snapshot, lambda: background.click(
            pid, window.window_id, point, count=count, right=button == "right", flags=flags,
        ))

    def drag(
        self, pid: int, points: list[tuple[float, float]] | list[list[float]], *, window_id: int | None = None,
        snapshot: DesktopSnapshot | None = None,
    ) -> ActResult:
        """Press at the first point, move through the rest and release at the last."""
        from .backends import macos_background as background

        window = self._window(pid, window_id)
        path = [(window.bounds.x + float(px), window.bounds.y + float(py)) for px, py in points]
        return self._raw(pid, snapshot, lambda: background.drag_path(pid, window.window_id, path))

    def scroll_at(
        self, pid: int, x: float, y: float, *, dx: float = 0, dy: float = 0, window_id: int | None = None,
        snapshot: DesktopSnapshot | None = None,
    ) -> ActResult:
        """Scroll the content under a point by ``dx``/``dy`` points; positive ``dy``
        moves the content down (shows what is above), as a scroll wheel turned up does."""
        from .backends import macos_background as background

        window = self._window(pid, window_id)
        point = (window.bounds.x + x, window.bounds.y + y)
        return self._raw(pid, snapshot, lambda: background.scroll(
            pid, window.window_id, point, dx=round(dx), dy=round(dy),
        ))

    def press(self, pid: int, keys: str, *, snapshot: DesktopSnapshot | None = None) -> ActResult:
        """Press one key (ENTER, TAB, ARROW_DOWN...) or a chord (MOD+S) in the app's key window."""
        from .backends.macos_ax import _press_hotkey, _press_key

        self._window(pid, None)
        app = self._app(pid).backend.app
        send = (lambda: _press_hotkey(app, keys)) if "+" in keys else (lambda: _press_key(app, keys))
        return self._raw(pid, snapshot, send)

    def type_text(self, pid: int, text: str, *, snapshot: DesktopSnapshot | None = None) -> ActResult:
        """Type text as key events into whatever has key focus in the app."""
        self._window(pid, None)
        app = self._app(pid).backend.app
        return self._raw(pid, snapshot, lambda: app.type_text(text))

    def screenshot(self, pid: int, *, window_id: int | None = None, max_side: int = 1568) -> Screenshot:
        """PNG of the app's window. ``scale`` is image pixels per window point: divide
        a pixel position by it to get the point to pass to ``click_at``."""
        from .backends.macos_ocr import _capture_window, _frameworks, png_image

        window = self._window(pid, window_id)
        quartz = _frameworks()[0]
        image = _capture_window(quartz, window.window_id)
        if image is None:
            raise UnsupportedDesktopAction("The window could not be captured; is Screen Recording allowed?")
        png = png_image(image, max_side=max_side)
        width = quartz.CGImageGetWidth(image)
        height = quartz.CGImageGetHeight(image)
        shrink = min(1.0, max_side / max(width, height, 1))
        return Screenshot(
            png=png,
            width=max(1, round(width * shrink)),
            height=max(1, round(height * shrink)),
            scale=width * shrink / max(window.bounds.width, 1),
            window_id=window.window_id,
            title=window.title,
        )

def _action(
    snapshot: DesktopSnapshot,
    kind: ActionKind,
    target: str | None,
    **fields: Any,
) -> ExecutableAction:
    element = None
    if kind in _TARGETED:
        if not target:
            raise UnsupportedDesktopAction(f"{kind.value} needs a target element id")
        try:
            element = snapshot.element(target)
        except KeyError as exc:
            raise UnsupportedDesktopAction(f"No element {target!r} in this snapshot") from exc
        if kind not in element.actions:
            raise UnsupportedDesktopAction(f"{kind.value} is not offered for {target} ({element.role})")
        if kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE} and fields.get("value") is None:
            raise UnsupportedDesktopAction(f"{kind.value} needs a value")
    return ExecutableAction(
        kind=kind,
        target_id=element.id if element else None,
        target_guard=element.semantic_guard() if element else None,
        **fields,
    )
