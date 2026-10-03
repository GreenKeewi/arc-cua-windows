"""A native macOS window showing one web page in WKWebView, for the web workflows.

    python web_host.py PAGE.html TITLE WIDTH HEIGHT SOCKET

It runs as its own app process and never activates itself, so the driver has to
work in the background. The page's JavaScript can be evaluated from outside over a
Unix socket: send one JSON line {"js": "..."}; get back {"value": ...} or
{"error": "..."}. Workflows check their result that way, without the driver.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

import AppKit
import Foundation
import objc
import WebKit


class Host(AppKit.NSObject):
    def initWithPage_title_size_(self, page: str, title: str, size):
        self = objc.super(Host, self).init()
        self.page, self.title, self.size = page, title, size
        self.pending: dict[int, dict] = {}
        self.lock = threading.Lock()
        return self

    def build(self) -> None:
        width, height = self.size
        frame = Foundation.NSMakeRect(120, 140, width, height)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskMiniaturizable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_(self.title)
        config = WebKit.WKWebViewConfiguration.alloc().init()
        self.web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            self.window.contentView().bounds(), config,
        )
        self.web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.window.contentView().addSubview_(self.web)
        page = Path(self.page)
        self.web.loadHTMLString_baseURL_(page.read_text("utf-8"), Foundation.NSURL.fileURLWithPath_(str(page.parent)))
        self.window.orderFrontRegardless()  # Shown, but the app does not activate.

    def evaluate_(self, request) -> None:
        """On the main thread: evaluate JavaScript and hand the result back to the socket thread."""
        key, script = request
        entry = self.pending[key]

        def done(value, error) -> None:
            if error is not None:
                entry["result"] = {"error": str(error.localizedDescription())}
            else:
                entry["result"] = {"value": _plain(value)}
            entry["event"].set()

        self.web.evaluateJavaScript_completionHandler_(script, done)


def _plain(value):
    """Foundation objects from JavaScript as JSON-able Python values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Foundation.NSNumber):
        number = value.doubleValue()
        return number if number != int(number) else int(number)
    if isinstance(value, Foundation.NSString):
        return str(value)
    if isinstance(value, (Foundation.NSArray, list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (Foundation.NSDictionary, dict)):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Foundation.NSNull):
        return None
    return str(value)


def serve(host: Host, path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    counter = 0
    while True:
        connection, _ = server.accept()
        with connection, connection.makefile("rw") as stream:
            for line in stream:
                request = json.loads(line)
                counter += 1
                event = threading.Event()
                host.pending[counter] = {"event": event}
                host.performSelectorOnMainThread_withObject_waitUntilDone_("evaluate:", (counter, request["js"]), False)
                if not event.wait(10):
                    reply = {"error": "timed out"}
                else:
                    reply = host.pending.pop(counter)["result"]
                stream.write(json.dumps(reply) + "\n")
                stream.flush()


def main() -> None:
    page, title, width, height, sock = sys.argv[1:6]
    activity = Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        Foundation.NSActivityUserInitiated | Foundation.NSActivityLatencyCritical, "benchmark web page",
    )
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    host = Host.alloc().initWithPage_title_size_(page, title, (int(width), int(height)))
    host.build()
    threading.Thread(target=serve, args=(host, sock), daemon=True).start()
    app.run()
    del activity


if __name__ == "__main__":
    main()
