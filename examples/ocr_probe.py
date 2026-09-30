"""Print the visible text Apple Vision reads in a macOS app's window.

Usage:
  python examples/ocr_probe.py com.apple.TextEdit   # or a process ID
"""

import sys

from arc_cua.backends import (
    MacOSApp,
    MacOSHybridBackend,
)

target = sys.argv[1] if len(sys.argv) > 1 else "com.apple.finder"
pid = int(target) if target.isdigit() else MacOSApp.from_bundle_id(target).pid

with MacOSHybridBackend(pid) as backend:
    snapshot = backend.observe()

print(
    f"\n{snapshot.application} "
    f"— {snapshot.window}"
)

print(
    f"total elements: "
    f"{len(snapshot.elements)}"
)

ocr = [
    element
    for element in snapshot.elements
    if element.source == "macos_ocr"
]

print(
    f"OCR elements: {len(ocr)}\n"
)

for element in ocr:

    bounds = element.bounds

    print(
        f"{element.id}  "
        f"{element.name!r}  "
        f"conf="
        f"{element.metadata.get('confidence')}  "
        f"bounds=("
        f"{bounds.x:.0f}, "
        f"{bounds.y:.0f}, "
        f"{bounds.width:.0f}, "
        f"{bounds.height:.0f})"
    )
