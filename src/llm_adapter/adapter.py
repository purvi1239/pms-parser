"""LLM Adapter for provider-agnostic, structured LLM requests.

Key design principles:
- Minimal and dependency-free: uses standard library urllib for HTTP.
- Strict isolation: accepts only minimal prompt text, never raw statements or documents.
- Untrusted output: all responses undergo strict schema and JSON validation.
- Audit & Ledger: every call records latency, tokens, cost, and prompt hash.
- Record & Replay: supports deterministic offline tests without network or API keys.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Type, Union

from pydantic import BaseModel, ValidationError

from src.llm_adapter.exceptions import (
    LLMAdapterError,
    LLMAPIError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMReplayError,
    LLMResponseValidationError,
    LLMTimeoutError,
)
from src.llm_adapter.ledger import LLMLedger
from src.llm_adapter.models import LLMCallRecord, LLMConfig, LLMRequest, LLMResponse

_Q6 = Decimal("0.000001")
_ZERO_COST = Decimal("0.000000")

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_local_url(api_base: str) -> bool:
    """Return True when api_base resolves to a loopback address.

    Uses urllib.parse for reliable host extraction rather than string matching.
    Handles IPv4 (127.0.0.1), IPv6 (::1, [::1]), and the 'localhost' hostname.
    """
    try:
        parsed = urllib.parse.urlparse(api_base)
        host = (parsed.hostname or "").lower().strip("[]")
        return host in _LOCAL_HOSTNAMES
    except Exception:
        return False


def _extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Extract a JSON object from raw LLM text (handling markdown code blocks).

    Raises:
        LLMResponseValidationError: If no valid JSON object can be parsed.
    """
    text = raw_text.strip()

    # 1. Check for markdown code fences: ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    candidate = match.group(1).strip() if match else text

    # 2. Try direct json.loads
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. Fallback: find outermost curly braces {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = text[start : end + 1]
        try:
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseValidationError(f"Malformed JSON in response: {exc}") from exc

    raise LLMResponseValidationError("No valid JSON object found in response.")


def _validate_against_schema(
    data: dict[str, Any], schema: Union[dict[str, Any], Type[BaseModel], None]
) -> dict[str, Any]:
    """Validate parsed JSON dictionary against expected schema or Pydantic model.

    Raises:
        LLMResponseValidationError: If required fields are missing or types fail.
    """
    if schema is None:
        return data

    # Case 1: Pydantic model class
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            validated = schema.model_validate(data)
            return validated.model_dump()
        except ValidationError as exc:
            raise LLMResponseValidationError(
                f"Response failed Pydantic schema validation: {exc}"
            ) from exc

    # Case 2: JSON Schema dict
    if isinstance(schema, dict):
        required_fields = schema.get("required", [])
        missing = [f for f in required_fields if f not in data]
        if missing:
            raise LLMResponseValidationError(
                f"Response missing required schema field(s): {', '.join(missing)}"
            )

    return data


class LLMAdapter:
    """Provider-agnostic adapter for sending requests to LLM completion APIs."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        transport: Callable[[dict[str, Any], LLMConfig], dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the LLM adapter.

        Args:
            config: Optional LLMConfig. Defaults to LLMConfig.from_env() if None.
            transport: Optional custom HTTP transport callable for mocking or proxies.
        """
        self.config = config or LLMConfig.from_env()
        self.ledger = LLMLedger(self.config.ledger_path)
        self._transport = transport or self._default_http_transport

    def complete(
        self,
        prompt: str,
        response_schema: Union[dict[str, Any], Type[BaseModel], None] = None,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send a prompt and return a structured LLMResponse.

        Args:
            prompt: Minimal prompt string (financial values/PII must be redacted prior).
            response_schema: Optional dict schema or Pydantic model to enforce.
            system_prompt: Optional instructions for the model.
            temperature: Sampling temperature (default: 0.0 for deterministic output).
            max_tokens: Max output tokens.
            model: Optional model override for this request.
            metadata: Optional auditing metadata (task ID, format ID, etc.).

        Returns:
            LLMResponse: Typed response with structured dict, token usage, cost, and latency.

        Raises:
            LLMAdapterError: On configuration, network, timeout, API, or validation errors.
        """
        request = LLMRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            response_schema=response_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            metadata=metadata or {},
        )
        return self.complete_request(request)

    def complete_request(self, request: LLMRequest) -> LLMResponse:
        """Execute an LLMRequest and return the structured LLMResponse."""
        prompt_hash = request.compute_prompt_hash()
        target_model = request.model or self.config.model

        # -------------------------------------------------------------
        # Replay mode check
        # -------------------------------------------------------------
        if self.config.replay_mode == "replay":
            return self._handle_replay(request, prompt_hash, target_model)

        # -------------------------------------------------------------
        # Live execution (network or custom transport)
        # -------------------------------------------------------------
        payload = self._build_api_payload(request, target_model)
        start_time = time.perf_counter()

        try:
            raw_response = self._transport(payload, self.config)
            latency_ms = (time.perf_counter() - start_time) * 1000.0

            # Extract response content and token counts
            content = self._extract_content(raw_response)
            usage = raw_response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

            if _is_local_url(self.config.api_base):
                cost = _ZERO_COST
            else:
                cost = self._calculate_cost(prompt_tokens, completion_tokens)

            # Parse and validate structured output if expected
            structured: dict[str, Any] | None = None
            if request.response_schema is not None:
                parsed_json = _extract_json_payload(content)
                structured = _validate_against_schema(parsed_json, request.response_schema)
            else:
                # Best-effort JSON extraction if not requested
                try:
                    structured = _extract_json_payload(content)
                except LLMResponseValidationError:
                    structured = None

            response = LLMResponse(
                content=content,
                structured=structured,
                model=target_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                prompt_hash=prompt_hash,
            )

            # Record success in ledger
            self.ledger.record(
                LLMCallRecord(
                    call_id=response.call_id,
                    model=target_model,
                    prompt_hash=prompt_hash,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    success=True,
                    metadata=request.metadata,
                )
            )

            # Record mode: persist response fixture
            if self.config.replay_mode == "record" and self.config.replay_dir:
                self._save_replay_fixture(prompt_hash, response)

            return response

        except Exception as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            # Record failure in ledger
            self.ledger.record(
                LLMCallRecord(
                    call_id="failed",
                    model=target_model,
                    prompt_hash=prompt_hash,
                    latency_ms=latency_ms,
                    success=False,
                    error_message=str(exc),
                    metadata=request.metadata,
                )
            )
            raise

    def _handle_replay(
        self, request: LLMRequest, prompt_hash: str, target_model: str
    ) -> LLMResponse:
        """Retrieve pre-recorded response fixture without network access."""
        if not self.config.replay_dir:
            raise LLMReplayError("Replay mode enabled but 'replay_dir' is not configured.")

        fixture_path = Path(self.config.replay_dir) / f"{prompt_hash}.json"
        if not fixture_path.exists():
            raise LLMReplayError(
                f"No recorded response found for prompt hash '{prompt_hash}' at '{fixture_path}'."
            )

        try:
            with open(fixture_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            response = LLMResponse.model_validate(data)
            response.cost_usd = Decimal("0.000000")
            response.latency_ms = 0.0

            # Validate against schema if schema was requested
            if request.response_schema is not None:
                if response.structured is None:
                    parsed = _extract_json_payload(response.content)
                    response.structured = _validate_against_schema(
                        parsed, request.response_schema
                    )
                else:
                    response.structured = _validate_against_schema(
                        response.structured, request.response_schema
                    )

            # Record replay hit in ledger (0 cost for replayed calls)
            self.ledger.record(
                LLMCallRecord(
                    call_id=response.call_id,
                    model=target_model,
                    prompt_hash=prompt_hash,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    total_tokens=response.total_tokens,
                    cost_usd=Decimal("0.000000"),
                    latency_ms=0.0,
                    success=True,
                    metadata={"replay": True, **request.metadata},
                )
            )
            return response

        except (json.JSONDecodeError, OSError, ValidationError) as exc:
            raise LLMReplayError(f"Corrupt replay fixture '{fixture_path}': {exc}") from exc

    def _save_replay_fixture(self, prompt_hash: str, response: LLMResponse) -> None:
        """Save successful response to replay_dir."""
        try:
            dest = Path(self.config.replay_dir) / f"{prompt_hash}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(response.model_dump_json(indent=2))
        except OSError:
            pass

    def _build_api_payload(self, request: LLMRequest, model: str) -> dict[str, Any]:
        """Construct standard OpenAI-compatible chat completion payload."""
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        # Request JSON output if structured schema is specified
        if request.response_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        return payload

    def _extract_content(self, raw_response: dict[str, Any]) -> str:
        """Extract text content from OpenAI-compatible response object."""
        choices = raw_response.get("choices", [])
        if not choices:
            raise LLMAPIError("LLM response contained no choices.")

        message = choices[0].get("message", {})
        content = message.get("content")
        if content is None:
            raise LLMAPIError("LLM response choice contained no content.")
        return content

    def _calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        """Calculate estimated cost in USD based on configured token rates."""
        cost_p = (
            Decimal(str(prompt_tokens))
            * self.config.cost_per_1k_prompt_tokens
            / Decimal("1000")
        )
        cost_c = (
            Decimal(str(completion_tokens))
            * self.config.cost_per_1k_completion_tokens
            / Decimal("1000")
        )
        return (cost_p + cost_c).quantize(_Q6, rounding=ROUND_HALF_UP)

    def _default_http_transport(
        self, payload: dict[str, Any], config: LLMConfig
    ) -> dict[str, Any]:
        """Execute HTTP POST using Python standard library urllib.

        For local Ollama endpoints (localhost / 127.0.0.1 / ::1) the
        Authorization header is omitted entirely when no API key is set.
        A real API key is still required for every non-local API base URL.
        """
        local = _is_local_url(config.api_base)

        if not local and not config.api_key:
            raise LLMConfigurationError(
                "LLM API key not configured. Set the LLM_API_KEY environment variable."
            )

        url = f"{config.api_base.rstrip('/')}/chat/completions"
        data_bytes = json.dumps(payload).encode("utf-8")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8"))

        except urllib.error.HTTPError as exc:
            # Read error message without revealing Authorization header or secret
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8")
            except Exception:
                pass

            if exc.code in (401, 403):
                raise LLMAPIError(
                    f"Authentication failed (HTTP {exc.code}): Invalid or missing API key.",
                    status_code=exc.code,
                ) from exc
            if exc.code == 429:
                raise LLMRateLimitError(
                    f"Rate limit exceeded (HTTP 429): {error_body[:200]}",
                    status_code=429,
                ) from exc
            raise LLMAPIError(
                f"LLM API returned HTTP {exc.code}: {error_body[:200]}",
                status_code=exc.code,
            ) from exc

        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in str(exc).lower():
                raise LLMTimeoutError(
                    f"Request to LLM timed out after {config.timeout_seconds} seconds."
                ) from exc
            raise LLMConnectionError(
                f"Failed to connect to LLM endpoint at {config.api_base}: {exc}"
            ) from exc


class FakeLLMAdapter(LLMAdapter):
    """In-memory fake LLM adapter for fast, offline unit and integration testing.

    Accepts predefined responses or dynamic handlers without requiring an API key.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        default_response: dict[str, Any] | str | None = None,
        config: LLMConfig | None = None,
    ) -> None:
        cfg = config or LLMConfig(api_key="mock-test-key", replay_mode="disabled")
        super().__init__(config=cfg)
        self._responses = responses or {}
        self._default_response = default_response or '{"status": "ok"}'

    def register_response(self, prompt_substring: str, response: dict[str, Any] | str) -> None:
        """Register a canned response for prompts containing the given substring."""
        self._responses[prompt_substring] = response

    def _default_http_transport(
        self, payload: dict[str, Any], config: LLMConfig
    ) -> dict[str, Any]:
        """Return canned in-memory response matching the prompt."""
        messages = payload.get("messages", [])
        user_prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_prompt = m.get("content", "")
                break

        matched_resp = None
        for key, resp in self._responses.items():
            if key in user_prompt:
                matched_resp = resp
                break

        if matched_resp is None:
            matched_resp = self._default_response

        # Format content string
        if isinstance(matched_resp, dict):
            content_str = json.dumps(matched_resp)
        else:
            content_str = str(matched_resp)

        return {
            "id": "mock-call-id",
            "model": payload.get("model", "mock-model"),
            "choices": [{"message": {"role": "assistant", "content": content_str}}],
            "usage": {
                "prompt_tokens": len(user_prompt) // 4,
                "completion_tokens": len(content_str) // 4,
                "total_tokens": (len(user_prompt) + len(content_str)) // 4,
            },
        }
