class JevDesktopError(RuntimeError):
    code = "desktop_error"  # Stable, machine-readable; reported as the MCP error code.


class StaleDesktopState(JevDesktopError):
    """The chosen target no longer means what it meant when observed."""

    code = "stale"


class InvalidDecision(JevDesktopError):
    """The policy returned an action that is not legal in the current snapshot."""

    code = "invalid_decision"


class UnsupportedDesktopAction(JevDesktopError):
    code = "unsupported_action"


class ElementNotFound(UnsupportedDesktopAction):
    """The snapshot has no element with that id."""

    code = "element_not_found"


class ActionNotOffered(UnsupportedDesktopAction):
    """The element does not offer that action."""

    code = "action_not_offered"


class CommandNotFound(UnsupportedDesktopAction):
    """The app's menu bar has no command at that path."""

    code = "command_not_found"


class InvalidArguments(UnsupportedDesktopAction):
    """The action is missing something it needs, or got a value it cannot use."""

    code = "invalid_arguments"


class CaptureFailed(UnsupportedDesktopAction):
    """The window could not be captured although Screen Recording is allowed."""

    code = "capture_failed"


class TargetUnavailable(JevDesktopError):
    """The target app quit, or has no window that can be observed and controlled."""

    code = "target_unavailable"


class Cancelled(JevDesktopError):
    """The caller cancelled the operation; nothing more was done."""

    code = "cancelled"
