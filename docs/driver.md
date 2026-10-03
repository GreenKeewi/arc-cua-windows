# arc as a macOS driver

arc-cua's lower layer is a driver for macOS apps: it reads an app's window, runs
its menu commands and acts on its controls **in the background**. The user's
pointer, front app and windows stay as they are while an agent works.

You can use the driver on its own, without arc-cua's decision-model action layer:

- **Over MCP**, from Claude Code, Codex or any MCP client: `arc-cua mcp`.
- **From Python**: `arc_cua.Driver`.

The action layer (`DesktopExecutor` with a decision model) is built on the same
backends; see the [main README](../README.md) for it.

## Set up

```bash
pip install -e '.[macos]'
```

The process that runs the driver needs **Accessibility**, and **Screen Recording**
for screenshots: System Settings → Privacy & Security. With MCP, that is the app
hosting the MCP client (your terminal, for Claude Code or Codex in a terminal).

### Claude Code

```bash
claude mcp add arc-cua -- arc-cua mcp
```

### Codex

```toml
# ~/.codex/config.toml
[mcp_servers.arc-cua]
command = "arc-cua"
args = ["mcp"]
```

Any other client: run `arc-cua mcp`; it speaks MCP over standard input and output,
with logs on standard error.

## How it works

### Snapshots and elements

`observe(pid)` reads the app's front window through accessibility and returns a
**snapshot**: an id, and the window's elements. Each element has an id, a role, a
name, a value when it has one, and **the actions it offers**:

```json
{"snapshot": "s4", "application": "Calculator", "window": "Calculator",
 "elements": [
   {"id": "ax_14", "role": "Button", "name": "7", "actions": ["CLICK"]},
   {"id": "ax_9", "role": "StaticText", "value": "42", "parent": "ax_8"}
 ]}
```

To act, name the snapshot, the element and one of its actions:

```json
{"snapshot": "s4", "action": "CLICK", "element": "ax_14"}
```

Only what is on screen in the window is read. A list of 2,000 files is read as
the rows you can see, so a snapshot stays small (typically 3–26 KB) and quick to
take. Elements keep their ids from one snapshot to the next while they exist.

| Action | Takes |
|---|---|
| `CLICK` | optional `modifier`: `MOD` (Command) or `SHIFT` |
| `DOUBLE_CLICK`, `RIGHT_CLICK` | |
| `SET_VALUE` | `value`: sets text fields, sliders, steppers and other settable values directly |
| `TYPE_TEXT` | `value` |
| `PRESS_KEY` | `key`: `ENTER`, `TAB`, `ESCAPE`, `ARROW_DOWN`… |
| `HOTKEY` | `hotkey`: `MOD+S`, `MOD+SHIFT+Z`… |
| `SCROLL` | `direction`: `UP`, `DOWN`, `LEFT`, `RIGHT` |
| `WAIT` | |

### Checked when it acts, not only when it looks

A snapshot is a picture of a moment, and apps keep changing after it: a sheet
slides in half a second after a click, a menu opens, another window takes focus.
An action based on what the screen *was* can land on something the agent never
saw. Accessibility actions even go through a sheet to the window beneath it.

So the driver keeps a journal of each app's structural changes, from its
accessibility notifications: windows, sheets and menus coming or going, focus
moving to another window. When you act on a snapshot:

- If the app's structure changed since that snapshot, the driver **does not act**.
  It returns status `changed` with a fresh snapshot to decide on.
- If the element itself changed (another value, another place), it returns
  `stale`, also with a fresh snapshot.
- Otherwise it acts, and returns `done`.

Value changes, such as text you just entered, do not count as structural, so
several actions on one snapshot work as you would expect.

### Waiting

Nothing waits after an action. When you expect an action to open something, call
`wait(snapshot)`: it returns a fresh snapshot as soon as the app's structure
changes after that snapshot, or after `timeout_s` (default 1 s).

### Menu commands

`commands(pid)` reads the app's menu bar as commands, without opening a menu:

```json
{"commands": [
  {"path": "File > New Folder", "shortcut": "MOD+SHIFT+N"},
  {"path": "View > as List", "shortcut": "MOD+2", "checked": true},
  {"path": "Edit > Paste", "shortcut": "MOD+V", "enabled": false}
]}
```

`run_command(pid, "File > New Folder")` runs one with the app in the background.
A full menu bar reads in tens of milliseconds. The system's Apple menu is left out.

### Minimized windows and hidden apps

A minimized window or a hidden app is read and controlled as it is: it stays in
the Dock, or hidden. Accessibility actions (`CLICK` on a pressable control,
`SET_VALUE`, `TYPE_TEXT`) and menu commands work there directly. Input that needs
real events (key presses, scrolling, pointer clicks, screenshots) first moves the
window onto an invisible display, where the app draws it and takes input; when
the session ends, or `release(pid)` is called, it is minimized or hidden again
and put back.

### Pixels, for what accessibility does not cover

Canvases, custom-drawn controls and drag targets are not always in the
accessibility tree. For those:

| | |
|---|---|
| `screenshot(pid)` | PNG of the window. `scale` is image pixels per window point. |
| `click_at(pid, x, y)` | `button` left or right, `count` up to 3, `modifiers` |
| `drag(pid, points)` | press at the first point, move through the rest, release at the last |
| `scroll_at(pid, x, y, dx, dy)` | positive `dy` shows what is above |
| `press(pid, keys)` | a key or a chord |
| `type_text(pid, text)` | key events where the app has key focus |

Points are relative to the window's top-left corner, in points (divide screenshot
pixels by `scale`). Pass the `snapshot` a point was chosen from, and the input is
refused when the app's structure changed since, as with `act`.

Prefer elements when they exist: they are faster, they do not depend on where
things are drawn, and they keep working while the window is out of sight.

## MCP tools

| Tool | Does |
|---|---|
| `apps` | Running apps with a user interface: pid, name, bundle id, frontmost, hidden |
| `windows` | An app's windows on screen: window id, title, bounds |
| `observe` | Snapshot of the front window (or a minimized or hidden one); `query` filters elements; `screenshot: true` adds a PNG |
| `act` | One action on an element of a snapshot |
| `wait` | Fresh snapshot once the structure changes, or after `timeout_s` |
| `commands` | The menu bar as commands; `query` filters by path |
| `run_command` | Run a menu command by path |
| `screenshot`, `click_at`, `drag`, `scroll_at`, `press`, `type_text` | Pixels and raw input at window points |

Errors come back as tool results with `isError`, such as an expired snapshot
("observe again") or an action an element does not offer.

## Python

```python
from arc_cua import Driver
from arc_cua.backends import MacOSApp

pid = MacOSApp.from_bundle_id("com.apple.calculator").pid
with Driver() as driver:
    snapshot = driver.observe(pid)
    seven = next(e for e in snapshot.elements if e.name == "7")

    result = driver.act(snapshot, "CLICK", seven.id)
    if not result.done:              # "changed" or "stale": decide again
        snapshot = result.snapshot

    print([c.compact() for c in driver.commands(pid, query="mode")])
    driver.run_command(pid, "View > Scientific")
```

`Driver` keeps one backend per app and puts windows it moved back when it closes.

## Measured

On an Apple-silicon Mac running macOS 26.6, through `arc-cua mcp` (medians):

| | |
|---|---|
| Observe Calculator / System Settings / Obsidian (Electron) | 20 / 55 / 14 ms |
| Observe Finder showing 2,000 files | 37 ms |
| Observe with a screenshot (Calculator) | 35 ms |
| Click until the effect is visible | 29 ms |
| One step: observe, click, observe | 50 ms |
| Type 200 characters into a field | 6 ms |
| Run a menu command until the effect is visible | 9 ms |
| Observe and click in a minimized window / a hidden app | 49 / 22 ms |
| Act on a snapshot taken before a sheet opened | refused in 5 ms, fresh snapshot returned |

`examples/driver_bench/bench.py` reproduces these on your Mac, against real apps and
a native fixture app whose state is checked without going through the driver. It
also checks that no action moved the user's pointer or changed their front app.

## Limits

- macOS only. For web pages, `ChromeBackend` works through the DevTools protocol
  instead (see the main README).
- Popup buttons are set by opening their menu and picking the item; AppKit takes
  about a third of a second to commit the choice.
- Pointer input (`click_at`, and clicks on controls that offer no press action)
  takes about 200 ms, for the event sequence browsers require.
- Windows on another desktop (Space) are not reachable; the driver says so.
