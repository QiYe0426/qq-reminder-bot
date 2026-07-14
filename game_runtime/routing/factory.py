"""Bridge from a trusted routed envelope to GameEvent."""

from collections.abc import Callable
from uuid import uuid4

from game_runtime.event import GameEvent, GameEventSource, GameEventType
from game_runtime.routing.models import RoutedGameMessageEnvelope


class GameEventFactory:
    def __init__(self, event_id_factory: Callable[[], str] | None = None) -> None:
        self._event_id_factory = event_id_factory or (lambda: str(uuid4()))

    def create_message_received(
        self,
        routed: RoutedGameMessageEnvelope,
    ) -> GameEvent:
        message = routed.message
        return GameEvent(
            event_id=self._event_id_factory(),
            game_id=routed.game_id,
            session_id=routed.session_id,
            event_type=GameEventType.MESSAGE_RECEIVED,
            actor="UNKNOWN",
            source=GameEventSource.PLATFORM,
            timestamp=message.timestamp,
            payload={
                "platform_event_id": message.platform_event_id,
                "sender_id": message.sender_id,
                "message_type": message.message_type.value,
                "observation": dict(message.payload),
                "participant_type": "UNKNOWN",
                "default_decision": "WAIT",
            },
            correlation_id=message.correlation_id,
        )
