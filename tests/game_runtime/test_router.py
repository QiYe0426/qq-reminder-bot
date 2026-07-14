from datetime import datetime, timezone

from game_runtime.actor import GameSessionActor
from game_runtime.routing import (
    ActorGameRuntimeIngress,
    GameModeRouterAdapter,
    GlobalControlResult,
    InMemorySessionRegistry,
    IngressMessageEnvelope,
    MessageType,
    ModeRouter,
    RouteDestination,
    RouteFailureReason,
)
from game_runtime.session import GameSessionStatus


def make_message(group_id: str = "20001") -> IngressMessageEnvelope:
    return IngressMessageEnvelope(
        platform_event_id=f"event-{group_id}",
        group_id=group_id,
        sender_id="unknown-user",
        timestamp=datetime(2026, 7, 15, tzinfo=timezone.utc),
        message_type=MessageType.GROUP_TEXT,
        correlation_id=f"correlation-{group_id}",
        payload={"text": "observation only"},
    )


def build_adapter(registry: object) -> tuple[GameModeRouterAdapter, ActorGameRuntimeIngress]:
    ingress = ActorGameRuntimeIngress()
    return GameModeRouterAdapter(ModeRouter(registry), ingress), ingress


def test_no_session_routes_to_normal() -> None:
    adapter, _ingress = build_adapter(InMemorySessionRegistry())

    result = adapter.handle(make_message())

    assert result.decision.destination is RouteDestination.NORMAL
    assert result.game_event is None
    assert not result.delivered


def test_running_session_routes_to_game_and_creates_event(session_factory) -> None:
    session = session_factory()
    session.transition_to(GameSessionStatus.RUNNING)
    registry = InMemorySessionRegistry()
    registry.bind_session(session)
    adapter, ingress = build_adapter(registry)
    actor = GameSessionActor(session)
    ingress.register(actor)

    result = adapter.handle(make_message())

    assert result.decision.destination is RouteDestination.GAME
    assert result.delivered
    assert result.game_event is not None
    assert result.game_event.game_id == session.game_id
    assert result.game_event.session_id == session.session_id
    assert result.game_event.actor == "UNKNOWN"
    assert result.game_event.payload["sender_id"] == "unknown-user"
    assert result.game_event.payload["default_decision"] == "WAIT"
    assert actor.processed_event_ids == (result.game_event.event_id,)


def test_paused_session_remains_game_owned(session_factory) -> None:
    session = session_factory()
    session.transition_to(GameSessionStatus.RUNNING)
    session.transition_to(GameSessionStatus.PAUSED)
    registry = InMemorySessionRegistry()
    registry.bind_session(session)
    adapter, ingress = build_adapter(registry)
    ingress.register(GameSessionActor(session))

    result = adapter.handle(make_message())

    assert result.decision.destination is RouteDestination.GAME
    assert result.delivered


def test_groups_are_routed_independently(session_factory) -> None:
    game_session = session_factory("game-a", "group-a")
    game_session.transition_to(GameSessionStatus.RUNNING)
    registry = InMemorySessionRegistry()
    registry.bind_session(game_session)
    adapter, ingress = build_adapter(registry)
    ingress.register(GameSessionActor(game_session))

    game_result = adapter.handle(make_message("group-a"))
    normal_result = adapter.handle(make_message("group-b"))

    assert game_result.decision.destination is RouteDestination.GAME
    assert normal_result.decision.destination is RouteDestination.NORMAL
    assert normal_result.game_event is None


def test_registry_failure_is_fail_closed() -> None:
    class FailingRegistry:
        def lookup(self, group_id: str):
            del group_id
            raise RuntimeError("registry unavailable")

    adapter, _ingress = build_adapter(FailingRegistry())

    result = adapter.handle(make_message())

    assert result.decision.destination is RouteDestination.REJECT
    assert result.decision.reason is RouteFailureReason.REGISTRY_ERROR
    assert result.game_event is None


def test_missing_actor_does_not_fall_back_to_normal(session_factory) -> None:
    session = session_factory()
    session.transition_to(GameSessionStatus.RUNNING)
    registry = InMemorySessionRegistry()
    registry.bind_session(session)
    adapter, _ingress = build_adapter(registry)

    result = adapter.handle(make_message())

    assert result.decision.destination is RouteDestination.REJECT
    assert result.decision.reason is RouteFailureReason.GAME_RUNTIME_UNAVAILABLE
    assert result.game_event is not None


def test_ended_session_releases_route_to_normal(session_factory) -> None:
    session = session_factory()
    session.transition_to(GameSessionStatus.ENDED)
    registry = InMemorySessionRegistry()
    registry.bind_session(session)
    adapter, _ingress = build_adapter(registry)

    result = adapter.handle(make_message())

    assert result.decision.destination is RouteDestination.NORMAL
    assert result.game_event is None


def test_global_control_has_priority_over_registry() -> None:
    class AuthorizedGlobalControl:
        def check(self, message: IngressMessageEnvelope) -> GlobalControlResult:
            del message
            return GlobalControlResult.AUTHORIZED

    class MustNotBeCalledRegistry:
        def lookup(self, group_id: str):
            raise AssertionError(f"registry unexpectedly called for {group_id}")

    router = ModeRouter(
        MustNotBeCalledRegistry(),
        global_control=AuthorizedGlobalControl(),
    )

    decision = router.route(make_message())

    assert decision.destination is RouteDestination.GLOBAL_CONTROL
