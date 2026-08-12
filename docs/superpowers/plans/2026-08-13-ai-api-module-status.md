# AI API Module Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the administrator QQ balance command to label each provider's bot responsibilities and report live Qwen/DashScope availability or arrears status.

**Architecture:** Keep the existing `plugins.ai_balance` command boundary and add focused immutable Qwen configuration/result types, environment discovery with deduplication, a one-token active probe, and a provider-independent report composer. DeepSeek and Qwen checks run independently so one failure cannot hide the other provider's status.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot v11, aiohttp, pytest

## Global Constraints

- Qwen status is a live one-token probe, not an Alibaba Cloud RMB balance query.
- Only configured QQ administrators in private messages may query; group messages remain silent.
- Never expose or log API keys, Authorization headers, raw response bodies, request IDs, or unfiltered provider errors.
- Preserve the existing three command aliases and add no dependency.
- Deduplicate Qwen probes only when key, base URL, and model are identical.
- Preserve unrelated worktree and production data.

---

### Task 1: Qwen configuration discovery

**Files:**
- Modify: `plugins/ai_balance.py`
- Modify: `tests/test_ai_balance.py`

**Interfaces:**
- Produces: `QwenProbeConfig(modules: tuple[str, ...], api_key: str, base_url: str, model: str)`, `discover_qwen_probe_configs(environ: Mapping[str, str]) -> list[QwenProbeConfig]`, and `build_qwen_probe_url(base_url: str) -> str`.
- Consumes: `IMAGE_VISION_*`, `KEYWORD_RETORT_IMAGE_SCAN_*`, and `OPENAI_*` environment variables.

- [ ] Write failing tests showing image configuration fallback, keyword configuration fallback, identical-config deduplication with merged module labels, separate profiles for distinct settings, and compatible-mode URL construction.
- [ ] Run `python -m pytest tests/test_ai_balance.py -q` and confirm RED due to missing Qwen interfaces.
- [ ] Implement frozen configuration data, exact fallback precedence, stable module labels, deduplication, and `/chat/completions` URL construction.
- [ ] Re-run the focused tests and confirm GREEN.

### Task 2: Safe one-token Qwen probe

**Files:**
- Modify: `plugins/ai_balance.py`
- Modify: `tests/test_ai_balance.py`

**Interfaces:**
- Produces: `QwenProbeResult(status: str, detail: str)`, `probe_qwen_status(config: QwenProbeConfig, session_factory: Callable[..., Any] = aiohttp.ClientSession) -> QwenProbeResult`, and `format_qwen_status(config, result) -> str`.
- Consumes: Task 1 config and OpenAI-compatible `POST /chat/completions` responses.

- [ ] Write failing fake-session tests asserting Bearer authentication, `stream=false`, a short text message, `max_tokens=1`, and 10-second timeout.
- [ ] Add failing classifications for HTTP 200, `Arrearage`, 401/403, 429, timeout, network failure, 5xx, and unknown/invalid JSON; assert secret and raw markers never enter replies or logs.
- [ ] Run the focused tests and confirm RED from missing probe behavior.
- [ ] Implement the minimal request, curated status mapping, safe logging, no retry, and pure Chinese formatter.
- [ ] Re-run the focused tests and confirm GREEN.

### Task 3: Multi-provider report and command integration

**Files:**
- Modify: `plugins/ai_balance.py`
- Modify: `tests/test_ai_balance.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `build_ai_api_status_report(deepseek_fetcher=..., qwen_configs=..., qwen_probe=...) -> str` and updated `balance_command_text()` using it after existing privacy/admin gates.
- Consumes: existing DeepSeek balance formatter plus Tasks 1-2.

- [ ] Write failing tests for the `AI API 状态` report, DeepSeek and Qwen module labels, provider partial failure, multiple Qwen profiles, command privacy, and README wording about Qwen probe cost/no RMB balance.
- [ ] Run `python -m pytest tests/test_ai_balance.py tests/test_project_config.py -q` and confirm RED.
- [ ] Implement independent concurrent provider checks, fixed output ordering, safe per-provider error blocks, and update README.
- [ ] Re-run the focused tests and confirm GREEN.

### Task 4: Verification, commit, and production deployment

**Files:**
- Verify and commit: `plugins/ai_balance.py`, `tests/test_ai_balance.py`, `README.md`, this plan, and the approved design spec.

**Interfaces:**
- Produces: a tested production commit on `feature/agent-runtime-v3` and a restarted healthy bot.

- [ ] Run `python -m compileall -q bot.py plugins scripts sts_knowledge_seed.py` and the full `python -m pytest -q` suite.
- [ ] Run `git diff --check`, inspect the exact feature diff, and search for unsafe credential/response logging.
- [ ] Commit only feature files with `feat(ai): add Qwen status to balance query` and push by fast-forward to `origin/feature/agent-runtime-v3`.
- [ ] On the server run `git pull --ff-only`, editable install, compile, restart `qq-reminder-bot`, then confirm service active, `ai_balance` loaded, NapCat running, and OneBot connected.
- [ ] Do not execute the live QQ query from diagnostics because it would expose account status in tool output; hand off the command to the administrator.
