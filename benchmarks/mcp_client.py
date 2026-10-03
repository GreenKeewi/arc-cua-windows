"""Minimal MCP client over stdio (JSON-RPC, one message per line)."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._next_id = 0
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "arc-driver-bench", "version": "0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Return the result and the size of the raw response line in bytes."""
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise MCPError(f"server closed the stream during {method}")
            message = json.loads(line)
            if message.get("id") == self._next_id:
                if "error" in message:
                    raise MCPError(str(message["error"]))
                return message["result"], len(line)

    def call(self, tool: str, **arguments: Any) -> tuple[dict[str, Any], int, float]:
        """Call a tool; return its structured content, response size and latency in ms."""
        started = time.perf_counter()
        result, size = self.request("tools/call", {"name": tool, "arguments": arguments})
        elapsed = (time.perf_counter() - started) * 1000
        if result.get("isError"):
            text = " ".join(part.get("text", "") for part in result.get("content", []))
            raise MCPError(f"{tool}: {text[:300]}")
        return result.get("structuredContent") or {}, size, elapsed

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
