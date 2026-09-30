"""Minimal synchronous Chrome DevTools Protocol client and Chrome launcher."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], None]


class CDPError(RuntimeError):
    """A DevTools command failed or timed out."""


class CDPConnection:
    """One browser-level DevTools websocket, used in flattened session mode.

    Commands are sent one at a time and wait for their own response. Events that
    arrive meanwhile go to `on_event`. A command can stop waiting early when an
    event matches `until`, for example an input event that opened a JS dialog,
    whose response Chrome withholds until the dialog closes.
    """

    def __init__(self, ws_url: str, *, timeout_s: float = 15, on_event: EventHandler | None = None) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise RuntimeError("Install the browser extra: pip install 'arc-cua[browser]'") from exc
        # Entered as a context manager: connecting directly is deprecated in newer websockets.
        self._connection = connect(ws_url, max_size=None, compression=None, proxy=None, open_timeout=timeout_s)
        self._ws = self._connection.__enter__()
        self._next_id = 0
        self.timeout_s = timeout_s
        self.on_event = on_event

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
        until: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        self._ws.send(json.dumps(message))
        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError(f"{method} timed out")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                raise CDPError(f"{method} timed out") from None
            reply = json.loads(raw)
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise CDPError(f"{method}: {reply['error'].get('message', 'failed')}")
                return reply.get("result", {})
            if "method" in reply:
                self._dispatch(reply)
                if until is not None and until(reply):
                    return {}
            # Replies to earlier commands that stopped waiting early are dropped.

    def drain(self) -> None:
        """Deliver events that have already arrived, without waiting."""
        while True:
            try:
                raw = self._ws.recv(timeout=0)
            except TimeoutError:
                return
            reply = json.loads(raw)
            if "method" in reply:
                self._dispatch(reply)

    def _dispatch(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def close(self) -> None:
        self._connection.__exit__(None, None, None)


def find_chrome() -> str:
    """Locate a Chrome or Chromium executable; ARC_CHROME overrides the search."""
    override = os.environ.get("ARC_CHROME")
    if override:
        return override
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif sys.platform == "win32":
        roots = [os.environ.get(name, "") for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        candidates = [str(Path(root) / "Google/Chrome/Application/chrome.exe") for root in roots if root]
    else:
        candidates = [
            found
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
            if (found := shutil.which(name))
        ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Chrome not found; install it or set ARC_CHROME to its executable")


class ChromeProcess:
    """A Chrome instance started with remote debugging on a free local port."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        headless: bool = False,
        user_data_dir: str | None = None,
        window_size: tuple[int, int] = (1280, 900),
        extra_args: tuple[str, ...] = (),
        start_timeout_s: float = 20,
    ) -> None:
        self._owned_dir = user_data_dir is None
        self.user_data_dir = user_data_dir or tempfile.mkdtemp(prefix="arc-cua-chrome-")
        port_file = Path(self.user_data_dir) / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        args = [
            executable or find_chrome(),
            "--remote-debugging-port=0",
            f"--user-data-dir={self.user_data_dir}",
            f"--window-size={window_size[0]},{window_size[1]}",
            "--no-first-run",
            "--no-default-browser-check",
            # Keep pages rendering and timers running when the window is behind
            # other windows, so the agent can work without taking focus.
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            *(("--headless=new",) if headless else ()),
            *extra_args,
            "about:blank",
        ]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + start_timeout_s
        while True:
            if self.process.poll() is not None:
                raise RuntimeError(f"Chrome exited during startup (code {self.process.returncode})")
            lines = port_file.read_text().split() if port_file.exists() else []
            if len(lines) >= 2:
                self.ws_url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                return
            if time.monotonic() > deadline:
                self.close()
                raise RuntimeError("Chrome did not open a DevTools port in time")
            time.sleep(0.05)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._owned_dir:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)


def browser_ws_url(endpoint: str, *, timeout_s: float = 5) -> str:
    """Resolve an http://host:port DevTools endpoint to its browser websocket URL."""
    import httpx

    response = httpx.get(endpoint.rstrip("/") + "/json/version", timeout=timeout_s)
    response.raise_for_status()
    return response.json()["webSocketDebuggerUrl"]
