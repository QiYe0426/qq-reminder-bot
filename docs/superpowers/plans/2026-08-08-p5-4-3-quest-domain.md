# P5-4.3 Quest Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `ACTIVATE_QUEST` vertical slice through the existing Game Mode control and commit chain.

**Architecture:** Introduce an independent immutable Quest slice and a pure activation transition. A Quest-specific Builder composes the accepted decision, validated evidence, mutation, candidate snapshot, and public result Event into the existing `ControlApplyPlan`; all execution after the plan reuses the existing Coordinator, Receipt, and Actor-owned visibility boundary.

**Tech Stack:** Python 3.10, frozen/slotted dataclasses, enums, pytest, asyncio contract fakes.

## Global Constraints

- Preserve Single Runtime, Single Control Plane, Single Coordinator, Single Writer, and Commit-before-Visibility.
- Do not modify Actor, Gate, Processor, Coordinator, Receipt, persistence, Event Store, recovery, or notification sender.
- No direct snapshot mutation, reducer/replay Apply, retry Apply, I/O, clock, random, UUID, Plugin, Tool, LLM, Memory, or Agent Loop in domain contracts/builders.
- Composite Snapshot schema version becomes 3; Quest Slice schema version is 1.
- `ACTIVATE_QUEST` is DM-only, high-risk confirmation, RUNNING-only, and allowed only in INTRODUCTION or EXPLORATION.
- Work test-first and preserve all existing domain behavior.

---

### Task 1: Composite Quest Snapshot Contract

**Files:**
- Modify: `game_runtime/session_control/composite_snapshot.py`
- Modify: `game_runtime/session_control/__init__.py`
- Test: `tests/game_runtime/test_quest_snapshot_contract.py`
- Modify fixtures in existing Composite/domain contract tests for schema v3.

**Interfaces:**
- Produces: `QuestSnapshotSlice(schema_version, domain_version, active_quest_id, source_rule_set_reference, committed_public_state_reference)`.
- Produces: `CandidateGameSnapshot.quest: QuestSnapshotSlice` and seven-value `GameSnapshotIdentity.domain_versions`.

- [ ] **Step 1: Write failing snapshot tests**

```python
def test_empty_and_active_quest_slices_are_exact_and_immutable():
    empty = QuestSnapshotSlice(1, 0, None, None, None)
    active = QuestSnapshotSlice(1, 1, "quest-1", "rule-set:1", "quest-public:1")
    assert empty.domain_version == 0
    assert active.active_quest_id == "quest-1"

def test_candidate_requires_quest_rule_and_hidden_bindings():
    candidate = make_snapshot(quest=QuestSnapshotSlice(1, 1, "quest-1", "rule-set:other", "quest-public:1"))
    with pytest.raises(CompositeSnapshotContractError):
        CandidateGameSnapshot(**candidate)
```

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_snapshot_contract.py`

Expected: collection/import failure because `QuestSnapshotSlice` does not exist.

- [ ] **Step 3: Implement schema v3 and exact invariants**

```python
COMPOSITE_SNAPSHOT_SCHEMA_VERSION = 3
QUEST_SLICE_SCHEMA_VERSION = 1

@dataclass(frozen=True, slots=True)
class QuestSnapshotSlice:
    schema_version: int
    domain_version: int
    active_quest_id: str | None
    source_rule_set_reference: str | None
    committed_public_state_reference: str | None
```

Validate all-none/version-zero or all-present/version-positive. Add the Quest slice to `CandidateGameSnapshot`, cross-bind its source rule-set and hidden-state presence, and expand deterministic identity ordering.

- [ ] **Step 4: Update all existing snapshot fixtures explicitly to schema v3 with an empty Quest slice**

- [ ] **Step 5: Run GREEN and existing Composite/domain tests**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_snapshot_contract.py tests/game_runtime/test_composite_game_snapshot_contract.py tests/game_runtime/test_composite_lifecycle_promotion_contract.py`

Expected: PASS.

### Task 2: Command, Governance, Evidence, and Result Event Contracts

**Files:**
- Modify: `game_runtime/session_control/commands.py`
- Modify: `game_runtime/session_control/authorization.py`
- Modify: `game_runtime/session_control/confirmation.py`
- Modify: `game_runtime/session_control/build_context.py`
- Create: `game_runtime/session_control/quest_evidence.py`
- Modify: `game_runtime/event/control_payloads.py`
- Modify public `__init__.py` modules.
- Test: `tests/game_runtime/test_quest_control_contract.py`

**Interfaces:**
- Produces: `SessionCommandType.ACTIVATE_QUEST` and `ActivateQuestPayload`.
- Produces: `QuestActivationDisposition`, `QuestEvidenceStatus`, and `ControlQuestActivationEvidence`.
- Produces: `QuestActivatedPayload` for `GameEventType.QUEST_ACTIVATED` with PUBLIC visibility.

- [ ] **Step 1: Write failing exact-set and governance tests**

```python
payload = ActivateQuestPayload("quest-1", 1, 0, 1)
assert required_permission_for(SessionCommandType.ACTIVATE_QUEST).value == "ACTIVATE_QUEST"
assert confirmation_requirement_for(SessionCommandType.ACTIVATE_QUEST).value == "REQUIRED"
```

Cover AVAILABLE, rejection dispositions, UNKNOWN validation, scope/version/payload mismatch, immutable slots, and hidden-reference absence from `QuestActivatedPayload`.

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_control_contract.py`

- [ ] **Step 3: Add closed command/governance/event mappings and exact Evidence validation**

```python
class QuestActivationDisposition(str, Enum):
    AVAILABLE = "AVAILABLE"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    NOT_ACTIVATABLE = "NOT_ACTIVATABLE"
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"
    UNKNOWN = "UNKNOWN"
```

AVAILABLE requires complete current/result bindings; business rejection evidence carries no speculative result references; UNKNOWN fails command validation.

- [ ] **Step 4: Run GREEN plus command/auth/confirmation/event regressions**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_control_contract.py tests/game_runtime/test_session_control_commands.py tests/game_runtime/test_session_control_authorization.py tests/game_runtime/test_session_control_confirmation.py tests/game_runtime/test_game_event_control_schema.py`

Expected: PASS.

### Task 3: Pure Quest Activation Transition

**Files:**
- Create: `game_runtime/session_control/quest_transition.py`
- Modify: `game_runtime/session_control/__init__.py`
- Test: `tests/game_runtime/test_quest_transition_contract.py`

**Interfaces:**
- Consumes: `QuestActivationDisposition`.
- Produces: `QuestActivationState`, `QuestActivationRequest`, `QuestActivationAccepted`, `QuestActivationRejected`, typed effects/reasons/errors, and `transition_quest_activation(request)`.

- [ ] **Step 1: Write failing pure transition tests**

```python
decision = transition_quest_activation(
    QuestActivationRequest(
        current_state=QuestActivationState(None, None, None),
        requested_state=QuestActivationState("quest-1", "rule-set:1", "quest-public:1"),
        disposition=QuestActivationDisposition.AVAILABLE,
    )
)
assert isinstance(decision, QuestActivationAccepted)
```

Test every rejection mapping, contradictory state, UNKNOWN, determinism, immutability, and forbidden imports.

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_transition_contract.py`

- [ ] **Step 3: Implement one synchronous deterministic transition function**

No version, cursor, Event, evidence, ownership, clock, random, UUID, global mutable state, or I/O enters the transition module.

- [ ] **Step 4: Run GREEN**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_transition_contract.py`

Expected: PASS.

### Task 4: Quest Mutation and ApplyPlan Builder Composition

**Files:**
- Modify: `game_runtime/session_control/apply_contract.py`
- Create: `game_runtime/session_control/quest_control_builder.py`
- Modify: `game_runtime/session_control/composite_control_builder.py`
- Modify: `game_runtime/session_control/__init__.py`
- Test: `tests/game_runtime/test_quest_apply_composition_contract.py`

**Interfaces:**
- Produces: `QuestMutationType.ACTIVATE_QUEST` and frozen `QuestActivationMutation`.
- Produces: `QuestControlApplyPlanBuilder.build(context) -> BuildPlanReady | BuildReject | BuildNonCommit`.
- Composite dispatcher routes `ACTIVATE_QUEST` to exactly one Quest Builder.

- [ ] **Step 1: Write failing composition tests**

```python
outcome = QuestControlApplyPlanBuilder().build(available_context())
assert isinstance(outcome, BuildPlanReady)
assert outcome.plan.command_type is SessionCommandType.ACTIVATE_QUEST
assert outcome.plan.candidate_snapshot.quest.active_quest_id == "quest-1"
assert outcome.plan.candidate_snapshot.hidden_state.domain_version == 2
```

Test lifecycle/phase/business rejects, every disposition, evidence mismatch, plan completeness, version/cursor binding, unrelated-slice identity preservation, deterministic Event identity, PUBLIC visibility, and dispatcher single routing.

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_apply_composition_contract.py`

- [ ] **Step 3: Implement mutation validation and pure Builder**

Candidate changes only global version/cursor, Quest Slice, and Hidden Slice. `OwnershipIntentType.UNCHANGED`; one `QUEST_ACTIVATED` Event; no speculative candidate for rejection.

- [ ] **Step 4: Run GREEN plus all Builder contracts**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_apply_composition_contract.py tests/game_runtime/test_composite_control_builder_contract.py tests/game_runtime/test_apply_plan_builder_contract.py`

Expected: PASS.

### Task 5: End-to-End Contract and Release Gate

**Files:**
- Create: `tests/game_runtime/test_quest_activation_end_to_end_contract.py`

**Interfaces:**
- Uses existing Gate, Actor, Processor, Coordinator, atomic-commit fake, Receipt validation, and Actor-owned completion boundary unchanged.

- [ ] **Step 1: Write E2E contract tests**

Cover successful atomic commit before visibility, committed business rejection without snapshot replacement, exact duplicate execution once, conflicting duplicate fault stop, apply unknown, invalid receipt, hidden-data non-disclosure, and exactly one queue/coordinator/control plane.

- [ ] **Step 2: Run focused E2E tests and correct only in-scope contract defects**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime/test_quest_activation_end_to_end_contract.py`

- [ ] **Step 3: Run release gates**

Run: `.venv\Scripts\python.exe -m pytest -q tests/game_runtime`

Run: `.venv\Scripts\python.exe -m pytest -q`

Run: `.venv\Scripts\python.exe -m compileall -q game_runtime tests/game_runtime`

Run: `git diff --check`

Scan changed production modules for forbidden dependencies and verify Actor, Gate, Processor, Coordinator, Receipt, persistence, recovery, and notification files are unchanged.

- [ ] **Step 4: Architecture review**

Confirm one Runtime, Gate queue, Actor state cell, Coordinator, Receipt chain, Candidate snapshot, and visibility boundary; no direct mutation or replay Apply.

- [ ] **Step 5: Create and push one checkpoint**

```text
git commit -m "feat(game-mode): add quest activation control plane"
git push origin feature/game-mode-runtime-v01
```
