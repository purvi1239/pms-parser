"""Build and register parsing specifications for synthetic evaluation samples.

This module provides :func:`build_synthetic_spec_store`, which constructs
``ParsingSpec`` objects for every format family in ``samples/synthetic/``,
extracts live fingerprints from the companion PDFs, and saves all specs into
a caller-supplied ``SpecStore`` (typically backed by a temporary directory).

Design principles
-----------------
* **Isolation**: specs are registered into the supplied ``SpecStore`` only.
  The production ``specs/`` directory is never read or written.
* **Determinism**: fingerprints are extracted from the actual synthetic PDFs
  using the same ``extract_fingerprint(DocumentIR)`` function used by the
  pipeline, so the match threshold seen at evaluation time is identical to
  what would be seen in production.
* **Single source of truth**: locator anchors and column names are taken
  directly from the format-builder source files and their docstrings.
  No values are invented independently of the generator code.
* **No side effects**: the function never touches the DB or the file system
  beyond reading the PDFs and writing into the supplied ``SpecStore``.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from src.fingerprint.extractor import extract_fingerprint
from src.ir.extractor import extract_pdf
from src.models.enums import StatementType
from src.sample_generator.generator import ENCRYPTED_SAMPLE_LABEL, ENCRYPTED_SAMPLE_PASSWORD
from src.specs.models import FieldLocator, LocatorType, ParsingSpec, TableLocator
from src.specs.store import SpecStore


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _SpecDef(NamedTuple):
    """Thin carrier: PDF label → spec to register."""

    pdf_label: str          # matches the filename stem in samples/synthetic/
    spec: ParsingSpec


def _locate_samples_dir() -> Path:
    """Return the absolute path to samples/synthetic/ next to the project root."""
    # src/eval/synthetic_specs.py → src/eval → src → project_root
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "samples" / "synthetic"


# ---------------------------------------------------------------------------
# Format A / A-messy / A-drift spec builder
# ---------------------------------------------------------------------------
#
# All three variants share the same structural anchors (section headings,
# meta-block labels, holdings table headers, summary label text).  The
# Format-A spec is therefore used for all three: the fingerprints extracted
# from their actual PDFs will each be registered under different format_ids
# so the pipeline can match them individually.
#
# Format A PDF layout (from format_a.py docstring):
#   Title: "Portfolio Management Statement"
#   Meta block (4-col table): "Client ID:" | value | "Statement Period:" | value
#                              "Currency:"  | value | "Statement Type:"  | value
#   Section: "Holdings"
#     Columns: Security Name | ISIN | Qty | Cost (INR) | Mkt Value (INR) | Weight % | (empty)
#   Section: "Transactions"
#     Columns: Date | Type | Security | Qty | Amount (INR)
#   Section: "Portfolio Summary"
#     2-col label/value table, anchors: Opening Portfolio Value, Closing Portfolio Value,
#     Opening Cash Balance, Closing Cash Balance, Fees Charged,
#     Income (Dividends/Int.), Realised Gains, Unrealised Gains
#
# Format A-messy (from format_a_messy.py docstring):
#   Same anchors as Format A, multi-page holdings table with repeated header rows.
#
# Format A-drift (from format_a_drift.py docstring):
#   Title renamed: "Portfolio Statement Report"
#   Holdings column "Mkt Value (INR)" → "Net Value (INR)"
#   Holdings columns reordered: Sec | Qty | Cost | Net Value | Weight | ISIN
#   Summary section renamed: "Account Summary"  (same row labels)


def _build_format_a_spec(
    format_id: str,
    statement_type: StatementType = StatementType.PMS,
    description: str = "",
) -> ParsingSpec:
    """Return a ParsingSpec for the Format-A family layout."""
    return ParsingSpec(
        format_id=format_id,
        version="1.0.0",
        statement_type=statement_type,
        description=description,
        metadata_fields={
            "provider_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                # Matches lines like "Pinnacle Wealth PMS — India Growth Strategy"
                # or any provider name up to the em-dash separator
                pattern=r"^(.+?)\s+[—\-]{1,2}\s+.+$",
            ),
            "strategy_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                # Matches the part after the em-dash on the subtitle line
                pattern=r"^.+?\s+[—\-]{1,2}\s+(.+)$",
            ),
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Client ID:",
            ),
            "statement_period": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Statement Period:",
            ),
            "currency": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Currency:",
            ),
        },
        summary_fields={
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Portfolio Value",
                offset_x=500.0,
            ),
            "closing_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Portfolio Value",
                offset_x=500.0,
            ),
            "opening_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Cash Balance",
                offset_x=500.0,
            ),
            "closing_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Cash Balance",
                offset_x=500.0,
            ),
            "fees_charged": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Fees Charged",
                offset_x=500.0,
            ),
            "income": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Income",
                offset_x=500.0,
            ),
            "realized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Realised Gains",
                offset_x=500.0,
            ),
            "unrealized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Unrealised Gains",
                offset_x=500.0,
            ),
        },
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Holdings",
            header_keywords=["Security Name", "ISIN", "Qty", "Cost", "Weight"],
            column_mapping={
                "security_name": "Security Name",
                "isin": "ISIN",
                "quantity": "Qty",
                "cost": "Cost (INR)",
                "market_value": "Mkt Value (INR)",
                "weight": "Weight %",
            },
            multi_page=True,
        ),
        transactions_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Transactions",
            header_keywords=["Date", "Type", "Security", "Qty", "Amount"],
            column_mapping={
                "date": "Date",
                "type": "Type",
                "security": "Security",
                "quantity": "Qty",
                "amount": "Amount (INR)",
            },
            multi_page=True,
        ),
    )


def _build_format_a_drift_spec(format_id: str = "synthetic-format-a-drift") -> ParsingSpec:
    """Return a ParsingSpec for the Format-A-drift layout.

    Differences from Format A (from format_a_drift.py docstring):
    * Holdings column renamed: "Mkt Value (INR)" → "Net Value (INR)"
    * Holdings columns reordered: Security | Qty | Cost | Net Value | Weight | ISIN
    * Summary section renamed: "Account Summary" (row labels unchanged)
    """
    return ParsingSpec(
        format_id=format_id,
        version="1.0.0",
        statement_type=StatementType.PMS,
        description="Format-A drift variant: reordered holdings columns, renamed summary heading",
        metadata_fields={
            "provider_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                pattern=r"^(.+?)\s+[—\-]{1,2}\s+.+$",
            ),
            "strategy_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                pattern=r"^.+?\s+[—\-]{1,2}\s+(.+)$",
            ),
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Client ID:",
            ),
            "statement_period": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Statement Period:",
            ),
            "currency": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Currency:",
            ),
        },
        summary_fields={
            # Summary section is renamed "Account Summary" but row labels are the same
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Portfolio Value",
                offset_x=500.0,
            ),
            "closing_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Portfolio Value",
                offset_x=500.0,
            ),
            "opening_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Cash Balance",
                offset_x=500.0,
            ),
            "closing_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Cash Balance",
                offset_x=500.0,
            ),
            "fees_charged": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Fees Charged",
                offset_x=500.0,
            ),
            "income": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Income",
                offset_x=500.0,
            ),
            "realized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Realised Gains",
                offset_x=500.0,
            ),
            "unrealized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Unrealised Gains",
                offset_x=500.0,
            ),
        },
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Holdings",
            # Drift reorders and renames columns; use keywords that are still present
            header_keywords=["Security Name", "ISIN", "Qty", "Cost", "Weight"],
            column_mapping={
                "security_name": "Security Name",
                "isin": "ISIN",
                "quantity": "Qty",
                "cost": "Cost (INR)",
                # "Net Value (INR)" is the renamed column for market_value
                "market_value": "Net Value (INR)",
                "weight": "Weight %",
            },
            multi_page=True,
        ),
        transactions_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Transactions",
            header_keywords=["Date", "Type", "Security", "Qty", "Amount"],
            column_mapping={
                "date": "Date",
                "type": "Type",
                "security": "Security",
                "quantity": "Qty",
                "amount": "Amount (INR)",
            },
            multi_page=True,
        ),
    )


# ---------------------------------------------------------------------------
# Format B spec builder
# ---------------------------------------------------------------------------
#
# Format B PDF layout (from format_b.py docstring):
#   Title: "Client Portfolio Report"
#   Meta block (4-col table): "Account Number:" | val | "Report Period:" | val
#                              "Base Currency:" | val | "Account Type:" | val
#   Section: "Portfolio Holdings"
#     Columns: No. | Scrip Name | ISIN Code | Units | Purchase Cost | Current Value | Allocation %
#   Section: "Trade Ledger"
#     Columns: Trade Date | Activity | Instrument | Units | Consideration (INR)
#   Section: "Fund Summary"
#     **Horizontal** 2-row table: labels in row 0, values in row 1
#     Labels: Opening Value | Closing Value | Opening Cash | Closing Cash |
#             Fees Charged | Income | Realised Gains | Unrealised Gains
#
# The horizontal summary layout means VALUE_RIGHT_OF_ANCHOR cannot read
# individual cells.  Instead we use TABLE_AFTER_ANCHOR with positional
# column indices (0, 1, 2, …) so each label column maps to its value.


def _build_format_b_spec(format_id: str = "synthetic-format-b") -> ParsingSpec:
    """Return a ParsingSpec for the Format-B layout."""
    return ParsingSpec(
        format_id=format_id,
        version="1.0.0",
        statement_type=StatementType.PMS,
        description="Format B: Client Portfolio Report with horizontal summary and Trade Ledger",
        metadata_fields={
            # Format B: provider_name and strategy_name appear as separate subtitle
            # paragraphs on lines 2 and 3 after the "Client Portfolio Report" title.
            "provider_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                # The line immediately after the title (no em-dash combinator)
                pattern=r"Client Portfolio Report\s*\n([^\n]+)",
            ),
            "strategy_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                # The line immediately after the provider_name line
                pattern=r"Client Portfolio Report\s*\n[^\n]+\n([^\n]+)",
            ),
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Account Number:",
            ),
            "statement_period": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Report Period:",
            ),
            "currency": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Base Currency:",
            ),
        },
        summary_fields={
            # Format B summary is a horizontal 2-row table: labels in row 0, values in row 1.
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Opening Value",
            ),
            "closing_value": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Closing Value",
            ),
            "opening_cash": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Opening Cash",
            ),
            "closing_cash": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Closing Cash",
            ),
            "fees_charged": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Fees Charged",
            ),
            "income": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Income",
            ),
            "realized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Realised Gains",
            ),
            "unrealized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_BELOW_ANCHOR,
                anchor="Unrealised Gains",
            ),
        },
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Portfolio Holdings",
            header_keywords=["Scrip Name", "ISIN Code", "Units", "Purchase Cost", "Current Value"],
            column_mapping={
                "security_name": "Scrip Name",
                "isin": "ISIN Code",
                "quantity": "Units",
                "cost": "Purchase Cost",
                "market_value": "Current Value",
                "weight": "Allocation %",
            },
            multi_page=True,
        ),
        transactions_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Trade Ledger",
            header_keywords=["Trade Date", "Activity", "Instrument", "Units", "Consideration"],
            column_mapping={
                "date": "Trade Date",
                "type": "Activity",
                "security": "Instrument",
                "quantity": "Units",
                "amount": "Consideration (INR)",
            },
            multi_page=True,
        ),
    )


# ---------------------------------------------------------------------------
# Format C spec builder (AIF)
# ---------------------------------------------------------------------------
#
# Format C PDF layout (from format_c.py docstring):
#   Title: "Alternative Investment Fund — Investor Statement"
#   Meta block (4-col table): "Investor ID:" | val | "Commitment Period:" | val
#                              "Base Currency:" | val | "Fund Category:" | val
#   Section: "Fund Overview"  (2-col vertical: label | value)
#     Units Held | NAV Per Unit (INR) | Total Investment Value (INR)
#   Section: "Cash Ledger"  (3-col: Date | Activity | Amount (INR))
#   Section: "Fund Performance Summary"  (2-col vertical)
#     Opening Investment Value | Closing Investment Value | Total Contributions |
#     Management Fees Charged | Opening Cash Balance | Closing Cash Balance
#   No Holdings section (AIF investors hold units, not individual securities)


def _build_format_c_spec(format_id: str = "synthetic-format-c") -> ParsingSpec:
    """Return a ParsingSpec for the Format-C AIF layout."""
    return ParsingSpec(
        format_id=format_id,
        version="1.0.0",
        statement_type=StatementType.AIF,
        description="Format C: AIF Investor Statement with Fund Overview and Cash Ledger",
        metadata_fields={
            # Format C: provider_name and strategy_name appear as separate subtitle
            # paragraphs on lines 2 and 3 after the AIF title.
            "provider_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                # The line immediately after the AIF title
                pattern=r"Alternative Investment Fund[^\n]*\n([^\n]+)",
            ),
            "strategy_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                # The line immediately after the provider_name line
                pattern=r"Alternative Investment Fund[^\n]*\n[^\n]+\n([^\n]+)",
            ),
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Investor ID:",
            ),
            "statement_period": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Commitment Period:",
            ),
            "currency": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Base Currency:",
            ),
        },
        summary_fields={
            # Fund Overview section (2-col vertical)
            "units": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Units Held",
                offset_x=500.0,
            ),
            "nav_per_unit": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="NAV Per Unit",
                offset_x=500.0,
            ),
            "closing_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Total Investment Value",
                offset_x=500.0,
            ),
            # Fund Performance Summary section (2-col vertical)
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Investment Value",
                offset_x=500.0,
            ),
            "fees_charged": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Management Fees Charged",
                offset_x=500.0,
            ),
            "opening_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Cash Balance",
                offset_x=500.0,
            ),
            "closing_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Cash Balance",
                offset_x=500.0,
            ),
            "income": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Income",
                default=Decimal("0.00"),
            ),
            "realized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Realised Gains",
                default=Decimal("0.00"),
            ),
            "unrealized_gains": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Unrealised Gains",
                default=Decimal("0.00"),
            ),
        },
        # No holdings_table: AIF investors hold fund units, not individual securities
        holdings_table=None,
        transactions_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Cash Ledger",
            header_keywords=["Date", "Activity", "Amount"],
            column_mapping={
                "date": "Date",
                "type": "Activity",
                "amount": "Amount (INR)",
            },
            multi_page=True,
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_synthetic_spec_store(
    spec_store: SpecStore,
    samples_dir: Path | str | None = None,
) -> SpecStore:
    """Build and register parsing specifications for all synthetic PDF formats.

    Reads each synthetic PDF, extracts its fingerprint via
    :func:`src.fingerprint.extractor.extract_fingerprint`, attaches that
    fingerprint to the appropriate :class:`~src.specs.models.ParsingSpec`,
    and saves the spec into ``spec_store``.

    The caller supplies the ``spec_store`` so that specs can be kept in a
    temporary directory during tests, completely separate from the production
    ``specs/`` directory.

    Args:
        spec_store: Destination store.  Must be writable.  Existing specs
                    with the same ``format_id`` are overwritten so the
                    function is safe to call multiple times in the same
                    test session.
        samples_dir: Directory containing synthetic PDFs.  Defaults to
                     ``<project_root>/samples/synthetic``.

    Returns:
        The same ``spec_store`` instance after registration (for chaining).
    """
    sdir = Path(samples_dir) if samples_dir else _locate_samples_dir()

    # ------------------------------------------------------------------
    # Build the base spec objects (without fingerprints yet)
    # ------------------------------------------------------------------
    format_a_spec = _build_format_a_spec(
        format_id="synthetic-format-a",
        description="Format A: normal Portfolio Management Statement",
    )
    format_a_messy_spec = _build_format_a_spec(
        format_id="synthetic-format-a-messy",
        description="Format A messy: multi-page holdings, same anchors as Format A",
    )
    format_a_drift_spec = _build_format_a_drift_spec(
        format_id="synthetic-format-a-drift",
    )
    format_b_spec = _build_format_b_spec(
        format_id="synthetic-format-b",
    )
    format_c_spec = _build_format_c_spec(
        format_id="synthetic-format-c",
    )

    # ------------------------------------------------------------------
    # Map each PDF label to (spec, password).
    # format_a1, format_a2 share format_a_spec.
    # format_a1_encrypted also shares format_a_spec (same layout).
    # ------------------------------------------------------------------
    pdf_to_spec: list[tuple[str, ParsingSpec, str | None]] = [
        ("format_a1",           format_a_spec,       None),
        ("format_a2",           format_a_spec,       None),
        ("format_a1_encrypted", format_a_spec,       ENCRYPTED_SAMPLE_PASSWORD),
        ("format_a_messy",      format_a_messy_spec, None),
        ("format_a_drift",      format_a_drift_spec, None),
        ("format_b",            format_b_spec,       None),
        ("format_c",            format_c_spec,       None),
    ]

    # ------------------------------------------------------------------
    # Extract fingerprints and collect one fingerprint per format_id.
    # When multiple PDFs share a spec (format_a1, format_a2, encrypted)
    # we use the first successfully extracted fingerprint.
    # ------------------------------------------------------------------
    fingerprints_by_format: dict[str, object] = {}

    for pdf_label, spec, password in pdf_to_spec:
        fid = spec.format_id
        if fid in fingerprints_by_format:
            # Already have a fingerprint for this format_id — skip
            continue

        pdf_path = sdir / f"{pdf_label}.pdf"
        if not pdf_path.is_file():
            # PDF not present — skip gracefully (CI may not have samples)
            continue

        try:
            doc_ir = extract_pdf(pdf_path, password=password)
        except Exception:
            # Extraction failure — skip this PDF, try next
            continue

        fp = extract_fingerprint(doc_ir, format_id=fid)
        fingerprints_by_format[fid] = fp

    # ------------------------------------------------------------------
    # Attach fingerprints and save specs
    # ------------------------------------------------------------------
    seen_format_ids: set[str] = set()
    for _, spec, _ in pdf_to_spec:
        fid = spec.format_id
        if fid in seen_format_ids:
            continue
        seen_format_ids.add(fid)

        fp = fingerprints_by_format.get(fid)
        if fp is not None:
            spec = spec.model_copy(update={"fingerprint": fp})

        spec_store.save(spec, overwrite=True)

    return spec_store
