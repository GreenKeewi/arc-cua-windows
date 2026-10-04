class JevDesktopError(RuntimeError):
    pass


class StaleDesktopState(JevDesktopError):
    """The chosen target no longer means what it meant when observed."""


class InvalidDecision(JevDesktopError):
    """The policy returned an action that is not legal in the current snapshot."""


class UnsupportedDesktopAction(JevDesktopError):
    pass


class TargetUnavailable(JevDesktopError):
    """The target app quit, or has no window that can be observed and controlled."""


class Cancelled(JevDesktopError):
    """The caller cancelled the operation; nothing more was done."""
