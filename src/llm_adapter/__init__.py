"""LLM Adapter module for structured, provider-agnostic LLM calls."""
from src.llm_adapter.adapter import FakeLLMAdapter, LLMAdapter
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
from src.llm_adapter.models import (
    LLMCallRecord,
    LLMConfig,
    LLMRequest,
    LLMResponse,
)

__all__ = [
    # Core adapter classes
    "LLMAdapter",
    "FakeLLMAdapter",
    # Config & Models
    "LLMConfig",
    "LLMRequest",
    "LLMResponse",
    "LLMCallRecord",
    "LLMLedger",
    # Exceptions
    "LLMAdapterError",
    "LLMConfigurationError",
    "LLMConnectionError",
    "LLMTimeoutError",
    "LLMAPIError",
    "LLMRateLimitError",
    "LLMResponseValidationError",
    "LLMReplayError",
]
