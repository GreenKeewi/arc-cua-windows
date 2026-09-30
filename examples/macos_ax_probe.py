"""Print a compact semantic snapshot of a macOS app.

Usage:
  pip install -e '.[macos]'
  python examples/macos_ax_probe.py com.apple.TextEdit   # or a process ID

Grant your terminal/Python host Accessibility permission first.
"""

import sys

from arc_cua.backends import MacOSApp, MacOSAXBackend

target = sys.argv[1] if len(sys.argv) > 1 else "com.apple.finder"
pid = int(target) if target.isdigit() else MacOSApp.from_bundle_id(target).pid
with MacOSAXBackend(pid) as backend:
    snapshot = backend.observe()
print(f"{snapshot.application} — {snapshot.window} — {len(snapshot.elements)} elements")
for element in snapshot.elements[:150]:
    print(element.compact())
