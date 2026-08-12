# DeepSeek Balance QQ Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin-only private QQ command that queries and safely reports the configured DeepSeek account balance without invoking an AI model.

**Architecture:** A focused NoneBot plugin owns URL normalization, the authenticated HTTP request, response validation, Chinese formatting, and the private-message command boundary. The network function accepts injectable configuration/session inputs so unit tests can cover behavior without real credentials or network access.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot v11, aiohttp, pytest

## Global Constraints

- Only DeepSeek is supported in the first version.
- Commands are `查询 AI 余额`, `查询余额`, and `AI余额`, matched exactly.
- The command is private-message-only and restricted by the existing `BOT_ADMIN_USER_IDS` policy.
- Never log, reply with, or persist API keys, Authorization headers, raw response bodies, or balance details.
- Add no third-party dependency.
- Preserve all unrelated dirty-worktree changes.

---

### Task 1: Balance service behavior

**Files:**
- Create: `plugins/ai_balance.py`
- Create: `tests/test_ai_balance.py`

**Interfaces:**
- Produces: `DeepSeekBalanceError`, `build_balance_url(base_url: str) -> str`, `fetch_deepseek_balance(*, api_key: str | None = None, base_url: str | None = None, session_factory: Callable[..., Any] = aiohttp.ClientSession) -> dict[str, Any]`, and `format_deepseek_balance(payload: Mapping[str, Any]) -> str`.
- Consumes: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `aiohttp.ClientSession`, and DeepSeek `GET /user/balance` response fields.

- [ ] **Step 1: Write failing tests for URL and formatting**

  Add tests asserting that empty/default, trailing-slash, and `/v1` base URLs become `https://api.deepseek.com/user/balance` or the equivalent configured origin, and that valid CNY/USD payloads render status, total, granted, and topped-up balances without float conversion.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run: `./.venv/Scripts/python -m pytest tests/test_ai_balance.py -q`

  Expected: FAIL because `plugins.ai_balance` does not exist.

- [ ] **Step 3: Implement URL normalization and formatting minimally**

  Create immutable error/result boundaries, validate `is_available` and every `balance_infos` entry, map `CNY` to `¥` and `USD` to `$`, and raise a stable `DeepSeekBalanceError` for malformed data.

- [ ] **Step 4: Run the focused tests and verify GREEN**

  Run: `./.venv/Scripts/python -m pytest tests/test_ai_balance.py -q`

  Expected: URL/format tests PASS.

- [ ] **Step 5: Write failing async request tests**

  Add fake aiohttp-compatible session/response objects for success, missing key, HTTP 401/402/429/500, timeout, invalid JSON, and malformed JSON. Assert test secrets and raw response markers never appear in exceptions or logs.

- [ ] **Step 6: Run request tests and verify RED**

  Run: `./.venv/Scripts/python -m pytest tests/test_ai_balance.py -q`

  Expected: FAIL because the request behavior is not implemented.

- [ ] **Step 7: Implement the authenticated balance request**

  Use a 10-second aiohttp timeout, Bearer authorization, `Accept: application/json`, no retry, status-specific safe Chinese messages, and structure validation. Log only exception class or HTTP status.

- [ ] **Step 8: Run the focused tests and verify GREEN**

  Run: `./.venv/Scripts/python -m pytest tests/test_ai_balance.py -q`

  Expected: PASS with no secret leakage.

### Task 2: NoneBot command boundary and registration

**Files:**
- Modify: `plugins/ai_balance.py`
- Modify: `tests/test_ai_balance.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `admin_denial(event)`, `PrivateMessageEvent`, and Task 1 balance service functions.
- Produces: a matcher registered with `on_fullmatch(("查询 AI 余额", "查询余额", "AI余额"), priority=5, block=True)` and a handler that only answers private messages.

- [ ] **Step 1: Write failing command-boundary and registration tests**

  Test a small handler helper with private admin, private non-admin, and group contexts; inspect `pyproject.toml` for `plugins.ai_balance`; inspect README text for the three aliases and privacy restriction.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `./.venv/Scripts/python -m pytest tests/test_ai_balance.py tests/test_project_config.py -q`

  Expected: FAIL because handler behavior, registration, and docs are absent.

- [ ] **Step 3: Implement command boundary, registration, and documentation**

  Filter group events before querying, reuse `admin_denial`, return only formatted/safe error text, add `plugins.ai_balance` to `[tool.nonebot].plugins`, and document the administrator private command in README.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Run: `./.venv/Scripts/python -m pytest tests/test_ai_balance.py tests/test_project_config.py -q`

  Expected: PASS.

### Task 3: Repository verification and deployment

**Files:**
- Verify only: `bot.py`, `plugins/`, `tests/`, `pyproject.toml`, `README.md`

**Interfaces:**
- Consumes: completed Tasks 1-2.
- Produces: a verified local change and a restarted production bot with a connected OneBot session.

- [ ] **Step 1: Run compile verification**

  Run: `./.venv/Scripts/python -m compileall -q bot.py plugins scripts sts_knowledge_seed.py`

  Expected: exit 0.

- [ ] **Step 2: Run the full test suite**

  Run: `./.venv/Scripts/python -m pytest -q`

  Expected: all tests PASS. If unrelated dirty game-runtime tests fail, distinguish and report them without altering those files.

- [ ] **Step 3: Review the exact feature diff and secret safety**

  Run `git diff --check`, inspect only feature files, and search them for credential literals or unsafe response logging.

- [ ] **Step 4: Commit only feature files**

  Stage `plugins/ai_balance.py`, `tests/test_ai_balance.py`, `pyproject.toml`, `README.md`, and this plan. Commit with `feat(ai): add DeepSeek balance QQ command`; do not stage unrelated changes.

- [ ] **Step 5: Deploy through the repository workflow**

  Push the current branch only if it is the configured deployable branch and the deployment workflow supports it; otherwise use the documented server `git pull --ff-only` route without editing production source files. Install dependencies, compile, restart `qq-reminder-bot`, and inspect logs.

- [ ] **Step 6: Verify runtime health**

  Confirm `systemctl is-active qq-reminder-bot` is `active`, NapCat is running, and the journal shows `Bot 1467807354 connected`. Do not send a balance query automatically because that would disclose financial data in tool output; ask the administrator to test the QQ command after deployment.
