"""LLM call ledger for recording token usage, cost, latency, and audit history."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Sequence, Union

from src.llm_adapter.models import LLMCallRecord


class LLMLedger:
    """Maintains an in-memory audit log of all LLM calls with optional disk append."""

    def __init__(self, ledger_path: Union[str, Path, None] = None) -> None:
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self._records: list[LLMCallRecord] = []

    @property
    def records(self) -> list[LLMCallRecord]:
        """Return a shallow copy of all recorded calls."""
        return list(self._records)

    def record(self, entry: LLMCallRecord) -> None:
        """Add a call record to the ledger and optionally append to file."""
        self._records.append(entry)

        if self.ledger_path:
            self._append_to_file(entry)

    def _append_to_file(self, entry: LLMCallRecord) -> None:
        """Append record as a single JSON line."""
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                # Use model_dump_json to serialize datetimes and Decimals properly
                f.write(entry.model_dump_json() + "\n")
        except OSError:
            pass  # Failure to write disk ledger must not crash parsing pipeline

    def total_cost(self) -> Decimal:
        """Calculate cumulative cost in USD across all recorded calls."""
        return sum((r.cost_usd for r in self._records), Decimal("0.000000"))

    def total_tokens(self) -> int:
        """Calculate total tokens consumed (prompt + completion) across all calls."""
        return sum(r.total_tokens for r in self._records)

    def total_calls(self) -> int:
        """Total number of attempted calls."""
        return len(self._records)

    def successful_calls(self) -> int:
        """Number of successful calls."""
        return sum(1 for r in self._records if r.success)

    def failed_calls(self) -> int:
        """Number of failed calls."""
        return sum(1 for r in self._records if not r.success)

    def clear(self) -> None:
        """Reset in-memory records."""
        self._records.clear()

    def export_jsonl(self, path: Union[str, Path]) -> None:
        """Export in-memory ledger records to a JSONL file."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            for r in self._records:
                f.write(r.model_dump_json() + "\n")
