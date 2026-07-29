"""Pure Session Control authorization policy for P3-D-3."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantType,
)
from game_runtime.session import GameSessionStatus
from game_runtime.session_control.commands import SessionCommandType
from game_runtime.session_control.resolver import (
    CreateSessionBootstrapContext,
    ExistingSessionContext,
    SessionResolution,
)


class AuthorizationDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


class AuthorizationReason(str, Enum):
    ALLOWED = "ALLOWED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    GLOBAL_GATE_DENIED = "GLOBAL_GATE_DENIED"
    GAME_MODE_PERMISSION_DENIED = "GAME_MODE_PERMISSION_DENIED"
    SESSION_ROLE_DENIED = "SESSION_ROLE_DENIED"
    REQUESTER_MISMATCH = "REQUESTER_MISMATCH"
    SESSION_SCOPE_MISMATCH = "SESSION_SCOPE_MISMATCH"
    INACTIVE_PARTICIPANT = "INACTIVE_PARTICIPANT"
    BINDING_VERSION_MISMATCH = "BINDING_VERSION_MISMATCH"
    COMMAND_CONSTRAINT_DENIED = "COMMAND_CONSTRAINT_DENIED"


class SessionControlPermission(str, Enum):
    CREATE_SESSION = "CREATE_SESSION"
    START_GAME = "START_GAME"
    PAUSE_GAME = "PAUSE_GAME"
    RESUME_GAME = "RESUME_GAME"
    END_GAME = "END_GAME"
    CHANGE_PHASE = "CHANGE_PHASE"
    SET_SCRIPT = "SET_SCRIPT"
    ASSIGN_CHARACTER = "ASSIGN_CHARACTER"
    REPLACE_PLAYER = "REPLACE_PLAYER"


_COMMAND_PERMISSION: dict[SessionCommandType, SessionControlPermission] = {
    SessionCommandType.CREATE_SESSION: SessionControlPermission.CREATE_SESSION,
    SessionCommandType.START_GAME: SessionControlPermission.START_GAME,
    SessionCommandType.PAUSE_GAME: SessionControlPermission.PAUSE_GAME,
    SessionCommandType.RESUME_GAME: SessionControlPermission.RESUME_GAME,
    SessionCommandType.END_GAME: SessionControlPermission.END_GAME,
    SessionCommandType.CHANGE_PHASE: SessionControlPermission.CHANGE_PHASE,
    SessionCommandType.SET_SCRIPT: SessionControlPermission.SET_SCRIPT,
    SessionCommandType.ASSIGN_CHARACTER: SessionControlPermission.ASSIGN_CHARACTER,
    SessionCommandType.REPLACE_PLAYER: SessionControlPermission.REPLACE_PLAYER,
}

ALL_SESSION_CONTROL_PERMISSIONS = frozenset(SessionControlPermission)


@dataclass(frozen=True, slots=True)
class SessionRoleBinding:
    """Read-only, session-scoped participant authorization snapshot."""

    principal_id: str
    game_id: str
    session_id: str
    participant_id: str
    participant_type: ParticipantType
    membership_state: ParticipantMembershipState
    binding_version: int
    granted_permissions: frozenset[SessionControlPermission] = field(
        default_factory=frozenset
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("principal_id", self.principal_id),
            ("game_id", self.game_id),
            ("session_id", self.session_id),
            ("participant_id", self.participant_id),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.participant_type, ParticipantType):
            raise TypeError("participant_type must be a ParticipantType")
        if not isinstance(self.membership_state, ParticipantMembershipState):
            raise TypeError(
                "membership_state must be a ParticipantMembershipState"
            )
        if not isinstance(self.binding_version, int) or isinstance(
            self.binding_version, bool
        ):
            raise TypeError("binding_version must be an integer")
        if self.binding_version < 0:
            raise ValueError("binding_version must not be negative")
        permissions = frozenset(self.granted_permissions)
        if any(
            not isinstance(permission, SessionControlPermission)
            for permission in permissions
        ):
            raise TypeError(
                "granted_permissions must contain SessionControlPermission values"
            )
        object.__setattr__(self, "granted_permissions", permissions)


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Inputs to the four-way Effective Permission intersection."""

    global_gate_allowed: bool
    game_mode_permissions: frozenset[SessionControlPermission]
    role_binding: SessionRoleBinding | None = None
    bootstrap_controller: bool = False
    confirmation_required: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("global_gate_allowed", self.global_gate_allowed),
            ("bootstrap_controller", self.bootstrap_controller),
            ("confirmation_required", self.confirmation_required),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean")
        permissions = frozenset(self.game_mode_permissions)
        if any(
            not isinstance(permission, SessionControlPermission)
            for permission in permissions
        ):
            raise TypeError(
                "game_mode_permissions must contain SessionControlPermission values"
            )
        object.__setattr__(self, "game_mode_permissions", permissions)


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    decision: AuthorizationDecision
    reason: AuthorizationReason
    required_permission: SessionControlPermission

    @property
    def permitted(self) -> bool:
        return self.decision in {
            AuthorizationDecision.ALLOW,
            AuthorizationDecision.REQUIRE_CONFIRMATION,
        }


def required_permission_for(
    command_type: SessionCommandType,
) -> SessionControlPermission:
    if not isinstance(command_type, SessionCommandType):
        raise TypeError("command_type must be a SessionCommandType")
    return _COMMAND_PERMISSION[command_type]


class SessionControlAuthorizationPolicy:
    """Evaluate authorization only; never mutate or execute a command."""

    def authorize(
        self,
        resolution: SessionResolution,
        context: AuthorizationContext,
    ) -> AuthorizationResult:
        if not isinstance(
            resolution,
            (ExistingSessionContext, CreateSessionBootstrapContext),
        ):
            raise TypeError("resolution must be a SessionResolution")
        if not isinstance(context, AuthorizationContext):
            raise TypeError("context must be an AuthorizationContext")

        command = (
            resolution.command
            if isinstance(resolution, ExistingSessionContext)
            else None
        )
        command_type = (
            command.command_type
            if command is not None
            else SessionCommandType.CREATE_SESSION
        )
        required = required_permission_for(command_type)

        if not context.global_gate_allowed:
            return self._deny(required, AuthorizationReason.GLOBAL_GATE_DENIED)
        if required not in context.game_mode_permissions:
            return self._deny(
                required,
                AuthorizationReason.GAME_MODE_PERMISSION_DENIED,
            )

        if isinstance(resolution, CreateSessionBootstrapContext):
            if not context.bootstrap_controller:
                return self._deny(
                    required,
                    AuthorizationReason.SESSION_ROLE_DENIED,
                )
            return self._allow(required, context.confirmation_required)

        role_denial = self._check_session_role(resolution, context, required)
        if role_denial is not None:
            return self._deny(required, role_denial)

        if not self._constraint_allows(resolution):
            return self._deny(
                required,
                AuthorizationReason.COMMAND_CONSTRAINT_DENIED,
            )
        return self._allow(required, context.confirmation_required)

    @staticmethod
    def _check_session_role(
        resolution: ExistingSessionContext,
        context: AuthorizationContext,
        required: SessionControlPermission,
    ) -> AuthorizationReason | None:
        command = resolution.command
        session = resolution.session
        binding = context.role_binding
        if binding is None:
            return AuthorizationReason.SESSION_ROLE_DENIED
        if binding.principal_id != command.requester:
            return AuthorizationReason.REQUESTER_MISMATCH
        if binding.game_id != session.game_id or binding.session_id != session.session_id:
            return AuthorizationReason.SESSION_SCOPE_MISMATCH
        if binding.participant_type is not ParticipantType.DM:
            return AuthorizationReason.SESSION_ROLE_DENIED
        if binding.membership_state is not ParticipantMembershipState.ACTIVE:
            return AuthorizationReason.INACTIVE_PARTICIPANT
        if command.requester_binding_version != binding.binding_version:
            return AuthorizationReason.BINDING_VERSION_MISMATCH
        if required not in binding.granted_permissions:
            return AuthorizationReason.SESSION_ROLE_DENIED
        return None

    @staticmethod
    def _constraint_allows(resolution: ExistingSessionContext) -> bool:
        command_type = resolution.command.command_type
        status = resolution.session.status
        allowed_statuses = {
            SessionCommandType.START_GAME: frozenset({GameSessionStatus.CREATED}),
            SessionCommandType.PAUSE_GAME: frozenset({GameSessionStatus.RUNNING}),
            SessionCommandType.RESUME_GAME: frozenset({GameSessionStatus.PAUSED}),
            SessionCommandType.END_GAME: frozenset(
                {
                    GameSessionStatus.CREATED,
                    GameSessionStatus.RUNNING,
                    GameSessionStatus.PAUSED,
                }
            ),
            SessionCommandType.CHANGE_PHASE: frozenset(
                {GameSessionStatus.RUNNING}
            ),
            SessionCommandType.SET_SCRIPT: frozenset({GameSessionStatus.CREATED}),
            SessionCommandType.ASSIGN_CHARACTER: frozenset(
                {GameSessionStatus.CREATED, GameSessionStatus.PAUSED}
            ),
            SessionCommandType.REPLACE_PLAYER: frozenset(
                {GameSessionStatus.CREATED, GameSessionStatus.PAUSED}
            ),
        }
        return status in allowed_statuses[command_type]

    @staticmethod
    def _deny(
        required: SessionControlPermission,
        reason: AuthorizationReason,
    ) -> AuthorizationResult:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason=reason,
            required_permission=required,
        )

    @staticmethod
    def _allow(
        required: SessionControlPermission,
        confirmation_required: bool,
    ) -> AuthorizationResult:
        if confirmation_required:
            return AuthorizationResult(
                decision=AuthorizationDecision.REQUIRE_CONFIRMATION,
                reason=AuthorizationReason.CONFIRMATION_REQUIRED,
                required_permission=required,
            )
        return AuthorizationResult(
            decision=AuthorizationDecision.ALLOW,
            reason=AuthorizationReason.ALLOWED,
            required_permission=required,
        )
