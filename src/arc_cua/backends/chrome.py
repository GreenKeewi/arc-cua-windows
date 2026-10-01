"""Chrome backend: observes pages through the DOM and acts through DevTools input.

Input goes to the page through the DevTools protocol, so the real pointer and
keyboard focus are untouched and the browser window may stay in the background.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from importlib import resources
from typing import Any, Mapping

from ..errors import StaleDesktopState, UnsupportedDesktopAction
from ..keyboard import parse_hotkey
from ..models import ActionKind, Bounds, DesktopElement, DesktopSnapshot, ExecutableAction
from .cdp import CDPConnection, CDPError, ChromeProcess, browser_ws_url

logger = logging.getLogger(__name__)

PAGE_JS = resources.files(__package__).joinpath("chrome_page.js").read_text(encoding="utf-8")

SOURCE = "chrome_dom"
DIALOG_ACCEPT = "dialog_accept"
DIALOG_DISMISS = "dialog_dismiss"

# Requests whose responses usually change the page. Settling after an action waits
# for those the action started; ones already open before it (long polling,
# streams) do not hold it up.
_TRACKED_REQUESTS = frozenset({"Document", "XHR", "Fetch"})

# DevTools modifier bits.
_ALT, _CTRL, _META, _SHIFT = 1, 2, 4, 8

# key name -> (DOM key, DOM code, Windows virtual key code, inserted text)
_KEYS: dict[str, tuple[str, str, int, str]] = {
    "ENTER": ("Enter", "Enter", 13, "\r"),
    "ESCAPE": ("Escape", "Escape", 27, ""),
    "TAB": ("Tab", "Tab", 9, ""),
    "SPACE": (" ", "Space", 32, " "),
    "BACKSPACE": ("Backspace", "Backspace", 8, ""),
    "DELETE": ("Delete", "Delete", 46, ""),
    "ARROW_LEFT": ("ArrowLeft", "ArrowLeft", 37, ""),
    "ARROW_UP": ("ArrowUp", "ArrowUp", 38, ""),
    "ARROW_RIGHT": ("ArrowRight", "ArrowRight", 39, ""),
    "ARROW_DOWN": ("ArrowDown", "ArrowDown", 40, ""),
    "HOME": ("Home", "Home", 36, ""),
    "END": ("End", "End", 35, ""),
    "PAGE_UP": ("PageUp", "PageUp", 33, ""),
    "PAGE_DOWN": ("PageDown", "PageDown", 34, ""),
    "MINUS": ("-", "Minus", 189, "-"),
    "EQUAL": ("=", "Equal", 187, "="),
    "LEFT_BRACKET": ("[", "BracketLeft", 219, "["),
    "RIGHT_BRACKET": ("]", "BracketRight", 221, "]"),
    "BACKSLASH": ("\\", "Backslash", 220, "\\"),
    "SEMICOLON": (";", "Semicolon", 186, ";"),
    "QUOTE": ("'", "Quote", 222, "'"),
    "COMMA": (",", "Comma", 188, ","),
    "PERIOD": (".", "Period", 190, "."),
    "SLASH": ("/", "Slash", 191, "/"),
    "GRAVE": ("`", "Backquote", 192, "`"),
    **{c: (c.lower(), f"Key{c}", ord(c), c.lower()) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{d: (d, f"Digit{d}", ord(d), d) for d in "0123456789"},
    **{f"F{i}": (f"F{i}", f"F{i}", 111 + i, "") for i in range(1, 21)},
}

# On macOS, Chrome maps editing shortcuts to commands in the browser process, so
# DevTools key events need the command named explicitly.
_MAC_EDIT_COMMANDS = {
    ("MOD", "A"): "selectAll",
    ("MOD", "C"): "copy",
    ("MOD", "X"): "cut",
    ("MOD", "V"): "paste",
    ("MOD", "Z"): "undo",
    ("MOD", "SHIFT", "Z"): "redo",
}


def _is_dialog_event(event: Mapping[str, Any]) -> bool:
    return event.get("method") == "Page.javascriptDialogOpening"


class ChromeBackend:
    """DesktopBackend for one Chrome tab.

    Create it with `ChromeBackend.launch()` (a new Chrome with a temporary
    profile) or `ChromeBackend.connect()` (a new tab in a Chrome started with
    --remote-debugging-port). Elements are what is visible in the viewport;
    SCROLL reveals more. Links that open a new tab switch the backend to it.
    """

    # Settling after an action may take this long while the page is still fetching
    # (see settle_probe); the runtime's own cap applies when it is longer.
    settle_timeout_s = 10.0

    def __init__(
        self,
        connection: CDPConnection,
        target_id: str,
        *,
        process: ChromeProcess | None = None,
        owns_target: bool = False,
        capture_screenshots: bool = False,
        max_interactive: int = 250,
        max_text: int = 120,
        load_timeout_s: float = 10,
    ) -> None:
        self._conn = connection
        self._conn.on_event = self._on_event
        self._process = process
        self._owns_target = owns_target
        self.capture_screenshots = capture_screenshots
        self.max_interactive = max_interactive
        self.max_text = max_text
        self.load_timeout_s = load_timeout_s
        self._dialog: dict[str, Any] | None = None
        self._opened_tab: str | None = None
        self.target_id = target_id
        self.session_id: str | None = None
        self._conn.call("Target.setDiscoverTargets", {"discover": True})
        self._attach(target_id)

    # ---- construction ------------------------------------------------------

    @classmethod
    def launch(
        cls,
        url: str = "about:blank",
        *,
        headless: bool = False,
        executable: str | None = None,
        user_data_dir: str | None = None,
        window_size: tuple[int, int] = (1280, 900),
        extra_args: tuple[str, ...] = (),
        **options: Any,
    ) -> ChromeBackend:
        process = ChromeProcess(
            executable=executable,
            headless=headless,
            user_data_dir=user_data_dir,
            window_size=window_size,
            extra_args=extra_args,
        )
        try:
            connection = CDPConnection(process.ws_url)
            pages = [t for t in connection.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
            target_id = pages[0]["targetId"] if pages else connection.call(
                "Target.createTarget", {"url": "about:blank"},
            )["targetId"]
            backend = cls(connection, target_id, process=process, **options)
            if url != "about:blank":
                backend.navigate(url)
            return backend
        except BaseException:
            process.close()
            raise

    @classmethod
    def connect(
        cls, endpoint: str = "http://127.0.0.1:9222", url: str = "about:blank", **options: Any,
    ) -> ChromeBackend:
        """Open a new tab in an already running Chrome; the tab is closed by `close()`."""
        connection = CDPConnection(browser_ws_url(endpoint))
        target_id = connection.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        backend = cls(connection, target_id, owns_target=True, **options)
        if url != "about:blank":
            backend.navigate(url)
        return backend

    def _attach(self, target_id: str) -> None:
        self.target_id = target_id
        self.session_id = self._conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True},
        )["sessionId"]
        self._dialog = None
        self._requests: dict[str, float] = {}
        self._call("Page.enable")
        self._call("Network.enable")
        # Let the page behave as focused while the window is in the background.
        self._call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        platform = self._call("Runtime.evaluate", {
            "expression": "navigator.platform", "returnByValue": True,
        })["result"].get("value", "")
        self._mac = str(platform).startswith("Mac")

    @property
    def pid(self) -> int | None:
        """Process ID of the Chrome started by `launch()`; None after `connect()`."""
        return self._process.process.pid if self._process is not None else None

    def close(self) -> None:
        try:
            if self._owns_target:
                self._conn.call("Target.closeTarget", {"targetId": self.target_id})
            self._conn.close()
        except Exception as exc:  # Closing must not mask the caller's result.
            logger.debug("chrome close: %s", exc)
        if self._process is not None:
            self._process.close()

    def __enter__(self) -> ChromeBackend:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ---- navigation (for callers; not a model action) --------------------------

    def navigate(self, url: str) -> None:
        result = self._call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise RuntimeError(f"Navigation to {url} failed: {result['errorText']}")
        self._wait_loaded()

    def _wait_loaded(self) -> None:
        deadline = time.monotonic() + self.load_timeout_s
        while time.monotonic() < deadline:
            try:
                if self._page("probe", timeout_s=2)[1] != "loading":
                    return
            except CDPError:
                pass
            time.sleep(0.05)

    # ---- DevTools plumbing ------------------------------------------------------

    def _on_event(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params", {})
        if method == "Target.targetCreated":
            info = params.get("targetInfo", {})
            if info.get("type") == "page" and info.get("openerId") == self.target_id:
                self._opened_tab = info["targetId"]
        elif event.get("sessionId") == self.session_id:
            if method == "Page.javascriptDialogOpening":
                self._dialog = params
            elif method == "Page.javascriptDialogClosed":
                self._dialog = None
            elif method == "Network.requestWillBeSent":
                if params.get("type") in _TRACKED_REQUESTS:
                    self._requests[params["requestId"]] = time.monotonic()
            elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                self._requests.pop(params.get("requestId"), None)

    def _call(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._conn.call(method, params, session_id=self.session_id, **kwargs)

    def _page(self, method: str, args: Mapping[str, Any] | None = None, *, timeout_s: float | None = None) -> Any:
        """Run one method of the page script and return its JSON value."""
        expression = f"({PAGE_JS})({json.dumps(method)}, {json.dumps(dict(args or {}))})"
        result = self._call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_s=timeout_s,
            until=_is_dialog_event,
        )
        if self._dialog is not None:
            raise StaleDesktopState("A JavaScript dialog is open")
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "error")
            raise CDPError(f"page script {method} failed: {text.splitlines()[0]}")
        return result["result"].get("value")

    def _follow_opened_tab(self) -> bool:
        if self._opened_tab is None:
            return False
        target_id, self._opened_tab = self._opened_tab, None
        self._conn.call("Target.activateTarget", {"targetId": target_id})
        self._attach(target_id)
        self._wait_loaded()
        return True

    # ---- DesktopBackend -------------------------------------------------------

    def observe(self) -> DesktopSnapshot:
        self._conn.drain()
        switched = self._follow_opened_tab()
        if self._dialog is not None:
            return self._dialog_snapshot()
        deadline = time.monotonic() + self.load_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            try:
                data = self._page("observe", {
                    "maxInteractive": self.max_interactive,
                    "maxText": self.max_text,
                    "force": remaining <= 0,
                })
            except StaleDesktopState:
                return self._dialog_snapshot()
            except CDPError:
                # The page is navigating and its execution context went away.
                if remaining <= 0:
                    raise
                time.sleep(0.05)
                continue
            if data.get("loading"):
                time.sleep(0.05)
                continue
            break

        elements = tuple(_element(record) for record in data["elements"])
        context: dict[str, Any] = {
            "backend": "chrome",
            "url": data["url"],
            "title": data["title"],
            "viewport": data["viewport"],
            "scroll": data["scroll"],
            "more_above": data["more_above"],
            "more_below": data["more_below"],
        }
        if any(data["omitted"].values()):
            context["omitted_elements"] = data["omitted"]
        if switched:
            context["switched_to_new_tab"] = True
        screenshot = None
        if self.capture_screenshots:
            png = self._call("Page.captureScreenshot", {"format": "png"})["data"]
            screenshot = lambda png=png: base64.b64decode(png)  # noqa: E731
        return DesktopSnapshot(
            application="Chrome",
            window=data["title"] or data["url"],
            revision=_revision(data["url"], elements),
            elements=elements,
            context=context,
            captured_at_ms=round(time.time() * 1000),
            screenshot=screenshot,
        )

    def _dialog_snapshot(self) -> DesktopSnapshot:
        dialog = self._dialog or {}
        kind = dialog.get("type", "alert")
        elements = [
            DesktopElement(id="dialog_message", role="alertdialog", name=str(dialog.get("message", ""))[:500],
                           source=SOURCE, metadata={"dialog_type": kind}),
            DesktopElement(id=DIALOG_ACCEPT, role="button", name="OK", actions=(ActionKind.CLICK,),
                           parent_id="dialog_message", source=SOURCE),
        ]
        if kind != "alert":
            elements.append(DesktopElement(id=DIALOG_DISMISS, role="button", name="Cancel",
                                           actions=(ActionKind.CLICK,), parent_id="dialog_message", source=SOURCE))
        return DesktopSnapshot(
            application="Chrome",
            window=f"JavaScript {kind}",
            revision=_revision(f"dialog:{kind}:{dialog.get('message')}", tuple(elements)),
            elements=tuple(elements),
            context={"backend": "chrome", "url": dialog.get("url"), "dialog": kind},
            captured_at_ms=round(time.time() * 1000),
        )

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        self._conn.drain()
        if action.target_id in (DIALOG_ACCEPT, DIALOG_DISMISS):
            return self._dialog is not None
        if self._dialog is not None or self._opened_tab is not None:
            return False
        try:
            if action.target_id is None:
                return self._page("probe")[0] == snapshot.context.get("url")
            return self._current_guard(action.target_id) == action.target_guard
        except (CDPError, StaleDesktopState):
            return False

    def _current_guard(self, element_id: str) -> str | None:
        record = self._page("describe", {"id": element_id})
        return _element(record).semantic_guard() if record else None

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        self._conn.drain()
        self._action_started = time.monotonic()
        if action.target_id in (DIALOG_ACCEPT, DIALOG_DISMISS):
            if self._dialog is None:
                raise StaleDesktopState("The JavaScript dialog is no longer open")
            self._call("Page.handleJavaScriptDialog", {"accept": action.target_id == DIALOG_ACCEPT})
            self._dialog = None
            return
        if self._dialog is not None:
            raise StaleDesktopState("A JavaScript dialog is open")
        if action.target_id is not None and self._current_guard(action.target_id) != action.target_guard:
            raise StaleDesktopState(f"Element {action.target_id} changed before execution")

        kind = action.kind
        if kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK):
            x, y = self._point(action.target_id)
            modifiers = self._modifier_bits((action.click_modifier,)) if action.click_modifier else 0
            self._click(x, y, button="right" if kind == ActionKind.RIGHT_CLICK else "left",
                        count=2 if kind == ActionKind.DOUBLE_CLICK else 1, modifiers=modifiers)
        elif kind == ActionKind.TYPE_TEXT:
            x, y = self._point(action.target_id)
            self._click(x, y)
            if self._dialog is None:
                # A field that opens a popup on click (a search or airport picker) can move
                # the caret into the popup's own input; the text goes where the caret is.
                target = self._page("activeEditable").get("id") or action.target_id
                self._page("selectContents", {"id": target})
                self._call("Input.insertText", {"text": str(action.value)}, until=_is_dialog_event)
        elif kind == ActionKind.SET_VALUE:
            result = self._page("setValue", {"id": action.target_id, "value": action.value})
            if result.get("error") == "no_option":
                raise ValueError(f"No option matches {action.value!r}")
            if result.get("error"):
                raise StaleDesktopState(f"Could not set value: {result['error']}")
        elif kind == ActionKind.PRESS_KEY:
            self._key(action.key or "", ())
        elif kind == ActionKind.HOTKEY:
            modifiers, key = parse_hotkey(action.hotkey or "")
            self._key(key, modifiers)
        elif kind == ActionKind.SCROLL:
            self._scroll(action.scroll_direction or "DOWN", snapshot)
        elif kind == ActionKind.WAIT:
            time.sleep(0.2)
        else:
            raise UnsupportedDesktopAction(f"{kind.value} is not supported in Chrome")

    def settle_probe(self) -> tuple[Any, ...]:
        try:
            self._conn.drain()
            if self._dialog is not None:
                return ("dialog", self._dialog.get("message"))
            now = time.monotonic()
            since = getattr(self, "_action_started", now)
            if any(started >= since for started in self._requests.values()):
                # Content the page is still fetching has not arrived: never quiet.
                return ("loading", now)
            return (*self._page("probe", timeout_s=2), self._opened_tab)
        except (CDPError, StaleDesktopState):
            return ("unavailable",)

    # ---- input ---------------------------------------------------------------

    def _point(self, element_id: str | None) -> tuple[float, float]:
        result = self._page("point", {"id": element_id})
        if "error" in result:
            raise StaleDesktopState(f"Element {element_id} is {result['error']}")
        return result["x"], result["y"]

    def _modifier_bits(self, modifiers: tuple[str | None, ...]) -> int:
        bits = {"ALT": _ALT, "CTRL": _CTRL, "SHIFT": _SHIFT, "MOD": _META if self._mac else _CTRL}
        return sum(bits[m] for m in set(modifiers) if m)

    def _mouse(self, kind: str, x: float, y: float, **params: Any) -> None:
        self._call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **params}, until=_is_dialog_event)

    def _click(self, x: float, y: float, *, button: str = "left", count: int = 1, modifiers: int = 0) -> None:
        self._mouse("mouseMoved", x, y, modifiers=modifiers)
        for click_count in range(1, count + 1):
            if self._dialog is not None:
                return
            pressed = {"button": button, "clickCount": click_count, "modifiers": modifiers}
            self._mouse("mousePressed", x, y, **pressed)
            self._mouse("mouseReleased", x, y, **pressed)

    def _key(self, key: str, modifiers: tuple[str, ...]) -> None:
        if key not in _KEYS:
            raise UnsupportedDesktopAction(f"Unsupported key: {key!r}")
        dom_key, code, key_code, text = _KEYS[key]
        bits = self._modifier_bits(modifiers)
        if "SHIFT" in modifiers and len(dom_key) == 1:
            dom_key = dom_key.upper()
            text = text.upper()
        # Modified chords insert no text; only SHIFT keeps a printable key's text.
        typed = text if not bits & ~_SHIFT else ""
        down: dict[str, Any] = {
            "type": "keyDown" if typed else "rawKeyDown",
            "key": dom_key,
            "code": code,
            "windowsVirtualKeyCode": key_code,
            "modifiers": bits,
        }
        if typed:
            down["text"] = typed
        command = _MAC_EDIT_COMMANDS.get((*sorted(modifiers, key=["MOD", "SHIFT"].index), key)) \
            if self._mac and set(modifiers) <= {"MOD", "SHIFT"} else None
        if command:
            down["commands"] = [command]
        self._call("Input.dispatchKeyEvent", down, until=_is_dialog_event)
        if self._dialog is None:
            self._call("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": dom_key, "code": code,
                "windowsVirtualKeyCode": key_code, "modifiers": bits,
            }, until=_is_dialog_event)

    def _scroll(self, direction: str, snapshot: DesktopSnapshot) -> None:
        width, height = snapshot.context.get("viewport") or (1280, 800)
        dx = {"LEFT": -0.8 * width, "RIGHT": 0.8 * width}.get(direction, 0)
        dy = {"UP": -0.8 * height, "DOWN": 0.8 * height}.get(direction, 0)
        self._mouse("mouseWheel", width / 2, height / 2, deltaX=dx, deltaY=dy)


def _element(record: Mapping[str, Any]) -> DesktopElement:
    box = record.get("bounds")
    return DesktopElement(
        id=record["id"],
        role=record["role"],
        name=record.get("name") or "",
        value=record.get("value"),
        actions=tuple(ActionKind(action) for action in record.get("actions", ())),
        enabled=bool(record.get("enabled", True)),
        focused=bool(record.get("focused")),
        selected=record.get("selected"),
        expanded=record.get("expanded"),
        parent_id=record.get("parent"),
        bounds=Bounds(round(box["x"], 1), round(box["y"], 1), round(box["w"], 1), round(box["h"], 1)) if box else None,
        source=SOURCE,
        metadata=record.get("metadata") or {},
    )


def _revision(key: str, elements: tuple[DesktopElement, ...]) -> str:
    payload = [key, [(element.id, element.semantic_guard()) for element in elements]]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:24]
