"""The macOS privacy permissions the driver needs, as this process has them.

macOS grants them to the app that started the process (a terminal, an MCP client
or another app), so the messages name that app rather than a particular one.
"""

from __future__ import annotations

ACCESSIBILITY_REQUIRED = (
    "macOS Accessibility permission is required. Grant it to the app that started arc-cua "
    "in System Settings > Privacy & Security > Accessibility."
)
SCREEN_RECORDING_REQUIRED = (
    "macOS Screen Recording permission is required for screenshots and reading text from pixels. Grant it to "
    "the app that started arc-cua in System Settings > Privacy & Security, then restart that app."
)


def accessibility_trusted() -> bool:
    import ApplicationServices as AS  # type: ignore

    return bool(AS.AXIsProcessTrusted())


def screen_recording_allowed() -> bool:
    """Whether captures show other apps' windows; True when this macOS cannot say."""
    import Quartz  # type: ignore

    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except AttributeError:
        return True
