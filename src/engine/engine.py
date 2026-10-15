"""Deterministic document parsing engine mapping DocumentIR to domain models via ParsingSpec."""
from __future__ import annotations

from typing import Any

from src.engine.converters import clean_text, to_decimal
from src.engine.locators import extract_field_value
from src.engine.table_parser import parse_holdings_table, parse_transactions_table
from src.engine.text_section_parser import (
    parse_text_section_holdings,
    parse_text_section_transactions,
)
from src.ir.models import DocumentIR
from src.models.enums import DocumentStatus
from src.models.models import ParseResult, Statement, Summary
from src.specs.models import ParsingSpec


class ParsingEngine:
    """Engine for executing deterministic parsing specifications against DocumentIR."""

    def __init__(self) -> None:
        pass

    def parse(self, doc: DocumentIR, spec: ParsingSpec) -> ParseResult:
        """Parse an extracted DocumentIR using a ParsingSpec into a structured ParseResult.

        Args:
            doc: The extracted intermediate representation of the document.
            spec: The parsing specification defining field and table locators.

        Returns:
            ParseResult containing the Statement, Holdings, Transactions, Summary,
            status, warnings, errors, and metadata.
        """
        warnings: list[str] = []
        errors: list[str] = []

        # ── 1. Metadata Fields Extraction ────────────────────────────────────
        metadata_values: dict[str, Any] = {}
        for field_name, locator in spec.metadata_fields.items():
            raw_val, field_warns = extract_field_value(doc, locator, field_name=field_name)
            warnings.extend(field_warns)

            cleaned_val = clean_text(raw_val)
            if cleaned_val is None and locator.default is not None:
                cleaned_val = locator.default

            if locator.required and cleaned_val is None:
                warnings.append(f"Required metadata field '{field_name}' could not be located.")

            metadata_values[field_name] = cleaned_val

        # ── 2. Summary Fields Extraction ─────────────────────────────────────
        summary_values: dict[str, Any] = {}
        for field_name, locator in spec.summary_fields.items():
            raw_val, field_warns = extract_field_value(doc, locator, field_name=field_name)
            warnings.extend(field_warns)

            target_val = raw_val if raw_val is not None else locator.default
            if target_val is not None:
                try:
                    summary_values[field_name] = to_decimal(target_val)
                except ValueError as err:
                    warnings.append(f"Summary field '{field_name}': {err}")
                    summary_values[field_name] = None
            else:
                summary_values[field_name] = None

        # Construct Summary model if any field was extracted
        summary_obj: Summary | None = None
        if any(v is not None for v in summary_values.values()):
            # Filter only valid Summary fields
            valid_summary_keys = {
                "opening_value",
                "closing_value",
                "opening_cash",
                "closing_cash",
                "fees_charged",
                "income",
                "realized_gains",
                "unrealized_gains",
                "units",
                "nav_per_unit",
            }
            filtered_summary = {k: v for k, v in summary_values.items() if k in valid_summary_keys}
            summary_obj = Summary(**filtered_summary)

        # ── 3. Holdings Table Extraction ─────────────────────────────────────
        holdings = []
        if spec.holdings_table is not None:
            h_list, h_warns = parse_holdings_table(doc, spec.holdings_table)
            warnings.extend(h_warns)
            holdings = h_list

        # Fallback: text-section extraction when no table rows found
        if not holdings and spec.text_holdings is not None:
            tsl = spec.text_holdings
            h_list, h_warns = parse_text_section_holdings(
                doc,
                anchor=tsl.anchor,
                stop_anchor=tsl.stop_anchor,
                row_pattern=tsl.row_pattern,
                column_mapping=tsl.column_mapping,
                start_page=tsl.page_number,
                skip_patterns=tsl.skip_patterns,
            )
            warnings.extend(h_warns)
            holdings = h_list

        # ── 4. Transactions Table Extraction ─────────────────────────────────
        transactions = []
        if spec.transactions_table is not None:
            t_list, t_warns = parse_transactions_table(doc, spec.transactions_table)
            warnings.extend(t_warns)
            transactions = t_list

        # Fallback: text-section extraction when no table rows found
        if not transactions and spec.text_transactions is not None:
            tsl = spec.text_transactions
            t_list, t_warns = parse_text_section_transactions(
                doc,
                anchor=tsl.anchor,
                stop_anchor=tsl.stop_anchor,
                row_pattern=tsl.row_pattern,
                column_mapping=tsl.column_mapping,
                start_page=tsl.page_number,
                skip_patterns=tsl.skip_patterns,
            )
            warnings.extend(t_warns)
            transactions = t_list

        # ── 5. Assemble Domain Statement ─────────────────────────────────────
        provider_name = metadata_values.get("provider_name")
        strategy_name = metadata_values.get("strategy_name")
        client_id = metadata_values.get("client_identifier")
        statement_period = metadata_values.get("statement_period")
        currency = metadata_values.get("currency")

        statement = Statement(
            provider_name=provider_name,
            strategy_name=strategy_name,
            client_identifier=client_id,
            statement_period=statement_period,
            currency=currency,
            statement_type=spec.statement_type,
            holdings=holdings,
            transactions=transactions,
            summary=summary_obj,
        )

        # ── 6. Status Determination ──────────────────────────────────────────
        status = DocumentStatus.PARSED
        if warnings:
            status = DocumentStatus.PARSED_WITH_WARNINGS

        return ParseResult(
            statement=statement,
            holdings=holdings,
            transactions=transactions,
            summary=summary_obj,
            status=status,
            warnings=warnings,
            errors=errors,
            raw_metadata=doc.metadata,
        )


def parse_document(doc: DocumentIR, spec: ParsingSpec) -> ParseResult:
    """Convenience function to parse a DocumentIR with a given ParsingSpec."""
    engine = ParsingEngine()
    return engine.parse(doc, spec)
