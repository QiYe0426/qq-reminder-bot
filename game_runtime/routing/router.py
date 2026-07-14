"""Pure priority-based Mode Router for P3-B."""

from game_runtime.routing.contracts import GlobalControlChecker, SessionRegistry
from game_runtime.routing.models import (
    GlobalControlResult,
    IngressMessageEnvelope,
    RouteDecision,
    RouteDestination,
    RouteFailureReason,
    SessionLookupStatus,
    SessionOwnershipSnapshot,
)


class NoGlobalControlChecker:
    """Default checker for tests where no global control command exists."""

    def check(self, message: IngressMessageEnvelope) -> GlobalControlResult:
        del message
        return GlobalControlResult.NOT_CONTROL


class ModeRouter:
    """Select exactly one destination without invoking any business handler."""

    def __init__(
        self,
        registry: SessionRegistry,
        *,
        global_control: GlobalControlChecker | None = None,
    ) -> None:
        self._registry = registry
        self._global_control = global_control or NoGlobalControlChecker()

    def route(self, message: IngressMessageEnvelope) -> RouteDecision:
        control_decision = self._check_global_control(message)
        if control_decision is not None:
            return control_decision

        try:
            snapshot = self._registry.lookup(message.group_id)
        except Exception:
            return RouteDecision(
                destination=RouteDestination.REJECT,
                reason=RouteFailureReason.REGISTRY_ERROR,
            )

        if not isinstance(snapshot, SessionOwnershipSnapshot):
            return RouteDecision(
                destination=RouteDestination.REJECT,
                reason=RouteFailureReason.INVALID_REGISTRY_RESULT,
            )
        if snapshot.group_id != message.group_id:
            return RouteDecision(
                destination=RouteDestination.REJECT,
                reason=RouteFailureReason.INVALID_REGISTRY_RESULT,
            )
        if snapshot.status in {
            SessionLookupStatus.RUNNING,
            SessionLookupStatus.PAUSED,
        }:
            return RouteDecision(
                destination=RouteDestination.GAME,
                session=snapshot,
            )
        if snapshot.status in {
            SessionLookupStatus.NO_SESSION,
            SessionLookupStatus.ENDED,
        }:
            return RouteDecision(
                destination=RouteDestination.NORMAL,
                session=snapshot,
            )
        return RouteDecision(
            destination=RouteDestination.REJECT,
            reason=RouteFailureReason.INVALID_REGISTRY_RESULT,
        )

    def _check_global_control(
        self,
        message: IngressMessageEnvelope,
    ) -> RouteDecision | None:
        try:
            result = self._global_control.check(message)
        except Exception:
            return RouteDecision(
                destination=RouteDestination.REJECT,
                reason=RouteFailureReason.GLOBAL_CONTROL_ERROR,
            )
        if result is GlobalControlResult.NOT_CONTROL:
            return None
        if result is GlobalControlResult.AUTHORIZED:
            return RouteDecision(destination=RouteDestination.GLOBAL_CONTROL)
        if result is GlobalControlResult.DENIED:
            return RouteDecision(
                destination=RouteDestination.REJECT,
                reason=RouteFailureReason.GLOBAL_CONTROL_DENIED,
            )
        return RouteDecision(
            destination=RouteDestination.REJECT,
            reason=RouteFailureReason.GLOBAL_CONTROL_ERROR,
        )
