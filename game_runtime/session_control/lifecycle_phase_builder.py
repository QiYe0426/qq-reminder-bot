"""Pure Lifecycle / Phase ApplyPlan builder for P3-D-6.6."""

from __future__ import annotations

from enum import Enum

from game_runtime.event import (
    GameEvent,
    GameEventSource,
    GameEventType,
    PhaseChangedPayload,
    SessionControlRejectedPayload,
    SessionEndedPayload,
    SessionPausedPayload,
    SessionResumedPayload,
    SessionStartedPayload,
)
from game_runtime.event.control_payloads import (
    CONTROL_RESULT_SCHEMA_VERSION,
    CONTROL_RESULT_VISIBILITY,
    ControlResultPayload,
    ControlResultPayloadSchemaError,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    ControlApplyPlan,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
    BuildPlanReady,
    BuildReject,
)
from game_runtime.session_control.build_context import ControlApplyBuildContext
from game_runtime.session_control.commands import (
    ChangePhasePayload,
    EndGamePayload,
    PauseGamePayload,
    SessionCommandType,
)
from game_runtime.session_control.lifecycle_evidence import (
    ControlLifecycleEvidenceError,
    ResumeValidationStatus,
    StartReadinessStatus,
)
from game_runtime.session_control.lifecycle_transition import (
    LifecycleTransitionAccepted,
    LifecycleTransitionContractError,
    LifecycleTransitionRejectReason,
    LifecycleTransitionRejected,
    LifecycleTransitionRequest,
    LifecycleTransitionState,
    transition_lifecycle,
)
from game_runtime.session_control.operation import ControlOperationStatus


class LifecyclePhaseRejectReason(str, Enum):
    STALE_STATE_VERSION = "STALE_STATE_VERSION"
    INVALID_LIFECYCLE_TRANSITION = "INVALID_LIFECYCLE_TRANSITION"
    INVALID_PHASE_TRANSITION = "INVALID_PHASE_TRANSITION"
    START_NOT_READY = "START_NOT_READY"
    SESSION_ALREADY_ENDED = "SESSION_ALREADY_ENDED"


_SUPPORTED_COMMANDS = frozenset(
    {
        SessionCommandType.START_GAME,
        SessionCommandType.PAUSE_GAME,
        SessionCommandType.RESUME_GAME,
        SessionCommandType.END_GAME,
        SessionCommandType.CHANGE_PHASE,
    }
)

_LIFECYCLE_COMMANDS = frozenset(
    {
        SessionCommandType.START_GAME,
        SessionCommandType.PAUSE_GAME,
        SessionCommandType.END_GAME,
    }
)

_PHASE_TRANSITIONS: dict[GamePhase, frozenset[GamePhase]] = {
    GamePhase.INTRODUCTION: frozenset({GamePhase.EXPLORATION}),
    GamePhase.EXPLORATION: frozenset({GamePhase.DISCUSSION}),
    GamePhase.DISCUSSION: frozenset(
        {GamePhase.EXPLORATION, GamePhase.VOTING}
    ),
    GamePhase.VOTING: frozenset({GamePhase.DISCUSSION, GamePhase.ENDING}),
    GamePhase.LOBBY: frozenset(),
    GamePhase.ENDING: frozenset(),
}

_SUCCESS_RESULT_CODES: dict[SessionCommandType, str] = {
    SessionCommandType.START_GAME: "START_GAME_APPLIED",
    SessionCommandType.PAUSE_GAME: "PAUSE_GAME_APPLIED",
    SessionCommandType.RESUME_GAME: "RESUME_GAME_APPLIED",
    SessionCommandType.END_GAME: "END_GAME_APPLIED",
    SessionCommandType.CHANGE_PHASE: "CHANGE_PHASE_APPLIED",
}

_LIFECYCLE_RESULT_EVENT_TYPES: dict[
    SessionCommandType, tuple[GameEventType, ...]
] = {
    SessionCommandType.START_GAME: (
        GameEventType.SESSION_STARTED,
        GameEventType.PHASE_CHANGED,
    ),
    SessionCommandType.PAUSE_GAME: (GameEventType.SESSION_PAUSED,),
    SessionCommandType.END_GAME: (GameEventType.SESSION_ENDED,),
}


class LifecyclePhaseControlApplyPlanBuilder:
    """Transform one immutable Actor-turn context into one typed outcome."""

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
            )
        if context.command_intent.intent_schema_version != 1:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED",
            )

        command_type = context.command_intent.command_type
        if command_type not in _SUPPORTED_COMMANDS:
            return _noncommit(
                BuildNonCommitReason.REDUCER_UNAVAILABLE,
                f"{command_type.value}_REDUCER_UNAVAILABLE",
            )

        current_version = context.session_view.state_version
        observed_version = context.envelope.observed_state_version
        if current_version < observed_version:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "COMMAND_VERSION_AHEAD_OF_ACTOR",
            )
        if current_version > observed_version:
            return self._reject(context, LifecyclePhaseRejectReason.STALE_STATE_VERSION)

        try:
            context.lifecycle_evidence.validate_for_command(
                command_type,
                game_id=context.session_view.game_id,
                session_id=context.session_view.session_id,
                observed_state_version=current_version,
            )
        except ControlLifecycleEvidenceError:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                f"{command_type.value}_EVIDENCE_INCOMPLETE",
            )

        reducers = {
            SessionCommandType.START_GAME: self._build_start,
            SessionCommandType.PAUSE_GAME: self._build_pause,
            SessionCommandType.RESUME_GAME: self._build_resume,
            SessionCommandType.END_GAME: self._build_end,
            SessionCommandType.CHANGE_PHASE: self._build_change_phase,
        }
        return reducers[command_type](context)

    def _build_start(self, context: ControlApplyBuildContext) -> BuildOutcome:
        session = context.session_view
        readiness = context.lifecycle_evidence.start_readiness
        visibility = context.lifecycle_evidence.phase_visibility_intent
        if readiness is None or visibility is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "START_GAME_EVIDENCE_INCOMPLETE",
            )
        decision = _lifecycle_transition(context)
        if isinstance(decision, BuildNonCommit):
            return decision
        if isinstance(decision, LifecycleTransitionRejected):
            return self._reject(context, _map_transition_rejection(decision))
        if readiness.readiness_status is StartReadinessStatus.UNKNOWN:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "START_GAME_READINESS_UNKNOWN",
            )
        if readiness.readiness_status is StartReadinessStatus.NOT_READY:
            return self._reject(
                context,
                LifecyclePhaseRejectReason.START_NOT_READY,
            )
        setup = context.setup_view
        if (
            setup is None
            or readiness.setup_manifest_reference != setup.manifest_reference
            or readiness.setup_manifest_version != setup.setup_version
            or visibility.previous_phase is not GamePhase.LOBBY
            or visibility.target_phase is not GamePhase.INTRODUCTION
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "START_GAME_EVIDENCE_BINDING_MISMATCH",
            )
        ownership = context.ownership_evidence
        if ownership.active_generation is not None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "START_GAME_OWNERSHIP_CONFLICT",
            )

        generation = ownership.last_allocated_generation + 1
        result_state_version = session.state_version + 1
        events = (
            self._result_event(
                context,
                event_type=GameEventType.SESSION_STARTED,
                ordinal=1,
                payload=SessionStartedPayload(
                    **_common_payload(context, result_state_version),
                    previous_status=decision.previous_state.lifecycle_status,
                    current_status=decision.resulting_state.lifecycle_status,
                    ownership_generation=generation,
                ),
            ),
            self._result_event(
                context,
                event_type=GameEventType.PHASE_CHANGED,
                ordinal=2,
                payload=PhaseChangedPayload(
                    **_common_payload(context, result_state_version),
                    previous_phase=decision.previous_state.phase,
                    current_phase=decision.resulting_state.phase,
                ),
            ),
        )
        return _compose_lifecycle_apply_plan(
            context=context,
            transition=decision,
            ownership_intent=OwnershipIntent(
                intent_type=OwnershipIntentType.ACQUIRE,
                expected_generation=None,
                resulting_generation=generation,
            ),
            result_events=events,
        )

    def _build_pause(self, context: ControlApplyBuildContext) -> BuildOutcome:
        session = context.session_view
        decision = _lifecycle_transition(context)
        if isinstance(decision, BuildNonCommit):
            return decision
        if isinstance(decision, LifecycleTransitionRejected):
            return self._reject(context, _map_transition_rejection(decision))
        generation = context.ownership_evidence.active_generation
        if generation is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "PAUSE_GAME_OWNERSHIP_MISSING",
            )
        payload = context.command_intent.payload
        if not isinstance(payload, PauseGamePayload):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "PAUSE_GAME_PAYLOAD_INVALID",
            )
        result_state_version = session.state_version + 1
        event = self._result_event(
            context,
            event_type=GameEventType.SESSION_PAUSED,
            ordinal=1,
            payload=SessionPausedPayload(
                **_common_payload(context, result_state_version),
                previous_status=decision.previous_state.lifecycle_status,
                current_status=decision.resulting_state.lifecycle_status,
                reason_code=payload.reason_code,
            ),
        )
        return _compose_lifecycle_apply_plan(
            context=context,
            transition=decision,
            ownership_intent=_retain(generation),
            result_events=(event,),
        )

    def _build_resume(self, context: ControlApplyBuildContext) -> BuildOutcome:
        session = context.session_view
        evidence = context.lifecycle_evidence.resume_validation
        if evidence is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "RESUME_GAME_EVIDENCE_INCOMPLETE",
            )
        if session.status is not GameSessionStatus.PAUSED:
            return self._reject(
                context,
                LifecyclePhaseRejectReason.INVALID_LIFECYCLE_TRANSITION,
            )
        if evidence.validation_status is not ResumeValidationStatus.READY:
            return _noncommit(
                BuildNonCommitReason.RECOVERY_REQUIRED,
                "RESUME_GAME_RECOVERY_REQUIRED",
            )
        generation = context.ownership_evidence.active_generation
        if (
            generation is None
            or evidence.ownership_generation != generation
            or evidence.validated_cursor != session.last_applied_sequence_no
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "RESUME_GAME_EVIDENCE_BINDING_MISMATCH",
            )
        candidate = _candidate(
            context,
            status=GameSessionStatus.RUNNING,
            phase=session.current_phase,
        )
        event = self._result_event(
            context,
            event_type=GameEventType.SESSION_RESUMED,
            ordinal=1,
            payload=SessionResumedPayload(
                **_common_payload(context, candidate.state_version),
                previous_status=GameSessionStatus.PAUSED,
                current_status=GameSessionStatus.RUNNING,
                ownership_generation=generation,
            ),
        )
        return self._ready(
            context,
            candidate,
            _retain(generation),
            (event,),
        )

    def _build_end(self, context: ControlApplyBuildContext) -> BuildOutcome:
        session = context.session_view
        evidence = context.lifecycle_evidence.end_retention
        if evidence is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "END_GAME_EVIDENCE_INCOMPLETE",
            )
        decision = _lifecycle_transition(context)
        if isinstance(decision, BuildNonCommit):
            return decision
        if isinstance(decision, LifecycleTransitionRejected):
            return self._reject(context, _map_transition_rejection(decision))
        payload = context.command_intent.payload
        if (
            not isinstance(payload, EndGamePayload)
            or evidence.public_result_reference != payload.public_result_reference
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "END_GAME_RETENTION_BINDING_MISMATCH",
            )

        generation = context.ownership_evidence.active_generation
        if session.status is GameSessionStatus.CREATED:
            if generation is not None:
                return _noncommit(
                    BuildNonCommitReason.EVIDENCE_MISMATCH,
                    "END_GAME_CREATED_OWNERSHIP_CONFLICT",
                )
            ownership_intent = OwnershipIntent(
                intent_type=OwnershipIntentType.UNCHANGED,
                expected_generation=None,
                resulting_generation=None,
            )
        else:
            if generation is None:
                return _noncommit(
                    BuildNonCommitReason.EVIDENCE_MISMATCH,
                    "END_GAME_OWNERSHIP_MISSING",
                )
            ownership_intent = OwnershipIntent(
                intent_type=OwnershipIntentType.RELEASE,
                expected_generation=generation,
                resulting_generation=None,
            )

        result_state_version = session.state_version + 1
        event = self._result_event(
            context,
            event_type=GameEventType.SESSION_ENDED,
            ordinal=1,
            payload=SessionEndedPayload(
                **_common_payload(context, result_state_version),
                previous_status=decision.previous_state.lifecycle_status,
                current_status=decision.resulting_state.lifecycle_status,
                retention_reference=evidence.retention_reference,
            ),
        )
        return _compose_lifecycle_apply_plan(
            context=context,
            transition=decision,
            ownership_intent=ownership_intent,
            result_events=(event,),
        )

    def _build_change_phase(
        self, context: ControlApplyBuildContext
    ) -> BuildOutcome:
        session = context.session_view
        visibility = context.lifecycle_evidence.phase_visibility_intent
        if visibility is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_EVIDENCE_INCOMPLETE",
            )
        if session.status is not GameSessionStatus.RUNNING:
            return self._reject(
                context,
                LifecyclePhaseRejectReason.INVALID_LIFECYCLE_TRANSITION,
            )
        payload = context.command_intent.payload
        if not isinstance(payload, ChangePhasePayload):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CHANGE_PHASE_PAYLOAD_INVALID",
            )
        if payload.target_phase not in _PHASE_TRANSITIONS[session.current_phase]:
            return self._reject(
                context,
                LifecyclePhaseRejectReason.INVALID_PHASE_TRANSITION,
            )
        if (
            visibility.previous_phase is not session.current_phase
            or visibility.target_phase is not payload.target_phase
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_VISIBILITY_BINDING_MISMATCH",
            )
        generation = context.ownership_evidence.active_generation
        if generation is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_OWNERSHIP_MISSING",
            )

        candidate = _candidate(
            context,
            status=GameSessionStatus.RUNNING,
            phase=payload.target_phase,
        )
        event = self._result_event(
            context,
            event_type=GameEventType.PHASE_CHANGED,
            ordinal=1,
            payload=PhaseChangedPayload(
                **_common_payload(context, candidate.state_version),
                previous_phase=session.current_phase,
                current_phase=payload.target_phase,
            ),
        )
        return self._ready(
            context,
            candidate,
            _retain(generation),
            (event,),
        )

    def _ready(
        self,
        context: ControlApplyBuildContext,
        candidate: CandidateSessionSnapshot,
        ownership_intent: OwnershipIntent,
        events: tuple[GameEvent, ...],
    ) -> BuildPlanReady:
        session = context.session_view
        envelope = context.envelope
        return BuildPlanReady(
            plan=ControlApplyPlan(
                game_id=session.game_id,
                session_id=session.session_id,
                group_id=session.group_id,
                command_id=envelope.command_id,
                command_type=context.command_intent.command_type,
                operation_id=envelope.operation_id,
                operation_claim_id=context.claim.claim_id,
                input_event_id=envelope.event.event_id,
                input_sequence_no=envelope.event_sequence_no,
                expected_state_version=session.state_version,
                expected_cursor=session.last_applied_sequence_no,
                expected_binding_version=envelope.requester_binding_version,
                candidate_snapshot=candidate,
                participant_mutations=(),
                setup_mutations=(),
                ownership_intent=ownership_intent,
                result_events=events,
                operation_terminal_state=ControlOperationStatus.SUCCESS,
                lifecycle_evidence=context.lifecycle_evidence,
            )
        )

    def _reject(
        self,
        context: ControlApplyBuildContext,
        reason: LifecyclePhaseRejectReason,
    ) -> BuildReject:
        session = context.session_view
        envelope = context.envelope
        payload = SessionControlRejectedPayload(
            command_id=envelope.command_id,
            operation_id=envelope.operation_id,
            input_event_id=envelope.event.event_id,
            result_code=reason.value,
            result_state_version=session.state_version,
            reason_code=reason.value,
            state_version=session.state_version,
        )
        event = self._result_event(
            context,
            event_type=GameEventType.SESSION_CONTROL_REJECTED,
            ordinal=1,
            payload=payload,
        )
        return BuildReject(
            plan=ControlRejectPlan(
                game_id=session.game_id,
                session_id=session.session_id,
                group_id=session.group_id,
                command_id=envelope.command_id,
                command_type=context.command_intent.command_type,
                operation_id=envelope.operation_id,
                operation_claim_id=context.claim.claim_id,
                input_event_id=envelope.event.event_id,
                input_sequence_no=envelope.event_sequence_no,
                expected_state_version=session.state_version,
                expected_cursor=session.last_applied_sequence_no,
                expected_binding_version=envelope.requester_binding_version,
                rejection_event=event,
                operation_terminal_state=ControlOperationStatus.FAILED,
            )
        )

    @staticmethod
    def _result_event(
        context: ControlApplyBuildContext,
        *,
        event_type: GameEventType,
        ordinal: int,
        payload: ControlResultPayload,
    ) -> GameEvent:
        seed = context.result_event_seed
        return GameEvent(
            event_id=seed.derive_event_id(event_type, ordinal=ordinal),
            game_id=seed.game_id,
            session_id=seed.session_id,
            event_type=event_type,
            actor="session-actor",
            source=GameEventSource.CONTROL,
            correlation_id=seed.correlation_id,
            timestamp=seed.timestamp,
            payload=payload.to_mapping(),
            schema_version=CONTROL_RESULT_SCHEMA_VERSION,
            visibility=CONTROL_RESULT_VISIBILITY[event_type],
            observed_state_version=context.session_view.state_version,
            causation_event_id=context.envelope.event.event_id,
        )


class LifecycleControlApplyPlanBuilder(LifecyclePhaseControlApplyPlanBuilder):
    """Build only frozen Start, Pause, and End lifecycle control plans."""

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return super().build(context)
        if context.command_intent.intent_schema_version != 1:
            return super().build(context)
        command_type = context.command_intent.command_type
        if command_type not in _LIFECYCLE_COMMANDS:
            return _noncommit(
                BuildNonCommitReason.REDUCER_UNAVAILABLE,
                f"{command_type.value}_REDUCER_UNAVAILABLE",
            )
        return super().build(context)


def _compose_lifecycle_apply_plan(
    *,
    context: ControlApplyBuildContext,
    transition: LifecycleTransitionAccepted,
    ownership_intent: OwnershipIntent,
    result_events: tuple[GameEvent, ...],
) -> BuildPlanReady | BuildNonCommit:
    """Compose one complete lifecycle plan or fail closed without a plan."""

    if not isinstance(context, ControlApplyBuildContext):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_CONTEXT_INVALID",
        )
    if not isinstance(transition, LifecycleTransitionAccepted):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_TRANSITION_INVALID",
        )
    if not isinstance(ownership_intent, OwnershipIntent):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "LIFECYCLE_COMPOSITION_OWNERSHIP_INVALID",
        )
    if not isinstance(result_events, tuple) or any(
        not isinstance(event, GameEvent) for event in result_events
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_EVENTS_INVALID",
        )

    session = context.session_view
    envelope = context.envelope
    operation = context.command_intent.command_type
    if operation not in _LIFECYCLE_RESULT_EVENT_TYPES:
        return _noncommit(
            BuildNonCommitReason.REDUCER_UNAVAILABLE,
            "LIFECYCLE_COMPOSITION_OPERATION_UNSUPPORTED",
        )

    try:
        expected_transition = transition_lifecycle(
            LifecycleTransitionRequest(
                operation=operation,
                current_state=LifecycleTransitionState(
                    lifecycle_status=session.status,
                    phase=session.current_phase,
                ),
            )
        )
    except LifecycleTransitionContractError:
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_TRANSITION_INVALID",
        )
    if transition != expected_transition:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "LIFECYCLE_COMPOSITION_TRANSITION_MISMATCH",
        )

    claim = context.claim
    seed = context.result_event_seed
    input_event = envelope.event
    if (
        (session.game_id, session.session_id)
        != (input_event.game_id, input_event.session_id)
        or (
            claim.game_id,
            claim.session_id,
            claim.command_id,
            claim.operation_id,
            claim.input_event_id,
        )
        != (
            session.game_id,
            session.session_id,
            envelope.command_id,
            envelope.operation_id,
            input_event.event_id,
        )
        or (
            seed.game_id,
            seed.session_id,
            seed.command_id,
            seed.operation_id,
            seed.input_event_id,
        )
        != (
            session.game_id,
            session.session_id,
            envelope.command_id,
            envelope.operation_id,
            input_event.event_id,
        )
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "LIFECYCLE_COMPOSITION_IDENTITY_MISMATCH",
        )
    if (
        envelope.observed_state_version != session.state_version
        or context.ownership_evidence.observed_state_version
        != session.state_version
        or envelope.event_sequence_no
        != session.last_applied_sequence_no + 1
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "LIFECYCLE_COMPOSITION_VERSION_CURSOR_MISMATCH",
        )

    try:
        context.lifecycle_evidence.validate_for_command(
            operation,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
    except ControlLifecycleEvidenceError:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "LIFECYCLE_COMPOSITION_EVIDENCE_INVALID",
        )
    if not _valid_lifecycle_ownership(
        context=context,
        transition=transition,
        ownership_intent=ownership_intent,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "LIFECYCLE_COMPOSITION_OWNERSHIP_INVALID",
        )

    candidate_state_version = session.state_version + 1
    if not _valid_lifecycle_result_events(
        context=context,
        result_events=result_events,
        result_state_version=candidate_state_version,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_EVENTS_INVALID",
        )

    try:
        candidate = CandidateSessionSnapshot(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            dm_participant_id=session.dm_participant_id,
            status=transition.resulting_state.lifecycle_status,
            current_phase=transition.resulting_state.phase,
            state_version=candidate_state_version,
            last_applied_sequence_no=envelope.event_sequence_no,
        )
        plan = ControlApplyPlan(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            command_id=envelope.command_id,
            command_type=operation,
            operation_id=envelope.operation_id,
            operation_claim_id=claim.claim_id,
            input_event_id=input_event.event_id,
            input_sequence_no=envelope.event_sequence_no,
            expected_state_version=session.state_version,
            expected_cursor=session.last_applied_sequence_no,
            expected_binding_version=envelope.requester_binding_version,
            candidate_snapshot=candidate,
            participant_mutations=(),
            setup_mutations=(),
            ownership_intent=ownership_intent,
            result_events=result_events,
            operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=context.lifecycle_evidence,
        )
    except (TypeError, ValueError):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_PLAN_INVALID",
        )

    if (
        plan.candidate_snapshot is not candidate
        or plan.ownership_intent is not ownership_intent
        or plan.lifecycle_evidence is not context.lifecycle_evidence
        or plan.result_events != result_events
        or plan.operation_id != envelope.operation_id
        or plan.expected_state_version != session.state_version
        or plan.expected_cursor != session.last_applied_sequence_no
        or candidate.state_version != candidate_state_version
        or candidate.last_applied_sequence_no != envelope.event_sequence_no
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_COMPOSITION_PLAN_INCOMPLETE",
        )
    return BuildPlanReady(plan=plan)


def _valid_lifecycle_ownership(
    *,
    context: ControlApplyBuildContext,
    transition: LifecycleTransitionAccepted,
    ownership_intent: OwnershipIntent,
) -> bool:
    operation = transition.operation
    ownership = context.ownership_evidence
    active_generation = ownership.active_generation
    if operation is SessionCommandType.START_GAME:
        expected = OwnershipIntent(
            intent_type=OwnershipIntentType.ACQUIRE,
            expected_generation=None,
            resulting_generation=ownership.last_allocated_generation + 1,
        )
        return active_generation is None and ownership_intent == expected
    if operation is SessionCommandType.PAUSE_GAME:
        return (
            active_generation is not None
            and ownership_intent == _retain(active_generation)
        )
    if transition.previous_state.lifecycle_status is GameSessionStatus.CREATED:
        expected = OwnershipIntent(
            intent_type=OwnershipIntentType.UNCHANGED,
            expected_generation=None,
            resulting_generation=None,
        )
        return active_generation is None and ownership_intent == expected
    if active_generation is None:
        return False
    expected = OwnershipIntent(
        intent_type=OwnershipIntentType.RELEASE,
        expected_generation=active_generation,
        resulting_generation=None,
    )
    return ownership_intent == expected


def _valid_lifecycle_result_events(
    *,
    context: ControlApplyBuildContext,
    result_events: tuple[GameEvent, ...],
    result_state_version: int,
) -> bool:
    operation = context.command_intent.command_type
    expected_types = _LIFECYCLE_RESULT_EVENT_TYPES[operation]
    if tuple(event.event_type for event in result_events) != expected_types:
        return False
    session = context.session_view
    envelope = context.envelope
    seed = context.result_event_seed
    for ordinal, event in enumerate(result_events, start=1):
        try:
            payload = validate_control_result_event(event)
        except (TypeError, ValueError, ControlResultPayloadSchemaError):
            return False
        if (
            event.event_id
            != seed.derive_event_id(event.event_type, ordinal=ordinal)
            or (event.game_id, event.session_id)
            != (session.game_id, session.session_id)
            or event.source is not GameEventSource.CONTROL
            or event.correlation_id != seed.correlation_id
            or event.timestamp != seed.timestamp
            or event.causation_event_id != envelope.event.event_id
            or event.observed_state_version != session.state_version
            or payload.command_id != envelope.command_id
            or payload.operation_id != envelope.operation_id
            or payload.input_event_id != envelope.event.event_id
            or payload.result_code != _SUCCESS_RESULT_CODES[operation]
            or payload.result_state_version != result_state_version
        ):
            return False
    return True


def _candidate(
    context: ControlApplyBuildContext,
    *,
    status: GameSessionStatus,
    phase: GamePhase,
) -> CandidateSessionSnapshot:
    session = context.session_view
    return CandidateSessionSnapshot(
        game_id=session.game_id,
        session_id=session.session_id,
        group_id=session.group_id,
        dm_participant_id=session.dm_participant_id,
        status=status,
        current_phase=phase,
        state_version=session.state_version + 1,
        last_applied_sequence_no=context.envelope.event_sequence_no,
    )


def _lifecycle_transition(
    context: ControlApplyBuildContext,
) -> LifecycleTransitionAccepted | LifecycleTransitionRejected | BuildNonCommit:
    session = context.session_view
    try:
        request = LifecycleTransitionRequest(
            operation=context.command_intent.command_type,
            current_state=LifecycleTransitionState(
                lifecycle_status=session.status,
                phase=session.current_phase,
            ),
        )
        return transition_lifecycle(request)
    except LifecycleTransitionContractError:
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "LIFECYCLE_TRANSITION_CONTEXT_INVALID",
        )


def _map_transition_rejection(
    decision: LifecycleTransitionRejected,
) -> LifecyclePhaseRejectReason:
    mapping = {
        LifecycleTransitionRejectReason.INVALID_LIFECYCLE_TRANSITION:
            LifecyclePhaseRejectReason.INVALID_LIFECYCLE_TRANSITION,
        LifecycleTransitionRejectReason.SESSION_ALREADY_ENDED:
            LifecyclePhaseRejectReason.SESSION_ALREADY_ENDED,
    }
    return mapping[decision.reason]


def _retain(generation: int) -> OwnershipIntent:
    return OwnershipIntent(
        intent_type=OwnershipIntentType.RETAIN,
        expected_generation=generation,
        resulting_generation=generation,
    )


def _common_payload(
    context: ControlApplyBuildContext,
    result_state_version: int,
) -> dict[str, object]:
    return {
        "command_id": context.envelope.command_id,
        "operation_id": context.envelope.operation_id,
        "input_event_id": context.envelope.event.event_id,
        "result_code": _SUCCESS_RESULT_CODES[context.command_intent.command_type],
        "result_state_version": result_state_version,
    }


def _noncommit(
    reason: BuildNonCommitReason,
    detail_code: str,
) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
