"""Typed errors. Every governance failure raises one of these; nothing fails silently."""


class CommandCenterError(Exception):
    """Base class for all Command Center errors."""


# --- configuration / registry -------------------------------------------------
class ConfigurationError(CommandCenterError):
    """A configuration file is missing, malformed or internally inconsistent."""


class UnknownAgentError(CommandCenterError):
    """The requested agent_id is not registered."""


class AgentDisabledError(CommandCenterError):
    """The agent exists but is disabled and must not execute."""


class InvalidAgentProfileError(ConfigurationError):
    """An agent profile failed validation; the agent must not execute."""


class UnknownToolError(CommandCenterError):
    """The tool name is not in the canonical tool registry."""


# --- permissions / policy -----------------------------------------------------
class PermissionDeniedError(CommandCenterError):
    """The agent or the human principal lacks permission for the operation."""


class PolicyViolationError(CommandCenterError):
    """The operation violates organization or delegation policy."""


class BudgetExceededError(PolicyViolationError):
    """A cost, token or runtime limit was exceeded."""


# --- state / workflow -----------------------------------------------------------
class InvalidTransitionError(CommandCenterError):
    """A state machine transition is not permitted."""


class NotFoundError(CommandCenterError):
    """A stored record was not found."""


class ConcurrencyError(CommandCenterError):
    """Optimistic-concurrency version mismatch."""


class ApprovalError(CommandCenterError):
    """Base class for approval workflow errors."""


class ApprovalNotExecutableError(ApprovalError):
    """The approval cannot authorize execution (status, expiry, payload or reuse)."""


class PayloadMismatchError(ApprovalNotExecutableError):
    """The action payload differs from the payload that was approved."""


class ValidationFailedError(CommandCenterError):
    """A structured object failed contract validation."""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message if not errors else f"{message}: {'; '.join(errors)}")
        self.errors = list(errors or [])


class AuditIntegrityError(CommandCenterError):
    """The audit log hash chain is broken or a mutation was attempted."""


class AgentProfileMissingError(UnknownAgentError):
    """The agent is on the expected roster but its profile file is missing."""
