"""Domain errors, mapped to a stable `{detail, code}` body by an exception handler."""


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, detail: str = "", *, code: str | None = None, extra: dict | None = None):
        super().__init__(detail or self.code)
        self.detail = detail or self.code
        if code:
            self.code = code
        self.extra = extra or {}


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class PermissionDenied(DomainError):
    status_code = 403
    code = "permission_denied"


class PlanLimitExceeded(DomainError):
    status_code = 402
    code = "plan_limit_exceeded"


class SubscriptionLapsed(DomainError):
    status_code = 402
    code = "subscription_lapsed"


class PlatformMaintenance(DomainError):
    status_code = 503
    code = "maintenance"


class FeatureNotInPlan(DomainError):
    status_code = 403
    code = "feature_not_in_plan"


class CapabilityDisabled(DomainError):
    status_code = 403
    code = "capability_disabled"


class RejectEvent(DomainError):
    """Raised inside a sync-event handler: the event is structurally unusable.

    The device keeps it in its outbox as *failed* with this code, so a human can
    retry or discard it — it never silently disappears and never blocks the queue.
    """

    status_code = 422
    code = "event_rejected"
