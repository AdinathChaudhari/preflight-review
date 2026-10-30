"""Named exceptions: every failure mode fails loudly with its own type."""


class PreflightError(Exception):
    """Base class for all preflight-review errors."""


class GitError(PreflightError):
    """A git command failed or a ref/path could not be resolved."""


class ConfigError(PreflightError):
    """The config file is malformed, has unknown keys, or wrong types."""


class ScopeError(PreflightError):
    """The review scope (base, diff, files) could not be determined."""


class ToolError(PreflightError):
    """A deterministic tool (ruff, gitleaks, ...) failed unexpectedly."""


class BackendError(PreflightError):
    """The model backend failed to produce a usable response."""


class SchemaError(PreflightError):
    """Data does not match the expected schema."""


class PacketBudgetError(PreflightError):
    """The context packet exceeds its size budget."""


class StateError(PreflightError):
    """The persisted state file is corrupt or has an unsupported version."""


class RenderError(PreflightError):
    """Report rendering failed."""


class IncompleteRunError(PreflightError):
    """A lane failed or the budget ran out before the run completed."""
