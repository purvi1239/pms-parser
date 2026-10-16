"""Data models for LLM requests, responses, configuration, and call ledger entries."""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    """Configuration for LLM API connection, tolerances, and replay mode."""

    model_config = ConfigDict(validate_assignment=True)

    api_key: str | None = None
    api_base: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout_seconds: float = 120.0
    max_retries: int = 2
    cost_per_1k_prompt_tokens: Decimal = Decimal("0.00015")
    cost_per_1k_completion_tokens: Decimal = Decimal("0.00060")
    replay_mode: str = "disabled"  # "disabled", "record", "replay"
    replay_dir: Union[str, Path, None] = None
    ledger_path: Union[str, Path, None] = None

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_local_url(api_base: str) -> bool:
    """Return True when api_base resolves to a loopback address.

    Handles IPv4 (127.0.0.1), IPv6 (::1, [::1]), and the 'localhost' hostname.
    """
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(api_base)
        host = (parsed.hostname or "").lower().strip("[]")
        return host in _LOCAL_HOSTNAMES
    except Exception:
        return False


    @classmethod
    def from_env(cls) -> LLMConfig:
        """Create an LLMConfig populated from standard environment variables.

        Supported variables:
        - LLM_API_KEY: Provider API key
        - LLM_MODEL: Model name (default: llama3.2:3b for local endpoints, gpt-4o-mini for remote)
        - LLM_API_BASE: Custom API base URL
        - LLM_TIMEOUT: Request timeout in seconds
        - LLM_REPLAY_MODE: "disabled", "record", or "replay"
        - LLM_REPLAY_DIR: Path to store/load replay fixtures
        - LLM_LEDGER_PATH: Path to append JSONL call records
        """
        raw_timeout = os.getenv("LLM_TIMEOUT")
        timeout_val = float(raw_timeout) if raw_timeout else 120.0

        api_base = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")
        model = os.getenv("LLM_MODEL")
        if not model:
            if _is_local_url(api_base):
                model = "llama3.2:3b"
            else:
                model = "gpt-4o-mini"

        return cls(
            api_key=os.getenv("LLM_API_KEY"),
            api_base=api_base,
            model=model,
            timeout_seconds=timeout_val,
            replay_mode=os.getenv("LLM_REPLAY_MODE", "disabled"),
            replay_dir=os.getenv("LLM_REPLAY_DIR"),
            ledger_path=os.getenv("LLM_LEDGER_PATH"),
        )

    def __repr__(self) -> str:
        """Ensure API keys are never leaked in logs or repr."""
        masked_key = "***" if self.api_key else None
        return (
            f"LLMConfig(api_key={masked_key!r}, api_base={self.api_base!r}, "
            f"model={self.model!r}, timeout_seconds={self.timeout_seconds}, "
            f"replay_mode={self.replay_mode!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class LLMRequest(BaseModel):
    """Encapsulates a minimal prompt request to an LLM.

    Only the minimum necessary structural snippet should be provided in prompt.
    Financial statements and personal data must be redacted before sending.
    """

    model_config = ConfigDict(validate_assignment=True, arbitrary_types_allowed=True)

    prompt: str
    system_prompt: str | None = None
    response_schema: Any = None  # Dict JSON Schema or Pydantic model class
    temperature: float = 0.0
    max_tokens: int = 4096
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt")
    @classmethod
    def validate_non_empty_prompt(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("LLM prompt must not be empty or whitespace.")
        return v

    def compute_prompt_hash(self) -> str:
        """Compute deterministic SHA-256 hash of the request content."""
        hasher = hashlib.sha256()
        if self.system_prompt:
            hasher.update(self.system_prompt.encode("utf-8"))
            hasher.update(b"\n---\n")
        hasher.update(self.prompt.encode("utf-8"))
        return hasher.hexdigest()


class LLMResponse(BaseModel):
    """Response returned by the LLM adapter.

    Structured data is treated as untrusted and must be verified against
    domain models and validation rules before use.
    """

    model_config = ConfigDict(validate_assignment=True)

    content: str
    structured: dict[str, Any] | None = None
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal("0.000000")
    latency_ms: float = 0.0
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prompt_hash: str = ""

    def __getitem__(self, key: str) -> Any:
        """Allow dict-style key access to structured output."""
        if self.structured is not None and key in self.structured:
            return self.structured[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dict-style .get() access to structured output."""
        if self.structured is not None:
            return self.structured.get(key, default)
        return default

    def __contains__(self, key: str) -> bool:
        """Support 'key in response' for structured output."""
        return self.structured is not None and key in self.structured


class LLMCallRecord(BaseModel):
    """Entry in the LLM call ledger for auditing, cost tracking, and replay."""

    model_config = ConfigDict(validate_assignment=True)

    call_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model: str
    prompt_hash: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal("0.000000")
    latency_ms: float = 0.0
    success: bool = True
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
