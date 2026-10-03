"""``arc-cua mcp``: the macOS driver as an MCP server over standard input and output.

Any MCP client (Claude Code, Codex, an agent of your own) can observe and act on
macOS apps in the background through it. MCP's stdio transport is JSON-RPC with
one message per line, implemented here directly.

Tools: ``apps``, ``windows``, ``observe``, ``act``, ``wait``, ``commands`` and
``run_command``. An observation returns a snapshot id and the window's elements,
each with an id and the actions it offers; ``act`` names the snapshot, the element
and the action. If the app's structure changed since the snapshot (a sheet, window
or menu came or went), ``act`` does not act and returns a fresh snapshot instead.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import OrderedDict
from typing import IO, Any

from .errors import JevDesktopError
from .models import ActionKind, DesktopSnapshot

logger = logging.getLogger("arc_cua.mcp")

PROTOCOL_VERSION = "2025-06-18"
_KEEP_SNAPSHOTS = 64

INSTRUCTIONS = (
    "Control macOS apps in the background: the user's pointer, front app and windows stay as they are. "
    "Call observe(pid) to get a snapshot id and the window's elements; each element has an id and the "
    "actions it offers. Then act(snapshot, action, element). If act returns status 'changed' or 'stale', "
    "the app changed under the snapshot and nothing was done: decide again from the fresh snapshot it "
    "returns. commands(pid) lists the app's menu commands; run_command(pid, path) runs one."
)

_ACTIONS = [kind.value for kind in ActionKind]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "apps",
        "description": "Running apps with a user interface: pid, name, bundle_id, frontmost, hidden.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "windows",
        "description": "An app's windows on screen, front to back: window_id, title, bounds.",
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "observe",
        "description": (
            "Read the app's front window (or its minimized window, or a hidden app's window) as elements: "
            "id, role, name, value and the actions each offers. Only what is on screen in the window is read. "
            "Returns a snapshot id for act. query keeps elements whose role, name or value contains it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}, "query": {"type": "string"}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "act",
        "description": (
            "Perform one action on an element of a snapshot. Actions: " + ", ".join(_ACTIONS) + ". "
            "SET_VALUE and TYPE_TEXT take value; PRESS_KEY takes key (ENTER, TAB, ESCAPE, ARROW_DOWN...); "
            "HOTKEY takes hotkey (MOD+S; MOD is Command); SCROLL takes direction (UP, DOWN, LEFT, RIGHT); "
            "CLICK may take modifier (MOD or SHIFT). Returns status 'done', or 'changed'/'stale' with a fresh "
            "snapshot when the app changed since the snapshot, in which case nothing was done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string"},
                "action": {"type": "string", "enum": _ACTIONS},
                "element": {"type": "string"},
                "value": {"type": ["string", "number", "boolean"]},
                "key": {"type": "string"},
                "hotkey": {"type": "string"},
                "direction": {"type": "string", "enum": ["UP", "DOWN", "LEFT", "RIGHT"]},
                "modifier": {"type": "string", "enum": ["MOD", "SHIFT"]},
            },
            "required": ["snapshot", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": (
            "Wait until the app's structure changes after a snapshot (a sheet, window or menu comes or goes), "
            "or until timeout_s (default 1), then return a fresh snapshot. Use it after an action that should "
            "open something."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string"},
                "timeout_s": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["snapshot"],
            "additionalProperties": False,
        },
    },
    {
        "name": "commands",
        "description": (
            "The app's menu bar as commands: path, shortcut, and whether each is enabled or checked. "
            "query keeps commands whose path contains it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}, "query": {"type": "string"}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": "Run a menu command by path, such as 'File > Export > PDF…', with the app in the background.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "path": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            },
            "required": ["pid", "path"],
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    pass


class Server:
    def __init__(self, driver: Any = None) -> None:
        if driver is None:
            from .driver import Driver

            driver = Driver()
        self.driver = driver
        self._snapshots: OrderedDict[str, DesktopSnapshot] = OrderedDict()
        self._next = 0

    # ---- snapshots ---------------------------------------------------------------

    def _keep(self, snapshot: DesktopSnapshot) -> str:
        self._next += 1
        name = f"s{self._next}"
        self._snapshots[name] = snapshot
        while len(self._snapshots) > _KEEP_SNAPSHOTS:
            self._snapshots.popitem(last=False)
        return name

    def _snapshot(self, name: str) -> DesktopSnapshot:
        try:
            return self._snapshots[name]
        except KeyError:
            raise ToolError(f"Unknown or expired snapshot {name!r}; observe again") from None

    def _render(self, snapshot: DesktopSnapshot, query: str | None = None) -> dict[str, Any]:
        elements = [_element(e) for e in snapshot.elements if e.visible]
        if query:
            needle = query.casefold()
            elements = [
                e for e in elements
                if any(needle in str(e.get(field, "")).casefold() for field in ("role", "name", "value"))
            ]
        result: dict[str, Any] = {
            "snapshot": self._keep(snapshot),
            "pid": snapshot.context.get("pid"),
            "application": snapshot.application,
            "window": snapshot.window,
            "elements": elements,
        }
        return result

    # ---- tools ---------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            raise ToolError(f"Unknown tool {name!r}")
        return handler(**arguments)

    def tool_apps(self) -> dict[str, Any]:
        return {"apps": self.driver.apps()}

    def tool_windows(self, pid: int) -> dict[str, Any]:
        return {"windows": [
            {
                "window_id": w.window_id, "title": w.title,
                "bounds": {"x": w.bounds.x, "y": w.bounds.y, "width": w.bounds.width, "height": w.bounds.height},
            }
            for w in self.driver.windows(pid)
        ]}

    def tool_observe(self, pid: int, query: str | None = None) -> dict[str, Any]:
        return self._render(self.driver.observe(pid), query)

    def tool_act(
        self, snapshot: str, action: str, element: str | None = None, value: Any = None, key: str | None = None,
        hotkey: str | None = None, direction: str | None = None, modifier: str | None = None,
    ) -> dict[str, Any]:
        result = self.driver.act(
            self._snapshot(snapshot), action, element, value=value, key=key, hotkey=hotkey,
            scroll_direction=direction, click_modifier=modifier,
        )
        response: dict[str, Any] = {"status": result.status, "elapsed_ms": round(result.elapsed_ms, 1)}
        if result.changes:
            response["changes"] = list(result.changes)
        if result.snapshot is not None:
            response["fresh"] = self._render(result.snapshot)
        return response

    def tool_wait(self, snapshot: str, timeout_s: float = 1.0) -> dict[str, Any]:
        return self._render(self.driver.wait(self._snapshot(snapshot), timeout_s=timeout_s))

    def tool_commands(self, pid: int, query: str | None = None) -> dict[str, Any]:
        return {"commands": [c.compact() for c in self.driver.commands(pid, query=query)]}

    def tool_run_command(self, pid: int, path: str | list[str]) -> dict[str, Any]:
        result = self.driver.run_command(pid, path)
        return {"status": result.status, "elapsed_ms": round(result.elapsed_ms, 1)}

    # ---- protocol ------------------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; None for notifications."""
        method = message.get("method")
        ident = message.get("id")
        if ident is None:
            return None  # A notification, such as notifications/initialized.
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "arc-cua", "version": _version()},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._tool_result(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as exc:  # Never let one bad message stop the server.
            logger.exception("request failed")
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def _tool_result(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            content = self.call(name, arguments)
        except (ToolError, JevDesktopError, ValueError, TypeError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        text = json.dumps(content, ensure_ascii=False, separators=(",", ":"), default=str)
        return {"content": [{"type": "text", "text": text}], "structuredContent": content}

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                reply: dict[str, Any] | None = {
                    "jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"},
                }
            else:
                reply = self.handle(message) if isinstance(message, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                stdout.flush()

    def close(self) -> None:
        self.driver.close()


def _element(element: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": element.id, "role": element.role}
    if element.name:
        data["name"] = element.name
    if element.value not in (None, ""):
        data["value"] = element.value
    if element.actions:
        data["actions"] = [a.value for a in element.actions]
    for field in ("focused", "selected", "expanded"):
        if getattr(element, field):
            data[field] = True
    if not element.enabled:
        data["enabled"] = False
    if element.parent_id:
        data["parent"] = element.parent_id
    return data


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("arc-cua")
    except Exception:
        return "0"


def serve(stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> int:
    server = Server()
    try:
        server.serve(stdin, stdout)
    finally:
        server.close()
    return 0
