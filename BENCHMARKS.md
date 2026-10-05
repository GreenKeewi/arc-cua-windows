# arc vs cua-driver

arc-cua 0.1.1 against cua-driver 0.32.0, on one Mac (Apple M5, macOS 26.6.2), October 2026.

## How it was run

- Both drivers over MCP stdio, one at a time, same tasks, same harness.
- arc runs with `settle: true` (waits until the app has reacted), the fair counterpart to cua-driver's own wait after each action.
- Success is checked outside both drivers: the app's own state, or the web page's own evaluator.
- cua-driver follows its documented route: element first, then a fresh look, then pixels, then foreground delivery only after a look shows nothing happened (marked below).

## Head-to-head

Medians. arc with settle vs cua-driver.

| Measure | arc | cua-driver | Result |
|---|---|---|---|
| Click, until the call returns | 217 ms | 1,109 ms | arc 5.1x |
| Click → next observation (one agent turn) | 216 ms | 1,251 ms | arc 5.8x |
| Checkbox: act, settle, observe | 304 ms | 1,244 ms | arc 4.1x |
| Fill a form (2 fields, checkbox, popup, submit) | 1.85 s | 14.95 s | arc 8.1x |
| Type 200 characters, until visible | 6.5 ms | 209 ms | arc 32x |
| Menu command, until visible | 16 ms | 508 ms | arc 32x |
| Read a window: Calculator / form / System Settings | 8 / 4 / 25 ms | 58 / 31 / 124 ms | arc 5–8x |
| Read with a screenshot: Calculator / form | 26 / 18 ms | 178 / 66 ms | arc 4–7x |
| List windows | 0.5 ms | 3.5 ms | arc 7x |
| Minimized window: observe + click | 5/5 | 0/5 | arc |
| Hidden app: observe + click | 5/5 | 1/5 | arc |
| Menu command in a hidden app | 10/10 | 0/10 | arc |
| Act on a screen that changed (sheet opened after observing) | refuses, returns fresh view 5/5 | presses Submit under the sheet 5/5 | arc |
| Sheet opens 0.3 s / 0.8 s after a click | missed 0/5 / 0/5 | caught 5/5 / 5/5 | cua-driver |
| Menu command, until the call returns | 628 ms | 420 ms | cua-driver 1.5x |
| Read Obsidian (Electron) | 13 elements | 380 elements | cua-driver |
| Sheet opens immediately | 463 ms | 465 ms | tie |

**arc leads on 17 of 22 comparable measures, cua-driver on 4, 1 tie.**

Not compared: a sheet opening 1.5 s after a click (both miss it), and reading a 2,000-row table or a 2,000-file Finder window (cua-driver's read was cut off by its 1 s limit; arc read the visible rows in 11 ms and 38 ms).

## Web tasks (cua-bench-basic, 68 variants)

| Task | arc | cua-driver |
|---|---|---|
| click-button | 7/7, 0.37 s | 7/7, 2.51 s |
| click-icon | 6/6, 0.37 s | 6/6, 2.40 s |
| color-picker | 5/5, 0.38 s | 5/5, 2.40 s |
| date-picker | 5/5, 0.31 s | 0/5 |
| drag-drop | 0/5 | 5/5 (foreground), 3.33 s |
| drag-slider | 5/5, 0.39 s | 5/5 (4 foreground), 4.53 s |
| fill-form | 3/3, 4.86 s | 0/3 |
| right-click-menu | 5/5, 1.61 s | 5/5, 4.42 s |
| select-dropdown | 5/5, 0.91 s | 0/5 |
| spreadsheet-cell | 5/5, 0.93 s | 5/5, 3.23 s |
| toggle-switch | 5/5, 0.71 s | 5/5, 2.58 s |
| typing-input | 5/5, 0.65 s | 5/5, 3.53 s |
| video-player | 7/7, 0.40 s | 7/7 (4 foreground), 4.52 s |
| **All** | **63/68 (93%), all in the background** | **55/68 (81%), 42 in the background** |

On the 50 variants both solved, arc is **5.1x faster** (geometric mean; median 6.2x). Counting only cua-driver's background solves: 4.4x.

## Staying in the background

Checked by sampling the app's own active state and the front app's frontmost state every 5 ms:

- In these tests arc never activated the app, including popup menus and menu commands.
- cua-driver's menu command activates the app for about 0.3 s; its foreground-delivery steps (13 web solves) bring the app to the front by design.

## Caveats

- One run on one Mac.
- arc's web support was developed using the cua-bench-basic tasks.
- arc needs the app relaunched with `--force-renderer-accessibility` to read Electron/CEF content.
- arc's settle stops at about 0.3 s after the first reaction, so a dialog that opens later is not in the settled view (the next action is refused as changed instead).
