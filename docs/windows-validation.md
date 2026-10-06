# Windows preview validation evidence

## Cloud/Linux checks before publication

Environment: Linux, Python 3.12.14. Upstream base: `6ca19d62c95106732fad28f488ecd458c08e02f4`.

- Full portable suite: **393 passed, 10 skipped**, 25.95 seconds (`python -m pytest -q`). Eight expanded upstream cases require macOS AppKit/ApplicationServices, one upstream platform test is skipped, and one new native Windows dependency test is skipped on Linux.
- Windows backend/CLI/MCP/action-validation integration selection: **89 passed, 1 skipped**, 3.83 seconds.
- Ruff lint of changed implementation and new tests: passed.
- `git diff --check`: passed.
- Wheel and source distribution built successfully: `arc_cua-0.1.2.dev1-py3-none-any.whl` and `arc_cua-0.1.2.dev1.tar.gz`.
- Linux `python -m arc_cua windows-smoke`: exit 2, `unsupported_platform`, `live_windows_validated: false`, as intended.
- No live provider calls or user credentials were used. Test credentials are dummy values from upstream tests. License unchanged.

An initial unrestricted suite attempt encountered eight expanded macOS-framework cases and HTTPX's missing SOCKS support for this cloud environment's proxy. The cloud test environment was given `socksio`; the repository does not require that environment-specific dependency. Explicit non-macOS test exclusions document the framework cases, rather than pretending they passed.

## What these checks establish

Tests cover observation normalization, exact window identity, changed bounds/value/name/focus/visibility/enabled state, stale rejection before input, revalidation after activation, unsupported-action refusal, guard checks, keyboard literal escaping, Ctrl mapping, scroll target preservation, native adapter pattern mapping with fake wrappers, password observation suppression, traversal limits, existing runtime verification, CLI routing, Windows signal compatibility, MCP capability filtering, stale refresh and release.

The native adapter is mocked in Linux checks. **These results do not validate real Windows controls, focus, clicks, key events, Unicode input or scrolling.** Chrome unit tests remain in the full suite; no live Chrome browser test is implied.

## Windows CI and downloads

[The workflow](https://github.com/GreenKeewi/arc-cua-windows/actions/workflows/windows-preview.yml) runs the suite on Windows and Linux, Python 3.12/3.13, and builds distributions. A Windows job bundles native dependencies for a Python 3.12/x64 offline installer ZIP. Successful pushes to this fork publish it as a public prerelease.

A separate step attempts `windows-smoke` on the hosted runner and publishes its JSON as `hosted-windows-smoke-evidence`. Inspect the specific run's output: only `live_windows_validated: true` is evidence of a live **fixture** success. This attempt may fail when an interactive foreground desktop isn't usable. Package publication is intentionally independent of the fixture attempt; unit/build success must not be described as desktop success.

At the time this file was authored, Windows CI and the hosted fixture had not yet run. Use the run linked to the published commit for subsequent evidence. Windows local manual checks are listed in [windows.md](windows.md).
