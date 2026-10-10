# Step 10: LLM Adapter (Build Step 9)

## What Was Implemented

Implemented a lightweight, provider-agnostic LLM adapter in `src/llm_adapter/` following Project Plan v4.
The adapter provides a clean interface for executing prompts, obtaining validated structured responses, recording calls in an audit ledger, and enabling deterministic offline record/replay for test suites.

No external client libraries (e.g. `openai`, `anthropic`) were added; the adapter relies entirely on Python's standard library (`urllib.request`) and `pydantic`.
No learning loops, PII redaction, or statement parsing orchestration were implemented in this step.

---

## Modules Created

### `src/llm_adapter/exceptions.py`

Clean exception hierarchy rooted in `LLMAdapterError`:

| Exception | Purpose |
|---|---|
| `LLMAdapterError` | Base class for all adapter errors |
| `LLMConfigurationError` | Missing or invalid API key or configuration |
| `LLMConnectionError` | Network connection failures, DNS errors, endpoint unreachable |
| `LLMTimeoutError` | Request exceeded timeout threshold |
| `LLMAPIError` | Non-2xx HTTP responses (includes `status_code`; never leaks secrets) |
| `LLMRateLimitError` | HTTP 429 rate limit errors (subclasses `LLMAPIError`) |
| `LLMResponseValidationError` | Response is not valid JSON or fails schema/type validation |
| `LLMReplayError` | Replay mode requested but no recorded fixture exists |

### `src/llm_adapter/models.py`

Pydantic data models for configuration, requests, responses, and auditing:

| Model | Purpose |
|---|---|
| `LLMConfig` | API endpoint, credentials, timeout, token pricing rates, replay mode, ledger path. Masks API keys in `__repr__` and `__str__`. |
| `LLMRequest` | Minimal prompt, optional `system_prompt`, optional `response_schema`, temperature, max tokens, metadata. Enforces non-empty prompt and computes SHA-256 prompt hash. |
| `LLMResponse` | Raw text `content`, validated `structured` dict, token counts, cost estimate, latency, unique `call_id`, timestamp, and prompt hash. Supports dict-like access (`response["field"]`). |
| `LLMCallRecord` | Structured audit record for every attempted call (success or failure). |

### `src/llm_adapter/ledger.py`

`LLMLedger` maintains an audit trail of LLM interactions:
- Cumulative metrics: `total_cost()`, `total_tokens()`, `total_calls()`, `successful_calls()`, `failed_calls()`.
- Optional disk logging: appends single JSON lines to a `.jsonl` file whenever `ledger_path` is configured.
- Export utility: `export_jsonl(path)`.

### `src/llm_adapter/adapter.py`

Core adapter classes:

#### `LLMAdapter`
- **Minimal interface**: Exposes `complete(...)` and `complete_request(...)`.
- **Standard Library HTTP Transport**: Uses `urllib.request` with timeouts and safe error unpacking.
- **Untrusted Output Validation**: Automatically extracts JSON payloads from raw text or markdown code fences (` ```json ... ``` `) and validates them against Pydantic models or JSON Schema dictionaries.
- **Record & Replay**: When `replay_mode="record"`, saves JSON fixtures indexed by prompt SHA-256 hash. When `replay_mode="replay"`, serves recorded fixtures with zero network calls and zero token cost.
- **Pluggable Transport**: Accepts custom transport callables for mocking in tests or alternative proxies.

#### `FakeLLMAdapter`
- Subclasses `LLMAdapter` for fast, offline unit and integration tests.
- Allows registering canned responses by prompt substring or returning a default JSON response.
- Requires zero API credentials and zero network access.

### `src/llm_adapter/__init__.py`

Public exports for all adapter symbols, models, and exceptions.

---

## Configuration & Environment Variables

The adapter reads its configuration from environment variables via `LLMConfig.from_env()`:

| Environment Variable | Default | Description |
|---|---|---|
| `LLM_API_KEY` | `None` | API key for authentication. Strictly masked in logs and exceptions. |
| `LLM_MODEL` | `gpt-4o-mini` | Default model name. |
| `LLM_API_BASE` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible completions endpoints. |
| `LLM_TIMEOUT` | `30.0` | HTTP request timeout in seconds. |
| `LLM_REPLAY_MODE` | `disabled` | Replay mode: `disabled`, `record`, or `replay`. |
| `LLM_REPLAY_DIR` | `None` | Path to directory where prompt-hash replay fixtures are read or written. |
| `LLM_LEDGER_PATH` | `None` | Path to append JSONL audit call records. |

`.env.example` was updated with blank template entries for these keys.

---

## Security & Untrusted Output Principles

1. **Credential Safety**:
   - `LLMConfig.__repr__` and `__str__` replace `api_key` with `"***"`.
   - Error messages never expose the `Authorization` header or secret values.
2. **Minimal Data Exposure**:
   - The adapter accepts only concise prompt strings; it explicitly does not ingest raw PDF bytes, full financial statements, or PII.
3. **Untrusted LLM Output**:
   - All completions undergo structured JSON extraction and schema verification.
   - Malformed responses raise `LLMResponseValidationError` rather than silently propagating.
   - LLM responses never directly mutate domain `Statement` or `Holding` models or activate parsing specifications without validation.

---

## Tests Created

**`tests/test_llm_adapter.py`** — 8 test classes covering 20 focused test scenarios:

| Class | Coverage |
|---|---|
| `TestLLMConfig` | Defaults, `from_env()`, secrets masking in `repr`/`str`, missing keys |
| `TestLLMRequestResponse` | Non-empty prompt validation, deterministic prompt hashing, dict access on `LLMResponse` |
| `TestLLMAdapterExecution` | Custom transport execution, token counting, Decimal cost calculation, missing API key detection |
| `TestStructuredOutputValidation` | JSON markdown blocks, raw JSON, Pydantic schema validation (success/failure), required fields checking, malformed JSON |
| `TestErrorHandling` | HTTP 401 auth error without key leak, HTTP 429 rate limit, HTTP 500 API error, timeouts, connection failures |
| `TestLLMLedger` | Call logging, failure logging, cumulative tokens/cost, JSONL file append, export, clear |
| `TestRecordAndReplay` | End-to-end record-then-replay without network, missing replay fixture error |
| `TestFakeLLMAdapter` | In-memory mock adapter, substring response matching, offline test support |

---

## Files Created / Modified

| File | Status | Role |
|---|---|---|
| `src/llm_adapter/__init__.py` | Created | Module exports |
| `src/llm_adapter/exceptions.py` | Created | Exception hierarchy (`LLMAdapterError`, `LLMAPIError`, etc.) |
| `src/llm_adapter/models.py` | Created | `LLMConfig`, `LLMRequest`, `LLMResponse`, `LLMCallRecord` |
| `src/llm_adapter/ledger.py` | Created | `LLMLedger` call tracker and audit logger |
| `src/llm_adapter/adapter.py` | Created | `LLMAdapter` and `FakeLLMAdapter` |
| `tests/test_llm_adapter.py` | Created | Focused automated tests using mocks and fakes |
| `.env.example` | Modified | Added optional blank template variables (`LLM_MODEL`, `LLM_API_BASE`) |
| `docs/ai_log/step-10.md` | Created | This documentation log |

---

## Known Limitations

1. **OpenAI Chat Completion Wire Format**:
   The built-in HTTP transport formats requests as standard OpenAI `/chat/completions` JSON payloads. Other formats (e.g. Anthropic native Messages API or Gemini native REST API) can be used via OpenAI-compatible gateways or custom transport callables.
2. **Synchronous Requests**:
   The adapter currently uses synchronous `urllib.request`. For high-throughput concurrent batch requests, a thread pool or async client can be wrapped around the transport.
3. **No Automatic Retries with Exponential Backoff**:
   To avoid unexpected token charges or long delays, the adapter raises exceptions promptly upon error rather than implementing complex retry loops. Callers (such as the learner) decide how and when to retry repairs (up to 2 times per the project plan).

---

## Terminal Commands to Verify Later

```bash
# Run the LLM adapter unit tests
PYTHONPATH=. ./.venv/bin/pytest tests/test_llm_adapter.py -v

# Run with short tracebacks
PYTHONPATH=. ./.venv/bin/pytest tests/test_llm_adapter.py -v --tb=short

# Run full project test suite excluding legacy fingerprint test
PYTHONPATH=. ./.venv/bin/pytest tests/ -v --ignore=tests/test_fingerprint.py
```
