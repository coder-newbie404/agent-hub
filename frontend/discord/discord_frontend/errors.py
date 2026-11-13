"""Hub errors translated from HTTP responses.

The in-process bot caught hub-domain exceptions (NotFound, RunInProgress,
AgentOffline); over the API they arrive as HTTP statuses with a machine
`code` — these classes restore the same catch sites."""
class HubError(Exception):
    """Base: any hub API failure without a dedicated class."""


class NotFound(HubError):
    """404 / not_found."""


class RunInProgress(HubError):
    """409 / run_in_progress."""


class AgentOffline(HubError):
    """409 / agent_offline."""


class AgentFailed(HubError):
    """502 / agent_failed — the agent rejected the operation."""
