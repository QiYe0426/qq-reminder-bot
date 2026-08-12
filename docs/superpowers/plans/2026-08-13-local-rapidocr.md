# Local RapidOCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace image-recognition and keyword-retort cloud vision calls with one cached, bounded, local RapidOCR service for Simplified Chinese and English text.

**Architecture:** A new `plugins.local_ocr` module owns configuration, safe image download, lazy RapidOCR initialization, worker-thread inference, normalization, caching, concurrency, and timeout handling. `media_insights` and `group_reactions` consume that module and contain only flow-specific behavior.

**Tech Stack:** Python 3.10+, asyncio, RapidOCR 3.9.2, ONNX Runtime CPU 1.24.4, existing `plugins.safe_http_fetch`, pytest.

## Global Constraints

- OCR is local-only and must never fall back to a cloud visual API.
- OCR extracts Simplified Chinese and English text only; it does not describe people, objects, expressions, or scenes.
- The production host has two CPU cores, approximately 2 GiB RAM, and no GPU.
- Inference concurrency is one by default and synchronous inference must run outside the event loop.
- Automatic keyword retort silently skips OCR failures; direct media processing records a short failure.
- Do not retain user images beyond the existing request/temp lifecycle.
- Do not read, log, copy, or commit `.env.local` or real credentials.

---

### Task 1: Shared Local OCR Service

**Files:**
- Create: `plugins/local_ocr.py`
- Create: `tests/test_local_ocr.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `plugins.safe_http_fetch.fetch_public_url(url, timeout_seconds, max_bytes, headers)`.
- Produces: `local_ocr_enabled() -> bool`, `local_ocr_engine_name() -> str`, `async extract_text_from_bytes(body: bytes) -> str`, `async extract_text_from_url(url: str) -> str`, and `reset_local_ocr_state_for_tests() -> None`.

- [ ] **Step 1: Add failing configuration and normalization tests**

```python
def test_normalizes_rapidocr_result_in_reading_order(monkeypatch):
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: FakeEngine([
        ([[0, 30], [20, 30], [20, 40], [0, 40]], "world", 0.91),
        ([[0, 10], [20, 10], [20, 20], [0, 20]], "你好", 0.98),
        ([[0, 50], [20, 50], [20, 60], [0, 60]], "ignored", 0.2),
    ]))
    assert asyncio.run(local_ocr.extract_text_from_bytes(b"image")) == "你好\nworld"
```

Also assert invalid environment values are clamped to safe defaults, disabled OCR returns an empty string, empty bytes are rejected, and content above `LOCAL_OCR_MAX_IMAGE_BYTES` is rejected.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_local_ocr.py -q`

Expected: collection fails because `plugins.local_ocr` does not exist.

- [ ] **Step 3: Implement configuration, lazy engine loading, and result normalization**

Create `plugins/local_ocr.py` with these constants and boundaries:

```python
LOCAL_OCR_ENABLED_ENV = "LOCAL_OCR_ENABLED"
LOCAL_OCR_CONFIDENCE_ENV = "LOCAL_OCR_CONFIDENCE"
LOCAL_OCR_TIMEOUT_SECONDS_ENV = "LOCAL_OCR_TIMEOUT_SECONDS"
LOCAL_OCR_MAX_IMAGE_BYTES_ENV = "LOCAL_OCR_MAX_IMAGE_BYTES"
LOCAL_OCR_CACHE_TTL_SECONDS_ENV = "LOCAL_OCR_CACHE_TTL_SECONDS"
LOCAL_OCR_CACHE_MAX_ENTRIES_ENV = "LOCAL_OCR_CACHE_MAX_ENTRIES"
LOCAL_OCR_CONCURRENCY_ENV = "LOCAL_OCR_CONCURRENCY"

DEFAULT_CONFIDENCE = 0.5
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_CACHE_MAX_ENTRIES = 128
DEFAULT_CONCURRENCY = 1
```

`_load_engine()` imports `RapidOCR` lazily and constructs it once. `_run_engine(body)` calls the engine, supports the RapidOCR 3.x result object (`result.txts`, `result.scores`, `result.boxes`), converts entries into `(top, left, text, confidence)` tuples, filters below threshold, sorts by `(top, left)`, and joins lines with `"\n"`.

- [ ] **Step 4: Add failing cache, timeout, concurrency, and safe-download tests**

```python
def test_same_bytes_use_cached_result(monkeypatch):
    engine = CountingEngine("cached text")
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: engine)
    first = asyncio.run(local_ocr.extract_text_from_bytes(b"same"))
    second = asyncio.run(local_ocr.extract_text_from_bytes(b"same"))
    assert (first, second, engine.calls) == ("cached text", "cached text", 1)
```

Mock `fetch_public_url` and assert `extract_text_from_url` passes the configured timeout, size limit, and a fixed user agent. Use a blocking fake engine to assert timeout raises `LocalOCRError`, concurrent requests do not exceed the configured semaphore, and initialization failure is memoized without logging image data.

- [ ] **Step 5: Implement async execution, cache, timeout, and safe URL input**

Use `asyncio.to_thread` for `_run_engine`, `asyncio.wait_for` for the bounded timeout, a process-wide `asyncio.Semaphore`, SHA-256 of image bytes as the cache key, an `OrderedDict` of `(expires_at, text)` entries, and `fetch_public_url` for URL inputs. Do not store the image bytes after the call returns.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `python -m pytest tests/test_local_ocr.py tests/test_safe_http_fetch.py -q`

Expected: all tests pass.

- [ ] **Step 7: Pin runtime dependencies and commit**

Add to `[project].dependencies`:

```toml
"rapidocr==3.9.2",
"onnxruntime==1.24.4",
```

Run: `git add plugins/local_ocr.py tests/test_local_ocr.py pyproject.toml && git commit -m "feat(ocr): add shared local RapidOCR service"`

---

### Task 2: Route Both Image Consumers Through Local OCR

**Files:**
- Modify: `plugins/media_insights.py`
- Modify: `plugins/group_reactions.py`
- Create: `tests/test_media_insights_ocr.py`
- Create: `tests/test_group_reactions_ocr.py`

**Interfaces:**
- Consumes: `plugins.local_ocr.extract_text_from_url(url)`, `local_ocr_enabled()`, and `local_ocr_engine_name()`.
- Produces: media insight rows containing OCR text and `engine="RapidOCR/ONNXRuntime"`; keyword matching based on the same local OCR interface.

- [ ] **Step 1: Add failing media-insight routing tests**

Patch `media_insights.extract_local_ocr_text` with an async fake and assert `analyze_image_url(url)` returns its OCR text without constructing `openai.AsyncOpenAI`. Assert an empty OCR result raises `RuntimeError("no text detected")`. Assert pending image processing is gated by `LOCAL_OCR_ENABLED`, not API-key presence.

- [ ] **Step 2: Run media tests and verify RED**

Run: `python -m pytest tests/test_media_insights_ocr.py -q`

Expected: tests fail because media insights still use the cloud client.

- [ ] **Step 3: Replace media cloud vision with local OCR**

Import the shared service with aliases:

```python
from plugins.local_ocr import (
    extract_text_from_url as extract_local_ocr_text,
    local_ocr_enabled,
    local_ocr_engine_name,
)
```

Remove image model/API-key/base-URL helpers and the multimodal prompt. Keep `IMAGE_VISION_ENABLED` as a deprecated compatibility alias only if required by existing deployments; `LOCAL_OCR_ENABLED` is authoritative. Make `analyze_image_url()` call local OCR, raise `RuntimeError("no text detected")` on empty text, store `engine` instead of cloud `model`, and update status/skip messages to say local OCR.

- [ ] **Step 4: Add failing keyword-retort routing tests**

Patch `group_reactions.extract_local_ocr_text` and assert `extract_image_text(url)` returns normalized OCR text, strips URLs from OCR output before keyword matching, makes no cloud client, and returns `""` when local OCR is disabled. Assert `matching_keyword_retort_rule()` silently continues when OCR raises.

- [ ] **Step 5: Run keyword tests and verify RED**

Run: `python -m pytest tests/test_group_reactions_ocr.py -q`

Expected: tests fail because keyword retort still requires an API key and cloud model.

- [ ] **Step 6: Replace keyword-retort cloud vision with local OCR**

Delete keyword image model/API-key/base-URL/timeout configuration and `IMAGE_TEXT_SCAN_PROMPT`. Preserve `KEYWORD_RETORT_IMAGE_SCAN_ENABLED` and max-image count as flow controls. Implement:

```python
async def extract_image_text(image_url: str) -> str:
    if not local_ocr_enabled():
        return ""
    return normalize_text(strip_urls(await extract_local_ocr_text(image_url)))
```

Update admin state to report `engine: local_ocr_engine_name()` instead of a cloud model.

- [ ] **Step 7: Run focused consumer tests and commit**

Run: `python -m pytest tests/test_media_insights_ocr.py tests/test_group_reactions_ocr.py -q`

Expected: all tests pass.

Run: `git add plugins/media_insights.py plugins/group_reactions.py tests/test_media_insights_ocr.py tests/test_group_reactions_ocr.py && git commit -m "feat(ocr): use local OCR for image flows"`

---

### Task 3: Configuration, Documentation, and Status Query Alignment

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `plugins/ai_balance.py`
- Modify: `tests/test_ai_balance.py`
- Modify: `tests/test_project_config.py`

**Interfaces:**
- Consumes: the local OCR environment variables from Task 1.
- Produces: accurate operator docs and AI status output that no longer reports Qwen as responsible for either image module.

- [ ] **Step 1: Add failing status/config tests**

Assert AI balance output identifies `RapidOCR（本地）` as responsible for “图片识别、关键词回怼图片文字识别” and does not probe Qwen when no other module uses it. Assert `.env.example` contains all `LOCAL_OCR_*` keys and no longer advertises `IMAGE_VISION_MODEL/API_KEY/BASE_URL` or `KEYWORD_RETORT_IMAGE_SCAN_MODEL/API_KEY/BASE_URL`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_ai_balance.py tests/test_project_config.py -q`

Expected: local OCR status/config assertions fail.

- [ ] **Step 3: Update templates, docs, and AI module reporting**

Document these defaults:

```dotenv
LOCAL_OCR_ENABLED=1
LOCAL_OCR_CONFIDENCE=0.5
LOCAL_OCR_TIMEOUT_SECONDS=15
LOCAL_OCR_MAX_IMAGE_BYTES=5242880
LOCAL_OCR_CACHE_TTL_SECONDS=300
LOCAL_OCR_CACHE_MAX_ENTRIES=128
LOCAL_OCR_CONCURRENCY=1
KEYWORD_RETORT_IMAGE_SCAN_ENABLED=1
KEYWORD_RETORT_IMAGE_SCAN_MAX_IMAGES=2
```

Explain that both flows are offline, share cached inference, and recognize text only. Update `ai_balance.py` so the module list reports local OCR without making a balance/status API request for it; keep unrelated DeepSeek/Qwen reporting behavior intact.

- [ ] **Step 4: Run documentation/config tests and commit**

Run: `python -m pytest tests/test_ai_balance.py tests/test_project_config.py -q`

Expected: all tests pass.

Run: `git add .env.example README.md plugins/ai_balance.py tests/test_ai_balance.py tests/test_project_config.py && git commit -m "docs(ocr): document offline OCR configuration"`

---

### Task 4: Full Verification and Production Deployment

**Files:**
- No source files expected.

**Interfaces:**
- Consumes: completed Tasks 1–3.
- Produces: verified production deployment with live local OCR.

- [ ] **Step 1: Install dependencies in the isolated worktree**

Run: `.venv\Scripts\python -m pip install -e ".[dev]"` if that environment exists; otherwise use the configured workspace Python environment.

Expected: RapidOCR 3.9.2 and ONNX Runtime 1.24.4 install successfully.

- [ ] **Step 2: Run static and automated verification**

Run:

```powershell
python -m compileall -q bot.py plugins scripts sts_knowledge_seed.py
python -m pytest -q
python -m pip check
```

Expected: compilation succeeds, the full test suite passes, and `pip check` reports no broken requirements.

- [ ] **Step 3: Run a real local OCR smoke test**

Generate an in-memory or temporary Chinese/English image outside tracked paths and call `extract_text_from_bytes`. Confirm non-empty recognized text and remove the temporary artifact.

- [ ] **Step 4: Push and deploy through Git**

Push the implementation branch. On `tencent-bot`, run the repository's documented sequence: `git pull --ff-only`, `.venv/bin/pip install -e .`, compile check, full or focused pytest, and `sudo systemctl restart qq-reminder-bot`. Do not edit production source directly.

- [ ] **Step 5: Verify production health and resource usage**

Check `systemctl is-active qq-reminder-bot`, the NapCat container, recent privacy-safe logs, OneBot connection, RapidOCR engine check, and process RSS before/after OCR. Send representative QQ images when possible; otherwise execute a server-local OCR fixture and report that live QQ validation remains pending.

- [ ] **Step 6: Record final deployment commit**

If deployment-only documentation changes are unnecessary, no additional commit is required. Report the deployed commit, test totals, smoke-test result, service status, and measured memory usage.
