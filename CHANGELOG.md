# Changelog

## Unreleased

### For apps that embed the driver

- **`release` tool.** `release(pid)` puts back the windows of an app that were moved
  onto the invisible display (minimized or hidden again) and expires its snapshots;
  without `pid` it releases every app. `act` and the input tools report
  `"parked": true` while an app has such a window. In Python: `Driver.release`,
  `Driver.release_all` and `Driver.parked`.
- **Cancellation.** `notifications/cancelled` stops a request: a `wait` returns
  early, an action not yet started is not performed, a queued request is skipped,
  and none gets a response. `ping` is answered while a request runs. In Python, set
  `Driver.cancelled` from another thread; the call raises `Cancelled`.
- **Error codes.** Tool errors carry a stable `code` and `message` in
  `structuredContent` (`permission_denied`, `target_unavailable`, `snapshot_expired`,
  `element_not_found`, `action_not_offered`, ...); see docs/driver.md. The driver's
  exceptions have the same `code`, with new subclasses `ElementNotFound`,
  `ActionNotOffered`, `CommandNotFound`, `InvalidArguments` and `CaptureFailed`.

### Fixed

- A missing Accessibility permission, and any unexpected failure in a tool, came back
  from `arc-cua mcp` as a JSON-RPC protocol error; they are now tool errors.
- Permission messages named "your terminal/Python host"; they now name the app that
  started arc-cua, which may be any app.
- A screenshot without Screen Recording permission now says so (`permission_denied`)
  instead of a generic capture failure.
- An app that could not be opened (for example, without Accessibility permission)
  left a background thread running for each attempt.

## 0.1.0 — 2026-10-04

### A macOS driver you can use on its own

arc-cua now ships its macOS driver as a standalone layer, usable without the
decision-model action layer. It reads an app's windows, runs its menu commands and
acts on its controls in the background: the user's pointer, front app and windows
stay as they are. See [docs/driver.md](docs/driver.md).

- **MCP server.** `arc-cua mcp` serves the driver over stdio to Claude Code, Codex
  or any MCP client, with no extra dependencies: `apps`, `windows`, `observe`, `act`,
  `wait`, `commands`, `run_command`, `screenshot`, `click_at`, `drag`, `scroll_at`,
  `press` and `type_text`.

  ```bash
  claude mcp add arc-cua -- arc-cua mcp
  ```

- **Python API.** `arc_cua.Driver`, with `WindowTarget`, `ActResult` and the same
  operations as the MCP tools.
- **Checked when it acts.** A per-app journal of structural changes (windows, sheets
  and menus coming or going, focus moving to another window) is consulted before
  every action. If the app changed since the snapshot, the action is not performed;
  a fresh snapshot comes back instead (`changed`, or `stale` when the element itself
  changed).
- **Exact windows.** Everything targets one window, a `WindowTarget(pid, window_id)`.
  A pid is resolved once; snapshots carry their window, so actions, waits,
  screenshots and pixel input go to it even when another window takes focus or
  covers it. Each window has its own backend, so observing one window does not make
  another's snapshot stale. A closed or replaced window is reported as gone, never
  swapped for another.
- **Menu commands.** `commands(pid)` reads the whole menu bar (paths, shortcuts,
  enabled, checked) without opening a menu; `run_command(pid, path)` runs one in the
  background, including in hidden apps.
- **Minimized windows and hidden apps** are read and controlled in place, through
  accessibility. Only input that needs real events moves the window onto an invisible
  display, and it is put back afterwards.
- **Screenshots and pixel input** for what accessibility does not cover: `screenshot`
  (with attached sheets), `click_at`, `drag` through any number of points, `scroll_at`,
  `press` and `type_text`, at points relative to the window.
- **Explicit waiting.** Nothing waits after an action; `wait(snapshot)` returns as soon
  as the app's structure changes, or at a timeout.

### Web pages and documents

- Text in web fields and in documents' text views is typed, so pages see their input
  events and documents record an edit (and Save saves it).
- Web checkboxes, switches and radio buttons are clicked rather than set.
- Web sliders, steppers and date fields are stepped to their value, which runs the
  page's handlers; date fields take `YYYY-MM-DD`, in any language.
- Scrolling moves scroll bars through accessibility, which web views follow, and works
  without bringing an out-of-sight window onto a display.
- Observing a web view right after its page loads waits briefly for its accessibility
  tree instead of returning an empty window.
- Picking from a popup or dropdown returns once AppKit has committed the choice.
- Menu commands are pressed even when their enabled state reads stale.

### Benchmarks

`benchmarks/` measures the driver on real apps (`primitives`) and on realistic
multi-step workflows (`workflows`), checks every outcome independently of the
driver, and compares runs between versions (`benchmarks/run.py compare`). Real apps
run as their own instance with window restoration off, on temporary files only.

### Changes

- `examples/driver_bench/` moved to `benchmarks/`.
- `Driver` methods take a `WindowTarget` or a pid; the pixel and input methods no
  longer take `window_id=` (pass `WindowTarget(pid, window_id)` instead).

### Limits

- macOS only (the Chrome backend for the action layer still runs anywhere Chrome does).
- HTML5 drag and drop is not supported: it needs a real on-screen pointer drag.
- Menu commands act on the app's key window.
- Windows on another desktop (Space) are not reachable.
