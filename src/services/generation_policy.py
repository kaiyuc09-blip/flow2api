"""Task-local submission policy for jobs without upstream idempotency support."""
from contextvars import ContextVar, Token


_no_submit_retry: ContextVar[bool] = ContextVar("flow_no_submit_retry", default=False)


def set_no_submit_retry(enabled: bool = True) -> Token:
    return _no_submit_retry.set(bool(enabled))


def reset_no_submit_retry(token: Token) -> None:
    _no_submit_retry.reset(token)


def no_submit_retry() -> bool:
    return _no_submit_retry.get()


def submission_attempts(configured: int) -> int:
    return 1 if no_submit_retry() else max(1, int(configured or 1))


class GenerationOutcomeUnknown(RuntimeError):
    """A mutation may have been accepted; the caller must not automatically retry."""

    outcome_unknown = True


def raise_if_submission_uncertain(error: Exception, *, submitted: bool) -> None:
    if getattr(error, "outcome_unknown", False):
        raise error
    if no_submit_retry() and submitted:
        # Explicit protocol rejections confirm that generation was not accepted.
        message = str(error)
        if "Flow frontend RPC rejected:" in message or "MODEL_ACCESS_DENIED" in message:
            return
        raise GenerationOutcomeUnknown(
            "Upstream submission outcome is unknown; automatic resubmission was stopped"
        ) from error
