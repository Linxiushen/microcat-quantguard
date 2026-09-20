class QuantGuardError(Exception):
    """A safe-to-report, categorized operational failure."""
    code = "agent_error"


class ConfigurationError(QuantGuardError):
    code = "configuration_error"


class BudgetExceeded(QuantGuardError):
    code = "budget_exhausted"


class ModelError(QuantGuardError):
    code = "model_error"


class TaskError(QuantGuardError):
    code = "task_error"


class CandidateError(QuantGuardError):
    code = "candidate_error"


class SandboxError(QuantGuardError):
    code = "sandbox_unavailable"
