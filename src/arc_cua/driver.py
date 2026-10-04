"""A macOS driver session: observe apps and act on them in the background, no policy.

Anything can drive it: a decision model, a frontier agent, a script. It targets
one exact window, a ``WindowTarget(pid, window_id)``: observing, screenshots and
actions all go to that window, and a snapshot carries the window it was read
from, so nothing re-resolves "the app's main window" between looking and acting.
Passing just a pid resolves it once, to the window an observation would read.

It keeps a journal of each app's structural changes, so an action is checked
against the app as it is when the action runs, not as it was observed:

* An action on a snapshot first looks for structural changes since that snapshot
  (a sheet, window or menu that came or went, focus moving to another window). If
  there are any, it does not act; it returns status ``"changed"`` with a fresh
  snapshot of the same window to decide on again.
* The target itself must still be the element that was observed (the backend's
  freshness check); otherwise the result is ``"stale"``, also with a fresh snapshot.

A window that is gone (closed, or replaced by a new one) raises TargetUnavailable;
target the app again to find its window. Nothing waits after an action by
default; use ``wait`` when the caller expects the app to take a while to react.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from .errors import StaleDesktopState, TargetUnavailable, UnsupportedDesktopAction
from .models import ActionKind, Bounds, DesktopSnapshot, ExecutableAction

_TARGETED = frozenset({
    ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK, ActionKind.TYPE_TEXT,
    ActionKind.SET_VALUE, ActionKind.DRAG_TO, ActionKind.DRAG_BY,
})
_MARKER = "changes_seen"


@dataclass(frozen=True, slots=True)
class WindowTarget:
    """One window of one app: its process ID and window-server window ID."""

    pid: int
    window_id: int


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


@dataclass(frozen=True, slots=True)
class WindowInfo:
    window_id: int
    title: str
    bounds: Bounds | None
    on_screen: bool
    minimized: bool


class _App:
    """One app: its journal, its MacOSApp (shared, so window parking is shared) and a
    backend per window, since a backend acts on the elements of its last observation."""

    def __init__(self, pid: int) -> None:
        from .backends.macos_app import MacOSApp
        from .backends.macos_ax import MacOSAXBackend
        from .backends.macos_changes import ChangeJournal

        # Journal first, so nothing that happens during the first observation is missed.
        self.journal = ChangeJournal(pid)
        self.app = MacOSApp(pid)
        self._backend_class = MacOSAXBackend
        self._resolver = MacOSAXBackend(pid, app=self.app)
        self._resolver.open()
        self._backends: dict[int, Any] = {}

    def resolve_window(self) -> int:
        return self._resolver.resolve_window()

    def backend(self, window_id: int) -> Any:
        backend = self._backends.get(window_id)
        if backend is None:
            backend = self._backends[window_id] = self._backend_class(self.app.pid, app=self.app)
        return backend

    def close(self) -> None:
        self.journal.close()
        for backend in (*self._backends.values(), self._resolver):
            backend.close()


Where = WindowTarget | int  # An exact window, or a pid to resolve once.


class Driver:
    """Policy-free session over macOS apps. Use as a context manager, or call ``close``."""

    def __init__(self, *, app_factory: Callable[[int], Any] | None = None) -> None:
        """``app_factory`` builds the per-app journal, MacOSApp and backends; tests pass fakes."""
        if app_factory is None and sys.platform != "darwin":
            raise RuntimeError("arc_cua.Driver controls macOS apps and needs macOS")
        self._factory = app_factory or _App
        self._apps: dict[int, Any] = {}

    def __enter__(self) -> Driver:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.release_all()

    def release(self, pid: int) -> bool:
        """Stop working with one app and put its windows back as they were. Its
        snapshots can no longer be acted on. True when the driver was working with it."""
        app = self._apps.pop(pid, None)
        if app is None:
            return False
        app.close()
        return True

    def release_all(self) -> list[int]:
        """Release every app the driver is working with; returns their pids."""
        pids = list(self._apps)
        for pid in pids:
            self.release(pid)
        return pids

    def parked(self, pid: int) -> bool:
        """Whether the app has windows moved onto the invisible display, to be put back
        by ``release``. Accessibility actions never park; input events and screenshots
        in a minimized window or a hidden app do."""
        app = self._apps.get(pid)
        return app is not None and bool(app.app.parked)

    def _app(self, pid: int) -> Any:
        app = self._apps.get(pid)
        if app is None:
            app = self._apps[pid] = self._factory(pid)
        return app

    # ---- targets ---------------------------------------------------------------

    def target(self, where: Where) -> WindowTarget:
        """The exact window to work in. A pid resolves, once, to the window an
        observation would read: the focused window when it is on screen here, else the
        main or first one, else a minimized window or a hidden app's window."""
        if isinstance(where, WindowTarget):
            return where
        return WindowTarget(where, self._app(where).resolve_window())

    @staticmethod
    def target_of(snapshot: DesktopSnapshot) -> WindowTarget:
        """The window a snapshot was read from."""
        return WindowTarget(snapshot.context["pid"], snapshot.context["window_id"])

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

    def windows(self, pid: int) -> list[WindowInfo]:
        """All of the app's windows: those on screen front to back, then minimized ones
        and a hidden app's. Any of them can be targeted by its window_id."""
        return [WindowInfo(**info) for info in self._app(pid).app.all_windows()]

    def observe(self, where: Where) -> DesktopSnapshot:
        """Read one window. The snapshot's context records its pid and window_id."""
        target = self.target(where)
        app = self._app(target.pid)
        # Read the journal before the walk: a change during the walk counts as after it.
        marker = app.journal.sequence
        snapshot = app.backend(target.window_id).observe(target.window_id)
        return replace(snapshot, context={**snapshot.context, _MARKER: marker})

    def wait(self, snapshot: DesktopSnapshot, *, timeout_s: float = 1.0, quiet_s: float = 0.05) -> DesktopSnapshot:
        """Observe the snapshot's window again once the app's structure changes after
        ``snapshot``, or after ``timeout_s``. After a change, waits until ``quiet_s``
        pass with no further change."""
        target = self.target_of(snapshot)
        app = self._app(target.pid)
        seen = snapshot.context.get(_MARKER, app.journal.sequence)
        if app.journal.wait_after(seen, timeout_s):
            last = app.journal.sequence
            while app.journal.wait_after(last, quiet_s):
                last = app.journal.sequence
        return self.observe(target)

    @staticmethod
    def commands(where: Where, *, query: str | None = None) -> list[Any]:
        """The app's menu bar as commands (path, shortcut, enabled, checked), read
        as it is now; ``query`` keeps commands whose path contains it."""
        from .backends.macos_menus import read_commands

        return read_commands(_pid(where), query=query)

    # ---- acting ----------------------------------------------------------------

    def run_command(self, where: Where, path: tuple[str, ...] | list[str] | str) -> ActResult:
        """Run a menu command by path, such as ``"File > Export > PDF…"``. The menu is
        read when the command runs, so there is no snapshot to go stale. Menus belong
        to the app: the command acts on the app's own key window."""
        from .backends.macos_menus import run_command

        started = time.perf_counter()
        pid = _pid(where)
        with self._app(pid).app.input_scope():
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
        """Perform one action on an element of ``snapshot``, in the snapshot's window,
        if the app has not changed under it."""
        started = time.perf_counter()
        kind = ActionKind(kind)
        window = self.target_of(snapshot)
        action = _action(snapshot, kind, target, value=value, key=key, hotkey=hotkey,
                         scroll_direction=scroll_direction, click_modifier=click_modifier)

        refused, snapshot = self._check(snapshot, started)
        if refused is not None:
            return refused
        try:
            self._app(window.pid).backend(window.window_id).execute(snapshot, action)
        except StaleDesktopState:
            return ActResult("stale", self.observe(window), (), (time.perf_counter() - started) * 1000)
        return ActResult("done", None, (), (time.perf_counter() - started) * 1000)

    def _check(self, snapshot: DesktopSnapshot, started: float) -> tuple[ActResult | None, DesktopSnapshot]:
        """A "changed" result when the app's structure changed under ``snapshot``;
        otherwise the snapshot to act on (a fresh one when a change was announced late)."""
        window = self.target_of(snapshot)
        app = self._app(window.pid)
        if not app.app.exists(window.window_id):
            # A window closed in the background announces nothing, and its controls can
            # still answer accessibility for a while.
            raise TargetUnavailable(
                f"Window {window.window_id} of process {window.pid} is gone (closed, or replaced by a new one); "
                "find the app's window again."
            )
        changes = app.journal.since(snapshot.context.get(_MARKER, app.journal.sequence))
        if not changes:
            return None, snapshot
        fresh = self.observe(window)
        if {e.id for e in fresh.elements} != {e.id for e in snapshot.elements}:
            return ActResult(
                "changed", fresh, tuple(f"{c.notification} {c.role}".strip() for c in changes),
                (time.perf_counter() - started) * 1000,
            ), fresh
        # Announced late: the snapshot already showed the change. Act on the fresh one,
        # which the window's backend's references now belong to.
        return None, fresh

    # ---- pixels and raw input --------------------------------------------------
    #
    # For what accessibility does not expose: canvases, custom-drawn controls, drags.
    # Points are relative to the top-left corner of the target window, in points (not
    # pixels); a screenshot reports its scale to convert. Given the snapshot a point
    # was chosen from, input goes to that snapshot's window and is refused when the
    # app's structure changed since. A sheet attached to the window takes its input.

    def _raw_target(self, where: Where, snapshot: DesktopSnapshot | None) -> WindowTarget:
        if snapshot is None:
            return self.target(where)
        mine = self.target_of(snapshot)
        if (isinstance(where, WindowTarget) and where != mine) or (isinstance(where, int) and where != mine.pid):
            raise UnsupportedDesktopAction(
                f"The snapshot is of window {mine.window_id} of process {mine.pid}, not the target given"
            )
        return mine

    def _window(self, target: WindowTarget) -> Any:
        """The target window on a display: brought onto the invisible display first
        when it is minimized or its app hidden."""
        app = self._app(target.pid).app
        if not app.exists(target.window_id):
            raise TargetUnavailable(
                f"Window {target.window_id} of process {target.pid} is gone (closed, or replaced by a new one); "
                "find the app's window again."
            )
        if not app.on_display(target.window_id):
            app.open(window_id=target.window_id)
        return app.window(target.window_id)

    def _raw(self, snapshot: DesktopSnapshot | None, send: Callable[[], None]) -> ActResult:
        started = time.perf_counter()
        if snapshot is not None:
            refused, _ = self._check(snapshot, started)
            if refused is not None:
                return refused
        send()
        return ActResult("done", None, (), (time.perf_counter() - started) * 1000)

    def click_at(
        self, where: Where, x: float, y: float, *, button: str = "left", count: int = 1,
        modifiers: tuple[str, ...] | list[str] = (), snapshot: DesktopSnapshot | None = None,
    ) -> ActResult:
        """Click at a point in the window: ``button`` "left" or "right", ``count`` up to 3,
        ``modifiers`` from MOD, SHIFT, ALT, CTRL."""
        from .backends.macos_ax import modifier_flags

        if button not in ("left", "right"):
            raise UnsupportedDesktopAction(f"Unsupported button {button!r}; use left or right")
        target = self._raw_target(where, snapshot)
        window = self._window(target)
        point = Bounds(window.bounds.x + x, window.bounds.y + y, 0, 0)
        flags = modifier_flags(tuple(modifiers)) if modifiers else 0
        app = self._app(target.pid).app
        return self._raw(snapshot, lambda: app.click(
            point, count=count, right=button == "right", flags=flags, window_id=target.window_id,
        ))

    def drag(
        self, where: Where, points: list[tuple[float, float]] | list[list[float]], *,
        snapshot: DesktopSnapshot | None = None,
    ) -> ActResult:
        """Press at the first point, move through the rest and release at the last."""
        target = self._raw_target(where, snapshot)
        window = self._window(target)
        path = [(window.bounds.x + float(px), window.bounds.y + float(py)) for px, py in points]
        if len(path) < 2:
            raise UnsupportedDesktopAction("A drag needs at least two points")
        app = self._app(target.pid).app
        return self._raw(snapshot, lambda: app.drag_path(path, window_id=target.window_id))

    def scroll_at(
        self, where: Where, x: float, y: float, *, dx: float = 0, dy: float = 0,
        snapshot: DesktopSnapshot | None = None,
    ) -> ActResult:
        """Scroll the content under a point by ``dx``/``dy`` points; positive ``dy``
        moves the content down (shows what is above), as a scroll wheel turned up does."""
        target = self._raw_target(where, snapshot)
        window = self._window(target)
        point = (window.bounds.x + x, window.bounds.y + y)
        app = self._app(target.pid).app
        return self._raw(snapshot, lambda: app.scroll_at(point, dx=round(dx), dy=round(dy), window_id=target.window_id))

    def press(self, where: Where, keys: str, *, snapshot: DesktopSnapshot | None = None) -> ActResult:
        """Press one key (ENTER, TAB, ARROW_DOWN...) or a chord (MOD+S) in the window.
        Command chords an app menu item has run through the app's menu."""
        from .backends.macos_ax import _press_hotkey, _press_key

        target = self._raw_target(where, snapshot)
        self._window(target)
        app = self._app(target.pid).app
        if "+" in keys:
            return self._raw(snapshot, lambda: _press_hotkey(app, keys, target.window_id))
        return self._raw(snapshot, lambda: _press_key(app, keys, target.window_id))

    def type_text(self, where: Where, text: str, *, snapshot: DesktopSnapshot | None = None) -> ActResult:
        """Type text as key events into the window, where it has key focus."""
        target = self._raw_target(where, snapshot)
        self._window(target)
        app = self._app(target.pid).app
        return self._raw(snapshot, lambda: app.type_text(text, window_id=target.window_id))

    def screenshot(
        self, where: Where, *, snapshot: DesktopSnapshot | None = None, max_side: int = 1568,
    ) -> Screenshot:
        """PNG of the window, with any sheet attached to it. ``scale`` is image pixels
        per window point: divide a pixel position by it to get the point for ``click_at``."""
        from .backends.macos_ocr import _capture_window, _frameworks, png_image

        target = self._raw_target(where, snapshot)
        window = self._window(target)
        quartz = _frameworks()[0]
        sheets = self._app(target.pid).app.attached_windows(target.window_id)
        if sheets:
            image = _capture_windows(quartz, [w.window_id for w in sheets] + [window.window_id], window.bounds)
        else:
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


def _pid(where: Where) -> int:
    return where.pid if isinstance(where, WindowTarget) else where


def _capture_windows(quartz: Any, window_ids: list[int], bounds: Bounds) -> Any:
    """One image of several windows (front first), cropped to ``bounds``."""
    options = quartz.kCGWindowImageBoundsIgnoreFraming
    if hasattr(quartz, "kCGWindowImageNominalResolution"):
        options |= quartz.kCGWindowImageNominalResolution
    rect = quartz.CGRectMake(bounds.x, bounds.y, bounds.width, bounds.height)
    return quartz.CGWindowListCreateImageFromArray(rect, window_ids, options)


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
