"""Tests for Step 9: LLM Adapter.

All tests use in-memory mocks, injected transports, or FakeLLMAdapter.
Zero real network requests or real API credentials required.
"""
from __future__ import annotations

import json
import socket
import urllib.error
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from src.llm_adapter import (
    FakeLLMAdapter,
    LLMAdapter,
    LLMAdapterError,
    LLMAPIError,
    LLMCallRecord,
    LLMConfig,
    LLMConfigurationError,
    LLMConnectionError,
    LLMLedger,
    LLMRateLimitError,
    LLMReplayError,
    LLMRequest,
    LLMResponse,
    LLMResponseValidationError,
    LLMTimeoutError,
)

D = Decimal


# ---------------------------------------------------------------------------
# Test Schemas for Structured Output
# ---------------------------------------------------------------------------

class SampleSpecDraft(BaseModel):
    format_name: str
    version: int = 1
    tables_count: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 1. Configuration & Secrets Masking
# ---------------------------------------------------------------------------

class TestLLMConfig:
    def test_default_config(self):
        cfg = LLMConfig()
        assert cfg.api_key is None
        assert cfg.api_base == "https://api.openai.com/v1"
        assert cfg.model == "gpt-4o-mini"
        assert cfg.timeout_seconds == 30.0
        assert cfg.replay_mode == "disabled"
        assert cfg.max_retries == 2

    def test_config_from_env(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-12345")
        monkeypatch.setenv("LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("LLM_API_BASE", "https://custom.endpoint.com/v1")
        monkeypatch.setenv("LLM_TIMEOUT", "45.5")
        monkeypatch.setenv("LLM_REPLAY_MODE", "replay")
        monkeypatch.setenv("LLM_REPLAY_DIR", "/tmp/replays")
        monkeypatch.setenv("LLM_LEDGER_PATH", "/tmp/ledger.jsonl")

        cfg = LLMConfig.from_env()
        assert cfg.api_key == "sk-test-secret-12345"
        assert cfg.model == "gpt-4o"
        assert cfg.api_base == "https://custom.endpoint.com/v1"
        assert cfg.timeout_seconds == 45.5
        assert cfg.replay_mode == "replay"
        assert cfg.replay_dir == "/tmp/replays"
        assert cfg.ledger_path == "/tmp/ledger.jsonl"

    def test_secret_masking_in_repr_and_str(self):
        cfg = LLMConfig(api_key="secret-key-super-confidential")
        repr_str = repr(cfg)
        str_str = str(cfg)

        assert "secret-key-super-confidential" not in repr_str
        assert "secret-key-super-confidential" not in str_str
        assert "***" in repr_str
        assert "***" in str_str


# ---------------------------------------------------------------------------
# 2. Request & Response Models
# ---------------------------------------------------------------------------

class TestLLMRequestResponse:
    def test_request_validation(self):
        req = LLMRequest(prompt="Extract columns from table")
        assert req.prompt == "Extract columns from table"
        assert req.temperature == 0.0

        with pytest.raises(ValueError, match="must not be empty"):
            LLMRequest(prompt="   ")

    def test_prompt_hash_deterministic(self):
        req1 = LLMRequest(prompt="Hello", system_prompt="Sys")
        req2 = LLMRequest(prompt="Hello", system_prompt="Sys")
        req3 = LLMRequest(prompt="Hello", system_prompt="Different Sys")

        assert req1.compute_prompt_hash() == req2.compute_prompt_hash()
        assert req1.compute_prompt_hash() != req3.compute_prompt_hash()

    def test_response_dict_access(self):
        resp = LLMResponse(
            content='{"key": "value", "count": 42}',
            structured={"key": "value", "count": 42},
            model="gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cost_usd=D("0.000015"),
            latency_ms=120.5,
            prompt_hash="abc123hash",
        )

        assert resp["key"] == "value"
        assert resp["count"] == 42
        assert resp.get("key") == "value"
        assert resp.get("missing", "default") == "default"
        assert "key" in resp
        assert "missing" not in resp

        with pytest.raises(KeyError):
            _ = resp["missing"]


# ---------------------------------------------------------------------------
# 3. LLMAdapter Execution with Custom Transports
# ---------------------------------------------------------------------------

class TestLLMAdapterExecution:
    def test_complete_with_injected_transport(self):
        def mock_transport(payload: dict[str, Any], config: LLMConfig) -> dict[str, Any]:
            assert payload["model"] == "gpt-4o-mini"
            assert payload["messages"][-1]["content"] == "Summarize headers"
            return {
                "id": "call-1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"summary": "table headers"}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }

        cfg = LLMConfig(api_key="mock-key")
        adapter = LLMAdapter(config=cfg, transport=mock_transport)

        resp = adapter.complete(prompt="Summarize headers")
        assert resp.content == '{"summary": "table headers"}'
        assert resp.structured == {"summary": "table headers"}
        assert resp.prompt_tokens == 100
        assert resp.completion_tokens == 50
        assert resp.total_tokens == 150
        assert resp.cost_usd > D("0.000000")
        assert resp.latency_ms >= 0.0
        assert len(adapter.ledger.records) == 1
        assert adapter.ledger.records[0].success is True

    def test_missing_api_key_raises_configuration_error(self):
        cfg = LLMConfig(api_key=None)
        adapter = LLMAdapter(config=cfg)

        with pytest.raises(LLMConfigurationError, match="API key not configured"):
            adapter.complete(prompt="Hello")

    def test_cost_calculation(self):
        cfg = LLMConfig(
            api_key="mock-key",
            cost_per_1k_prompt_tokens=D("0.00100"),
            cost_per_1k_completion_tokens=D("0.00200"),
        )
        adapter = LLMAdapter(config=cfg)
        cost = adapter._calculate_cost(prompt_tokens=2000, completion_tokens=1000)
        # 2000 * 0.001 / 1000 = 0.002; 1000 * 0.002 / 1000 = 0.002; Total = 0.004000
        assert cost == D("0.004000")


# ---------------------------------------------------------------------------
# 4. Structured Output Extraction & Validation
# ---------------------------------------------------------------------------

class TestStructuredOutputValidation:
    def test_extract_json_markdown_block(self):
        def transport(payload, config):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Here is the result:\n```json\n{\"tables\": 2, \"valid\": true}\n```\nHope it helps!"
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport)
        resp = adapter.complete(prompt="Find tables")
        assert resp.structured == {"tables": 2, "valid": True}

    def test_pydantic_schema_validation_success(self):
        def transport(payload, config):
            assert payload.get("response_format") == {"type": "json_object"}
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"format_name": "Format A", "tables_count": 3})
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport)
        resp = adapter.complete(prompt="Draft spec", response_schema=SampleSpecDraft)
        assert resp.structured == {"format_name": "Format A", "version": 1, "tables_count": 3}

    def test_pydantic_schema_validation_failure(self):
        def transport(payload, config):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"format_name": "Format A", "tables_count": -5})
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport)
        with pytest.raises(LLMResponseValidationError, match="schema validation"):
            adapter.complete(prompt="Draft spec", response_schema=SampleSpecDraft)

    def test_dict_schema_required_fields_missing(self):
        schema = {"required": ["anchor", "offset_x"]}

        def transport(payload, config):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"anchor": "Portfolio Valuation"})
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport)
        with pytest.raises(LLMResponseValidationError, match="missing required schema field.*offset_x"):
            adapter.complete(prompt="Get locators", response_schema=schema)

    def test_malformed_json_raises_validation_error(self):
        def transport(payload, config):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "I am not able to generate a JSON response for this."
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

        schema = {"required": ["status"]}
        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport)
        with pytest.raises(LLMResponseValidationError, match="No valid JSON object found"):
            adapter.complete(prompt="Draft spec", response_schema=schema)


# ---------------------------------------------------------------------------
# 5. Error Handling & Sanitization
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_http_401_auth_error_sanitized(self):
        def transport_401(payload, config):
            raise LLMAPIError("Authentication failed (HTTP 401)", status_code=401)

        adapter = LLMAdapter(config=LLMConfig(api_key="leaked-key"), transport=transport_401)
        with pytest.raises(LLMAPIError) as exc_info:
            adapter.complete(prompt="Test auth")

        assert exc_info.value.status_code == 401
        assert "leaked-key" not in str(exc_info.value)
        assert adapter.ledger.records[-1].success is False

    def test_http_429_rate_limit_error(self):
        def transport_429(payload, config):
            raise LLMRateLimitError("Rate limit exceeded (HTTP 429)", status_code=429)

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport_429)
        with pytest.raises(LLMRateLimitError) as exc_info:
            adapter.complete(prompt="Test rate limit")

        assert exc_info.value.status_code == 429
        assert adapter.ledger.records[-1].success is False

    def test_timeout_error(self):
        def transport_timeout(payload, config):
            raise LLMTimeoutError("Request timed out after 30s")

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport_timeout)
        with pytest.raises(LLMTimeoutError):
            adapter.complete(prompt="Test timeout")

        assert adapter.ledger.records[-1].success is False
        assert "timed out" in adapter.ledger.records[-1].error_message.lower()

    def test_connection_error(self):
        def transport_conn(payload, config):
            raise LLMConnectionError("Failed to connect: Connection refused")

        adapter = LLMAdapter(config=LLMConfig(api_key="mock-key"), transport=transport_conn)
        with pytest.raises(LLMConnectionError):
            adapter.complete(prompt="Test conn")

        assert adapter.ledger.records[-1].success is False


# ---------------------------------------------------------------------------
# 6. LLMLedger Auditing & Totals
# ---------------------------------------------------------------------------

class TestLLMLedger:
    def test_ledger_tracking_and_totals(self, tmp_path):
        ledger_file = tmp_path / "test_ledger.jsonl"
        ledger = LLMLedger(ledger_path=ledger_file)

        entry1 = LLMCallRecord(
            call_id="call-1",
            model="gpt-4o-mini",
            prompt_hash="h1",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost_usd=D("0.000100"),
            latency_ms=100.0,
            success=True,
        )
        entry2 = LLMCallRecord(
            call_id="call-2",
            model="gpt-4o-mini",
            prompt_hash="h2",
            prompt_tokens=200,
            completion_tokens=100,
            total_tokens=300,
            cost_usd=D("0.000200"),
            latency_ms=150.0,
            success=False,
            error_message="timeout",
        )

        ledger.record(entry1)
        ledger.record(entry2)

        assert ledger.total_calls() == 2
        assert ledger.successful_calls() == 1
        assert ledger.failed_calls() == 1
        assert ledger.total_tokens() == 450
        assert ledger.total_cost() == D("0.000300")

        # Verify disk file written as jsonl
        assert ledger_file.exists()
        lines = ledger_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        loaded = json.loads(lines[0])
        assert loaded["call_id"] == "call-1"

        # Export test
        export_file = tmp_path / "exported.jsonl"
        ledger.export_jsonl(export_file)
        assert export_file.exists()

        # Clear test
        ledger.clear()
        assert ledger.total_calls() == 0


# ---------------------------------------------------------------------------
# 7. Record and Replay
# ---------------------------------------------------------------------------

class TestRecordAndReplay:
    def test_record_then_replay(self, tmp_path):
        replay_dir = tmp_path / "replays"
        prompt = "Extract anchor for Format B"

        # 1. Record mode
        def transport(payload, config):
            return {
                "choices": [{"message": {"content": '{"anchor": "HOLDINGS SUMMARY"}'}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            }

        record_cfg = LLMConfig(
            api_key="mock-key",
            replay_mode="record",
            replay_dir=replay_dir,
        )
        record_adapter = LLMAdapter(config=record_cfg, transport=transport)
        resp1 = record_adapter.complete(prompt=prompt)
        assert resp1.structured == {"anchor": "HOLDINGS SUMMARY"}

        # Verify fixture file created on disk
        req = LLMRequest(prompt=prompt)
        hash_val = req.compute_prompt_hash()
        fixture_file = replay_dir / f"{hash_val}.json"
        assert fixture_file.exists()

        # 2. Replay mode (NO transport provided, NO API key required)
        replay_cfg = LLMConfig(
            api_key=None,
            replay_mode="replay",
            replay_dir=replay_dir,
        )
        replay_adapter = LLMAdapter(config=replay_cfg)
        resp2 = replay_adapter.complete(prompt=prompt)
        assert resp2.structured == {"anchor": "HOLDINGS SUMMARY"}
        assert resp2.cost_usd == D("0.000000")  # Replayed calls cost $0

    def test_missing_replay_fixture_raises_error(self, tmp_path):
        replay_cfg = LLMConfig(
            api_key=None,
            replay_mode="replay",
            replay_dir=tmp_path / "empty_replays",
        )
        replay_adapter = LLMAdapter(config=replay_cfg)
        with pytest.raises(LLMReplayError, match="No recorded response found"):
            replay_adapter.complete(prompt="Unknown prompt that was never recorded")


# ---------------------------------------------------------------------------
# 8. FakeLLMAdapter (Offline Mocking for Tests)
# ---------------------------------------------------------------------------

class TestFakeLLMAdapter:
    def test_fake_adapter_substring_matching(self):
        fake = FakeLLMAdapter()
        fake.register_response("Format A", {"format": "A", "columns": ["Security", "ISIN"]})
        fake.register_response("Format B", {"format": "B", "columns": ["Description", "Qty"]})

        resp_a = fake.complete(prompt="Please inspect Format A table")
        assert resp_a.structured["format"] == "A"
        assert resp_a.structured["columns"] == ["Security", "ISIN"]

        resp_b = fake.complete(prompt="Please inspect Format B table")
        assert resp_b.structured["format"] == "B"

        resp_default = fake.complete(prompt="Unregistered prompt")
        assert resp_default.structured == {"status": "ok"}

        assert fake.ledger.total_calls() == 3
        assert fake.ledger.successful_calls() == 3
