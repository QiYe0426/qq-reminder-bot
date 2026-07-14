"""Orchestrates routing, GameEvent creation, and actor delivery."""

from game_runtime.routing.contracts import GameRuntimeIngress
from game_runtime.routing.factory import GameEventFactory
from game_runtime.routing.models import (
    IngressMessageEnvelope,
    RouteDecision,
    RouteDestination,
    RouteFailureReason,
    RoutedGameMessageEnvelope,
    RouterAdapterResult,
)
from game_runtime.routing.router import ModeRouter


class GameModeRouterAdapter:
    """Platform-neutral adapter; it is not registered with production ingress."""

    def __init__(
        self,
        router: ModeRouter,
        game_runtime: GameRuntimeIngress,
        *,
        event_factory: GameEventFactory | None = None,
    ) -> None:
        self._router = router
        self._game_runtime = game_runtime
        self._event_factory = event_factory or GameEventFactory()

    def handle(self, message: IngressMessageEnvelope) -> RouterAdapterResult:
        decision = self._router.route(message)
        if decision.destination is not RouteDestination.GAME:
            return RouterAdapterResult(decision=decision)

        snapshot = decision.session
        if snapshot is None or snapshot.game_id is None or snapshot.session_id is None:
            return RouterAdapterResult(
                decision=RouteDecision(
                    destination=RouteDestination.REJECT,
                    reason=RouteFailureReason.INVALID_REGISTRY_RESULT,
                )
            )

        try:
            routed = RoutedGameMessageEnvelope(
                message=message,
                game_id=snapshot.game_id,
                session_id=snapshot.session_id,
                session_status=snapshot.status,
            )
            event = self._event_factory.create_message_received(routed)
        except (TypeError, ValueError):
            return RouterAdapterResult(
                decision=RouteDecision(
                    destination=RouteDestination.REJECT,
                    reason=RouteFailureReason.GAME_EVENT_ERROR,
                )
            )

        try:
            delivered = self._game_runtime.accept(event)
        except Exception:
            delivered = False
        if not delivered:
            return RouterAdapterResult(
                decision=RouteDecision(
                    destination=RouteDestination.REJECT,
                    reason=RouteFailureReason.GAME_RUNTIME_UNAVAILABLE,
                ),
                game_event=event,
            )
        return RouterAdapterResult(
            decision=decision,
            game_event=event,
            delivered=True,
        )
