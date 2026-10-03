"""A small native form used as a benchmark target.

Runs as its own process with a regular window that never activates itself, so
drivers have to reach it in the background. Its state is written to a JSON file
whenever it changes; the benchmark reads that file as the ground truth, without
going through either driver.

    python fixture_form.py STATE_PATH [--rows N]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import AppKit
import Foundation
import objc

PLANS = ("Basic", "Pro", "Team")
DIALOG_DELAYS_MS = (0, 300, 800, 1500)


def make_field(view, title: str, y: float):
    label = AppKit.NSTextField.labelWithString_(title)
    label.setFrame_(Foundation.NSMakeRect(20, y, 100, 22))
    view.addSubview_(label)
    field = AppKit.NSTextField.alloc().initWithFrame_(Foundation.NSMakeRect(130, y, 260, 24))
    field.setAccessibilityLabel_(title)
    view.addSubview_(field)
    return field


def make_table(view, source):
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(Foundation.NSMakeRect(20, 20, 380, 340))
    table = AppKit.NSTableView.alloc().initWithFrame_(scroll.bounds())
    column = AppKit.NSTableColumn.alloc().initWithIdentifier_("item")
    column.setTitle_("Item")
    column.setWidth_(340)
    table.addTableColumn_(column)
    table.setDataSource_(source)
    scroll.setDocumentView_(table)
    scroll.setHasVerticalScroller_(True)
    view.addSubview_(scroll)
    return table


class Form(AppKit.NSObject):
    def initWithPath_rows_(self, path: str, rows: int):
        self = objc.super(Form, self).init()
        self.path = Path(path)
        self.rows = rows
        self.submitted = 0
        self.counter = 0
        self.second = None
        self.start = "shown"
        self.last = None
        return self

    def build(self) -> None:
        height = 300 + (360 if self.rows else 0)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(80, 120, 420, height),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskMiniaturizable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Arc Bench Form")
        view = self.window.contentView()
        top = height - 40

        self.name = make_field(view, "Full name", top)
        self.email = make_field(view, "Email", top - 40)

        self.subscribe = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(130, top - 80, 200, 24))
        self.subscribe.setButtonType_(AppKit.NSButtonTypeSwitch)
        self.subscribe.setTitle_("Subscribe")
        view.addSubview_(self.subscribe)

        label = AppKit.NSTextField.labelWithString_("Plan")
        label.setFrame_(Foundation.NSMakeRect(20, top - 118, 100, 22))
        view.addSubview_(label)
        self.plan = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            Foundation.NSMakeRect(130, top - 122, 160, 28), False,
        )
        self.plan.addItemsWithTitles_(list(PLANS))
        self.plan.setAccessibilityLabel_("Plan")
        view.addSubview_(self.plan)

        submit = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(130, top - 170, 100, 32))
        submit.setTitle_("Submit")
        submit.setBezelStyle_(AppKit.NSBezelStyleRounded)
        submit.setTarget_(self)
        submit.setAction_("submit:")
        view.addSubview_(submit)

        # Each opens a sheet after its delay, to test how drivers wait for slow UI.
        for index, delay_ms in enumerate(DIALOG_DELAYS_MS):
            button = AppKit.NSButton.alloc().initWithFrame_(
                Foundation.NSMakeRect(20 + index * 98, top - 215, 94, 32),
            )
            button.setTitle_(f"Open {delay_ms} ms")
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTag_(delay_ms)
            button.setTarget_(self)
            button.setAction_("openLater:")
            view.addSubview_(button)
        self.dialog = None

        if self.rows:
            self.table = make_table(view, self)

        # Show the window without activating the app: drivers must work in the background.
        self.window.orderFrontRegardless()
        self.timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.005, self, "tick:", None, True,
        )

    def numberOfRowsInTableView_(self, table) -> int:
        return self.rows

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return f"Item {row + 1:05d}"

    def buildSecondWindow(self) -> None:
        """A second window exactly over the first, with its own controls."""
        frame = self.window.frame()
        self.second = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            self.window.contentRectForFrameRect_(frame),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskMiniaturizable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.second.setTitle_("Arc Bench Form 2")
        view = self.second.contentView()
        top = view.frame().size.height - 40
        self.notes = make_field(view, "Notes", top)
        self.agree = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(130, top - 80, 200, 24))
        self.agree.setButtonType_(AppKit.NSButtonTypeSwitch)
        self.agree.setTitle_("Agree")
        view.addSubview_(self.agree)
        self.second.orderFrontRegardless()

    def applyStart_(self, timer) -> None:
        if self.start == "minimized":
            self.window.miniaturize_(None)
        elif self.start == "hidden":
            AppKit.NSApp.hide_(None)

    def openLater_(self, sender) -> None:
        self.performSelector_withObject_afterDelay_("openDialog:", None, sender.tag() / 1000)

    def openDialog_(self, ignored) -> None:
        if self.dialog is not None:
            return
        sheet = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, 260, 110), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False,
        )
        label = AppKit.NSTextField.labelWithString_("Dialog open")
        label.setFrame_(Foundation.NSMakeRect(20, 64, 220, 22))
        sheet.contentView().addSubview_(label)
        close = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(150, 16, 90, 32))
        close.setTitle_("Close")
        close.setBezelStyle_(AppKit.NSBezelStyleRounded)
        close.setTarget_(self)
        close.setAction_("closeDialog:")
        sheet.contentView().addSubview_(close)
        self.dialog = sheet
        self.window.beginSheet_completionHandler_(sheet, None)

    def closeDialog_(self, sender) -> None:
        if self.dialog is not None:
            self.window.endSheet_(self.dialog)
            self.dialog.orderOut_(None)
            self.dialog = None

    def increment_(self, sender) -> None:
        self.counter += 1

    def secondToFront_(self, sender) -> None:
        if self.second is not None:
            self.second.orderFrontRegardless()

    def resetCounter_(self, sender) -> None:
        self.counter = 0
        self.second = None

    def buildMenu(self) -> None:
        """App menu plus a Bench menu: Increment (Cmd+I) and More > Reset Counter."""
        bar = AppKit.NSMenu.alloc().init()
        app_item = AppKit.NSMenuItem.alloc().init()
        bar.addItem_(app_item)
        app_menu = AppKit.NSMenu.alloc().initWithTitle_("Arc Bench")
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit Arc Bench", "terminate:", "q")
        app_item.setSubmenu_(app_menu)

        bench_item = AppKit.NSMenuItem.alloc().init()
        bar.addItem_(bench_item)
        bench = AppKit.NSMenu.alloc().initWithTitle_("Bench")
        increment = bench.addItemWithTitle_action_keyEquivalent_("Increment", "increment:", "i")
        increment.setTarget_(self)
        more_item = bench.addItemWithTitle_action_keyEquivalent_("More", None, "")
        more = AppKit.NSMenu.alloc().initWithTitle_("More")
        reset = more.addItemWithTitle_action_keyEquivalent_("Reset Counter", "resetCounter:", "")
        reset.setTarget_(self)
        raise_second = more.addItemWithTitle_action_keyEquivalent_("Second Window to Front", "secondToFront:", "")
        raise_second.setTarget_(self)
        more_item.setSubmenu_(more)
        bench_item.setSubmenu_(bench)
        AppKit.NSApp.setMainMenu_(bar)

    def submit_(self, sender) -> None:
        self.submitted += 1

    def state(self) -> dict:
        return {
            "name": str(self.name.stringValue()),
            "email": str(self.email.stringValue()),
            "subscribe": bool(self.subscribe.state()),
            "plan": str(self.plan.titleOfSelectedItem()),
            "submitted": self.submitted,
            "counter": self.counter,
            **({"notes": str(self.notes.stringValue()), "agree": bool(self.agree.state())} if self.second else {}),
            "dialog": self.dialog is not None,
            "active": bool(AppKit.NSApp.isActive()),
            "minimized": bool(self.window.isMiniaturized()),
            "hidden": bool(AppKit.NSApp.isHidden()),
        }

    def tick_(self, timer) -> None:
        state = self.state()
        if state != self.last:
            self.last = state
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(state))
            os.replace(temp, self.path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_path")
    parser.add_argument("--rows", type=int, default=0)
    parser.add_argument("--start", choices=("shown", "minimized", "hidden"), default="shown")
    parser.add_argument("--second-window", action="store_true", help="a second window over the first")
    args = parser.parse_args()

    # Keep the state timer precise: App Nap would otherwise delay the ground truth.
    activity = Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        Foundation.NSActivityUserInitiated | Foundation.NSActivityLatencyCritical, "benchmark ground truth",
    )
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    form = Form.alloc().initWithPath_rows_(args.state_path, args.rows)
    form.build()
    form.buildMenu()
    if args.second_window:
        form.buildSecondWindow()
    form.start = args.start
    if args.start != "shown":
        # Once the run loop is up, so the window server knows the window first.
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, form, "applyStart:", None, False,
        )
    app.run()
    del activity


if __name__ == "__main__":
    main()
