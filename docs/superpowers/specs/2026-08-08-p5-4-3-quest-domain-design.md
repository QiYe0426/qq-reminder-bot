# P5-4.3 Quest Domain Design and Implementation Freeze

## Purpose

P5-4.3 adds the first Quest Domain vertical slice, `ACTIVATE_QUEST`, to the
existing Game Mode Runtime. It reuses the single Envelope, Gate, Actor control
turn, Coordinator, Receipt, and Actor-owned visibility chain. It does not add a
second runtime, queue, mailbox, coordinator, commit path, or domain execution
plane.

The frozen execution chain is:

```text
ACTIVATE_QUEST Command
-> Quest activation Evidence
-> QuestControlApplyPlanBuilder
-> pure quest activation transition
-> complete ControlApplyPlan
-> existing ActorOwnedApplyCoordinator
-> ReceiptAccepted
-> existing Actor-owned completion boundary
-> CandidateGameSnapshot visibility
```

## Domain Ownership and Snapshot Schema

Quest runtime state belongs to a new independent `QuestSnapshotSlice`. It must
not be stored in `GameRuleSnapshotSlice`: Game Rule owns the active rule-set and
disclosure references, while Quest owns public Quest runtime state. Hidden
goals, answers, prerequisites, and internal progress remain exclusively in
`HiddenGameStateSlice`.

Adding the seventh domain slice upgrades `CandidateGameSnapshot` to schema
version 3 and expands `GameSnapshotIdentity.domain_versions` from six to seven
ordered values. The order is lifecycle, phase, setup, participants, game rule,
quest, hidden state.

`QuestSnapshotSlice` is a frozen, slotted, value-only contract containing:

- `schema_version`
- `domain_version`
- `active_quest_id`
- `source_rule_set_reference`
- `committed_public_state_reference`

An empty slice has all three optional values set to `None` and domain version
zero. An active slice has all three values present and a positive domain
version. Its source rule-set reference must equal the committed rule-set
reference in the same candidate snapshot. An active Quest also requires an
active hidden-state reference. Partial or cross-slice bindings fail closed.

## Command and Authorization Contract

`SessionCommandType.ACTIVATE_QUEST` uses immutable `ActivateQuestPayload`:

- `quest_id`
- `expected_game_rule_version`
- `expected_quest_version`
- `expected_hidden_state_version`

The existing canonical intent and envelope bind command identity, operation
identity, scope, requester binding, observed global state version, sequence,
and payload fingerprint. Authorization is DM-only. Confirmation follows the
existing high-risk mutating-control policy.

The lifecycle prerequisite is `RUNNING`. The allowed phases are
`INTRODUCTION` and `EXPLORATION`; `LOBBY`, `DISCUSSION`, and `ENDING` are normal
business rejections.

## Evidence Contract

`ControlQuestActivationEvidence` is frozen, slotted, value-only, and exact-set
validated. It contains:

- evidence schema, game/session scope, and observed global version;
- Quest identity;
- active rule-set reference and game-rule domain version;
- current Quest identity/public-state reference and Quest domain version;
- resulting public Quest state reference;
- current/resulting hidden-state references and hidden-state domain version;
- provenance reference;
- `QuestActivationDisposition` and verified validation status.

The disposition set is:

- `AVAILABLE`
- `ALREADY_ACTIVE`
- `CONFLICT`
- `NOT_FOUND`
- `NOT_ACTIVATABLE`
- `RULE_SET_NOT_ACTIVE`
- `UNKNOWN`

`AVAILABLE` requires a complete result reference set and must advance the
hidden reference. Business-rejection dispositions must not carry speculative
result references. `UNKNOWN` is representable but is rejected by command
validation and never reaches transition or commit.

Evidence contains no Session, Snapshot, Actor, Port, callback, task, mutable
collection, clock, or I/O capability.

## Pure Transition Contract

`transition_quest_activation(request)` is synchronous, deterministic, and
pure. Its immutable request contains only the current Quest state, requested
Quest state when available, and disposition. It does not receive versions,
cursors, evidence, events, ownership, receipts, or runtime objects.

An empty current state plus `AVAILABLE` produces
`QuestActivationAccepted`. Rejections map as follows:

- same active Quest: `QUEST_ALREADY_ACTIVE`;
- different active Quest: `QUEST_CONFLICT`;
- missing definition: `QUEST_NOT_FOUND`;
- unmet committed prerequisites: `QUEST_NOT_ACTIVATABLE`;
- no active rule-set: `RULE_SET_NOT_ACTIVE`.

Malformed, partial, contradictory, or unknown inputs raise a typed transition
contract error and are mapped by the Builder to `BuildNonCommit`.

## Builder and Apply Composition

`QuestControlApplyPlanBuilder` owns validation and composition but no state or
I/O. On accepted activation it creates one complete `ControlApplyPlan`:

- global state version advances exactly once;
- materialization cursor becomes the input sequence;
- Quest Slice advances from version zero to one and receives the accepted
  public state reference;
- Hidden Slice advances exactly once and receives the resulting opaque
  committed reference;
- Game Rule and every unrelated slice remain unchanged by reference;
- ownership intent is `UNCHANGED`;
- one `QuestActivationMutation` binds command, evidence, current state,
  candidate, versions, references, and provenance;
- one PUBLIC `QUEST_ACTIVATED` result Event is emitted.

The public Event contains only command/operation/input identity, result state
version, Quest id, public-state reference, and Quest domain version. Hidden
references, provenance, internal conditions, and rule material must not enter
the Event, Receipt reference, or completion identity.

The Composite dispatcher routes `ACTIVATE_QUEST` to exactly one Quest Builder.
The Builder never calls the Coordinator, persistence, Event Store,
notification, recovery, reducer, clock, random source, UUID generator, or any
async API.

## Idempotency, Rejection, and Failure Boundary

An exact duplicate operation is handled by the existing Claim contract and
must not execute a second Apply. A new operation that activates the same Quest
is a committed business rejection, not idempotent success. A new operation
while another Quest is active is `QUEST_CONFLICT`.

Normal business rejections use `ControlRejectPlan`, do not replace the
snapshot, and advance only the committed control cursor after atomic rejection
commit:

- `INVALID_LIFECYCLE`
- `INVALID_PHASE`
- `RULE_SET_NOT_ACTIVE`
- `QUEST_ALREADY_ACTIVE`
- `QUEST_CONFLICT`
- `QUEST_NOT_FOUND`
- `QUEST_NOT_ACTIVATABLE`

Missing/unknown evidence, scope/version/reference mismatch, malformed
transition output, incomplete composition, invalid mutation, or cross-slice
binding mismatch returns `BuildNonCommit`. Claim conflict, unknown apply
outcome, invalid receipt, or post-commit visibility conflict uses the existing
fault-stop and recovery-required behavior. There is no retry Apply or replay
Apply.

## Implementation Scope Freeze

Allowed production changes are limited to:

- Quest command, evidence, transition, mutation, payload, and Builder contract
  modules;
- Composite Snapshot schema and identity expansion;
- existing authorization, confirmation, build-context mapping, public exports,
  result-event schema, ApplyPlan validation, and Composite dispatcher routing;
- contract and end-to-end tests using existing fakes.

Forbidden changes include Actor, Gate, Processor, Coordinator, Receipt,
visibility boundary, persistence, Event Store, recovery, notification sender,
mailbox, queue, runtime wiring, reducer, direct snapshot mutation, and all
Phase/Setup/Participant behavior. Plugin, Tool, LLM, Memory, and Agent Loop
remain outside Runtime Core.

## Contract Test and Regression Gate

Tests must cover immutable exact-set contracts, schema v3 migration, Quest
cross-slice binding, command/auth/confirmation/event contracts, every
disposition, transition determinism, mutation and plan completeness,
candidate/version/cursor/reference binding, business rejection, fail-closed
paths, duplicate admission, commit-before-visibility, hidden-state
non-disclosure, and one shared execution plane.

Completion requires:

1. P5-4.3 focused contract tests pass;
2. all Game Runtime tests pass;
3. the full repository suite passes;
4. `compileall` passes;
5. `git diff --check` passes;
6. forbidden-dependency scan passes;
7. architecture review confirms one Runtime, Gate, Actor state cell,
   Coordinator, and commit chain;
8. one P5-4.3 checkpoint commit is pushed.
