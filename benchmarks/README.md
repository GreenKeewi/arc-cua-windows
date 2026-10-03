# Driver benchmarks

Benchmarks for arc's macOS driver, to run on every version and catch regressions in
speed or correctness. Two suites:

| Suite | What it measures |
|---|---|
| `primitives` | Single driver operations on real apps and a native fixture: listing windows, observing (Calculator, Finder with 2,000 files, System Settings, an Electron app, the fixture, a 2,000-row table), observing with a screenshot, clicks, typing, a form fill, menu commands, minimized and hidden windows, waiting for a sheet that opens late, and acting on a screen that changed after it was observed |
| `workflows` | Realistic multi-step tasks timed end to end: a calculation in Calculator, editing and saving a document in TextEdit, opening folders in Finder, a native form with a popup, a sheet and a menu command, the same form in a minimized window and in a covered window, a web signup form (text, email, dropdown, date, slider, radio, checkbox), and a field 45 rows down a web page |

Each runs through arc in-process and through `arc-cua mcp` over stdio, as an MCP
client uses it.

## Running

```bash
python benchmarks/run.py primitives            # ~10 min; --quick for fewer repetitions
python benchmarks/run.py workflows             # ~5 min; --reps N, --only NAME, --no-mcp
python benchmarks/run.py all --quick
```

Results are saved to `output/benchmarks/` as JSON and Markdown, named by suite and
commit. Each file records the arc version, git commit (and whether the tree had
uncommitted changes), macOS version, chip and Python version.

Compare two runs, for example the last release against your branch:

```bash
python benchmarks/run.py compare output/benchmarks/workflows-OLD.json output/benchmarks/workflows-NEW.json
```

It lists measurements whose median changed and flags any more than 1.2× slower
(`--slower` to change that) or with a lower success rate.

## What makes the numbers trustworthy

- **Checks never go through the driver.** Every outcome is read independently: the
  file on disk, the web page's own record of what it received (from its input
  events), the native fixture's state file, or a separate accessibility read
  (Calculator's display, Finder's window).
- **Effects, not just calls.** In the primitives suite a watcher thread times when an
  effect actually lands, separately from when the call returns.
- **Background guarantees are checked on every action:** the user's front app and
  pointer must not change. Pointer moves made while a physical mouse was in use are
  set aside rather than counted against the driver.
- **First runs are reported apart** (`first_ms`); medians and p90 are over the rest.
- **Late outcomes are not hidden.** After a workflow's last step, the check is retried
  for up to 3 s (untimed); how long that took is recorded as `landed_after_ms`.

## Safety

The suites drive real apps on your Mac, in the background. They are built not to
touch your data:

- Real apps (Calculator, TextEdit) are started as a separate instance with window
  restoration off, so they open only the benchmark's temporary files and none of
  yours, and are quit afterwards.
- Finder only ever acts in a window on a temporary folder, and only opens folders and
  selects a file in it; that window is closed afterwards.
- Everything else runs in the benchmark's own fixture app (`fixture_form.py`) and web
  pages (`pages/`, shown by `web_host.py`).
- Temporary folders are removed after each run.

Leave the Mac alone while a suite runs, and keep the display awake (`caffeinate -d`):
with the display asleep, macOS reports no windows.

## Requirements

macOS with Accessibility and Screen Recording granted to the terminal running the
benchmarks, and `pip install -e '.[macos]'`. The primitives suite also uses Finder,
System Settings and, when installed, Obsidian.
