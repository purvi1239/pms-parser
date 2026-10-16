"""Command-line interface for the PMS & AIF Investment Statement Parser.

Coordinates single-file and folder-based document processing using the existing
Pipeline and FolderIngester subsystems.
"""
from __future__ import annotations
from dotenv import load_dotenv

import argparse
import contextlib
from pathlib import Path
import sys
from typing import TextIO

from src.ingestion.exceptions import (
    IMAPAuthError,
    IMAPConnectionError,
    IMAPFetchError,
)
from src.ingestion.folder import FolderIngester, PipelineInterface
from src.ingestion.gmail import GmailIngester
from src.ingestion.models import FolderIngestionConfig, GmailIngestionConfig
from src.learner.learner import Learner
from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineConfig
from src.pipeline.pipeline import Pipeline
from src.specs import (
    DuplicateSpecError,
    SpecNotFoundError,
    SpecStore,
    SpecValidationError,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
def create_parser() -> argparse.ArgumentParser:
    """Create and configure the command-line argument parser.

    Returns:
        Configured ArgumentParser with descriptive help and usage examples.
    """
    parser = argparse.ArgumentParser(
        prog="pms-parser",
        description="PMS & AIF Investment Statement Parser CLI",
        epilog=(
            "Examples:\n"
            "  python -m src.cli statement.pdf\n"
            "  python -m src.cli statement.pdf --learn\n"
            "  python -m src.cli statement.pdf --password secret\n"
            "  python -m src.cli --folder ./statements/\n"
            "  python -m src.cli --folder ./statements/ --recursive --verbose\n"
            "  python -m src.cli list-drafts\n"
            "  python -m src.cli approve draft_format_a\n"
            "  python -m src.cli approve draft_format_a --overwrite\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to a single PDF statement file or directory to process.",
    )
    parser.add_argument(
        "-f",
        "--folder",
        metavar="DIR",
        default=None,
        help="Path to a directory containing statement PDFs to ingest in batch.",
    )
    parser.add_argument(
        "-p",
        "--password",
        metavar="PASSWORD",
        default=None,
        help="Candidate password for encrypted PDF files (never displayed).",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        default=False,
        help="Recursively scan subdirectories for PDF files (folder mode only).",
    )
    parser.add_argument(
        "-l",
        "--learn",
        action="store_true",
        default=False,
        help="Enable one-time LLM learning for unknown statement formats.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed diagnostic output and per-document status information.",
    )
    parser.add_argument(
        "--list-drafts",
        action="store_true",
        default=False,
        help="List all saved draft parsing specifications pending review.",
    )
    parser.add_argument(
        "--approve",
        metavar="DRAFT_ID",
        default=None,
        help="Approve a saved draft parsing specification by its format ID and activate it.",
    )
    parser.add_argument(
        "-g",
        "--gmail",
        nargs="?",
        const="INBOX",
        default=None,
        metavar="MAILBOX",
        help="Ingest statement attachments from Gmail via IMAP (default mailbox: INBOX).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        default=None,
        help="Maximum number of messages to fetch from mailbox (Gmail mode only).",
    )
    parser.add_argument(
        "--search",
        metavar="CRITERIA",
        default=None,
        help="IMAP search criteria for filtering messages, e.g. 'UNSEEN' (Gmail mode only, default: 'ALL').",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Allow overwriting an existing active specification during approval.",
    )
    parser.add_argument(
        "--verify-ollama",
        action="store_true",
        default=False,
        help="Verify live local Ollama (llama3.2:3b) connection using synthetic input.",
    )
    parser.add_argument(
        "--verify-gmail",
        action="store_true",
        default=False,
        help="Opt-in to verify live Gmail IMAP connection and authentication in read-only mode.",
    )
    return parser


# ---------------------------------------------------------------------------
# Rendering helpers  (pure functions — no side effects)
# ---------------------------------------------------------------------------

_STATUS_EMOJI: dict[DocumentStatus, str] = {
    DocumentStatus.PARSED:              "✅",
    DocumentStatus.PARSED_WITH_WARNINGS:"⚠️ ",
    DocumentStatus.NEEDS_REVIEW:        "🔍",
    DocumentStatus.DRIFT_SUSPECTED:     "🔀",
    DocumentStatus.DUPLICATE:           "♻️ ",
    DocumentStatus.SKIPPED:             "⏭️ ",
    DocumentStatus.NEEDS_PASSWORD:      "🔐",
    DocumentStatus.FAILED:              "❌",
    DocumentStatus.LEARNING_FAILED:     "🧠❌",
    DocumentStatus.MAYBE:              "❓",
}

_SEP_W = 64   # column-width for separator lines


def _sep(title: str = "") -> str:
    bar = "─" * _SEP_W
    if not title:
        return bar
    return f"\n{bar}\n  {title}\n{bar}"


def _fmt_inr(val) -> str:
    """Right-aligned INR amount or '—' if absent."""
    if val is None:
        return "—"
    try:
        return f"₹{float(val):>15,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_qty(val) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):,.4f}"
    except (TypeError, ValueError):
        return str(val)


def _meta(lines: list[str], label: str, value) -> None:
    lines.append(f"    {label:<20}: {value or '—'}")


def _sum_line(lines: list[str], label: str, value) -> None:
    lines.append(f"    {label:<28}: {_fmt_inr(value)}")


def _msgs(lines: list[str], heading: str, items: list[str]) -> None:
    if not items:
        return
    lines.append(_sep(f"{heading}  ({len(items)})"))
    for m in items:
        lines.append(f"  •  {m}")


# ---------------------------------------------------------------------------
# Core result renderer
# ---------------------------------------------------------------------------

def _render_single_result(res, path_obj: Path, verbose: bool) -> list[str]:
    """Convert a PipelineResult into human-readable display lines.

    Uses only data actually present on the result model — never invents values.
    """
    lines: list[str] = []
    emoji = _STATUS_EMOJI.get(res.status, " ")

    # ── Header ────────────────────────────────────────────────────────────
    lines.append("=" * _SEP_W)
    lines.append(f"  {emoji}  File: {path_obj.name}")
    lines.append("=" * _SEP_W)
    lines.append(f"  Status: {res.status.value}")
    if res.statement_type:
        lines.append(f"  Statement Type: {res.statement_type.value}")
    if res.format_id:
        lines.append(f"  Format ID: {res.format_id}")
    if res.learning_result:
        lines.append(f"  Learned Format: {res.learning_result.format_id} ({res.learning_result.status.value})")
    lines.append(f"  Warnings: {len(res.warnings)}")
    lines.append(f"  Errors: {len(res.errors)}")

    if verbose:
        lines.append(f"  Document ID: {res.document_id}")
        if res.file_hash:
            lines.append(f"  File Hash: {res.file_hash[:16]}…")
        if res.classification:
            lines.append(
                f"  Classification: {res.classification.decision.value}"
            )
        if res.fingerprint_match:
            sim = getattr(res.fingerprint_match, "similarity", None)
            if sim is not None:
                lines.append(f"  FP Similarity: {sim:.2%}")
        if res.is_quarantined:
            lines.append("  Quarantined: Yes — manual review required")
        if res.is_duplicate:
            lines.append("  Duplicate: Yes — content already ingested")

    # ── Unknown-format short-circuit ──────────────────────────────────────
    if res.status == DocumentStatus.NEEDS_REVIEW and not res.format_id:
        lines.append("")
        lines.append("  ⚠  No matching specification was found in the SpecStore.")
        lines.append("     This document's layout is unrecognised — it could not")
        lines.append("     be automatically parsed and no data has been extracted.")
        lines.append("")
        lines.append("  Possible next steps:")
        lines.append("    1. Register a ParsingSpec that describes this PDF layout.")
        lines.append("    2. Re-run with --learn to enable one-time specification learning.")
        _msgs(lines, "Warnings", res.warnings)
        _msgs(lines, "Errors", res.errors)
        lines.append("=" * _SEP_W)
        return lines

    # ── Extracted metadata ────────────────────────────────────────────────
    stmt = res.statement
    if stmt:
        lines.append(_sep("Extracted Metadata"))
        _meta(lines, "Provider",    stmt.provider_name)
        _meta(lines, "Strategy",    stmt.strategy_name)
        _meta(lines, "Client ID",   stmt.client_identifier)
        _meta(lines, "Period",      stmt.statement_period)
        _meta(lines, "Currency",    stmt.currency)

    # ── Portfolio summary ─────────────────────────────────────────────────
    summary = (
        (stmt.summary if stmt else None)
        or (res.parse_result.summary if res.parse_result else None)
    )
    if summary:
        lines.append(_sep("Portfolio Summary"))
        _sum_line(lines, "Opening Portfolio Value ", summary.opening_value)
        _sum_line(lines, "Closing Portfolio Value ", summary.closing_value)
        _sum_line(lines, "Opening Cash Balance    ", summary.opening_cash)
        _sum_line(lines, "Closing Cash Balance    ", summary.closing_cash)
        _sum_line(lines, "Fees Charged            ", summary.fees_charged)
        _sum_line(lines, "Income                  ", summary.income)
        _sum_line(lines, "Realised Gains          ", summary.realized_gains)
        _sum_line(lines, "Unrealised Gains        ", summary.unrealized_gains)
        if summary.units is not None:
            lines.append(f"    {'Units Held':<28}: {_fmt_qty(summary.units)}")
        if summary.nav_per_unit is not None:
            lines.append(f"    {'NAV Per Unit':<28}: {_fmt_inr(summary.nav_per_unit)}")

    # ── Holdings ──────────────────────────────────────────────────────────
    holdings = (
        (stmt.holdings if stmt else None)
        or (res.parse_result.holdings if res.parse_result else [])
    ) or []
    if holdings:
        count = len(holdings)
        lines.append(_sep(f"Holdings  ({count} position{'s' if count != 1 else ''})"))
        hdr = (
            f"  {'#':>3}  {'Security':<34} {'ISIN':<13}"
            f"  {'Qty':>10}  {'Cost':>14}  {'Mkt Value':>14}  {'Wt%':>6}"
        )
        lines.append(hdr)
        lines.append("  " + "·" * max(0, len(hdr) - 2))
        for idx, h in enumerate(holdings, 1):
            name = (h.security_name or "—")[:33]
            isin = (h.isin or "—")[:12]
            qty  = _fmt_qty(h.quantity)
            cost = _fmt_inr(h.cost).strip()
            mval = _fmt_inr(h.market_value).strip()
            wt   = f"{float(h.weight):.2f}%" if h.weight is not None else "—"
            lines.append(
                f"  {idx:>3}.  {name:<34} {isin:<13}"
                f"  {qty:>10}  {cost:>14}  {mval:>14}  {wt:>6}"
            )

    # ── Transactions ──────────────────────────────────────────────────────
    transactions = (
        (stmt.transactions if stmt else None)
        or (res.parse_result.transactions if res.parse_result else [])
    ) or []
    if transactions:
        count = len(transactions)
        lines.append(_sep(f"Transactions  ({count} row{'s' if count != 1 else ''})"))
        hdr = (
            f"  {'#':>3}  {'Date':<12}  {'Type':<10}"
            f"  {'Security':<28}  {'Qty':>10}  {'Amount (INR)':>17}"
        )
        lines.append(hdr)
        lines.append("  " + "·" * max(0, len(hdr) - 2))
        for idx, t in enumerate(transactions, 1):
            date_s = str(t.date) if t.date else "—"
            ttype  = t.type.value if t.type else "—"
            sec    = (t.security or "—")[:27]
            qty    = _fmt_qty(t.quantity)
            amt    = _fmt_inr(t.amount).strip()
            lines.append(
                f"  {idx:>3}.  {date_s:<12}  {ttype:<10}"
                f"  {sec:<28}  {qty:>10}  {amt:>17}"
            )

    # ── Validation findings ───────────────────────────────────────────────
    vr = res.validation_result
    if vr and vr.findings:
        err_f  = vr.errors
        warn_f = vr.warnings
        if err_f:
            lines.append(_sep(f"Validation Errors  ({len(err_f)})"))
            for f in err_f:
                lines.append(f"  ✗  [{f.check}] {f.message}")
                if verbose:
                    if f.actual:
                        lines.append(f"       actual   : {f.actual}")
                    if f.expected:
                        lines.append(f"       expected : {f.expected}")
        if warn_f:
            lines.append(_sep(f"Validation Warnings  ({len(warn_f)})"))
            for f in warn_f:
                lines.append(f"  ⚑  [{f.check}] {f.message}")
    elif vr and not vr.findings:
        lines.append("")
        lines.append("  ✓  All validation checks passed.")

    # ── Pipeline-level messages ───────────────────────────────────────────
    _msgs(lines, "Pipeline Warnings", res.warnings)
    _msgs(lines, "Pipeline Errors",   res.errors)

    lines.append("=" * _SEP_W)
    return lines


# ---------------------------------------------------------------------------
# Public processing functions  (signatures unchanged)
# ---------------------------------------------------------------------------

def process_single_file(
    file_path: Path | str,
    pipeline: Pipeline | PipelineInterface | None = None,
    password: str | None = None,
    verbose: bool = False,
    learn: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Process an individual statement PDF through the Pipeline.

    Args:
        file_path: Path to the target PDF statement.
        pipeline: Optional Pipeline or PipelineInterface instance.
        password: Optional explicit password for encrypted documents.
        verbose: Whether to display extended diagnostics.
        learn: Whether to enable one-time learning for unknown formats.
        out: Output stream for normal results.
        err: Error stream for failures and diagnostics.

    Returns:
        Exit code 0 on successful parse or known state; non-zero on failure.
    """
    path_obj = Path(file_path)
    if pipeline is not None:
        pipe = pipeline
        if learn and isinstance(pipe, Pipeline):
            pipe.config.enable_learning = True
            if pipe.learner is None:
                pipe.learner = Learner(
                    spec_store=pipe.spec_store,
                    engine=pipe.engine,
                    validator=pipe.validator,
                )
    else:
        config = PipelineConfig(
            enable_learning=learn,
            auto_activate_learned_spec=False,
        )
        pipe = Pipeline(
            config=config,
            learner=Learner() if learn else None,
        )

    try:
        res = pipe.process_file(path_obj, password=password)
    except Exception as exc:
        print(f"Error: Pipeline execution failed on {path_obj.name}: {exc}", file=err)
        if verbose:
            import traceback
            traceback.print_exc(file=err)
        return 1

    lines = _render_single_result(res, path_obj, verbose)
    print("\n".join(lines), file=out)

    if res.status in (
        DocumentStatus.PARSED,
        DocumentStatus.PARSED_WITH_WARNINGS,
        DocumentStatus.DUPLICATE,
        DocumentStatus.SKIPPED,
    ):
        return 0
    return 1


def process_folder(
    directory: Path | str,
    pipeline: Pipeline | PipelineInterface | None = None,
    ingester: FolderIngester | None = None,
    recursive: bool = False,
    password: str | None = None,
    verbose: bool = False,
    learn: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Process a directory of statement PDFs through FolderIngester.

    Args:
        directory: Path to the target directory.
        pipeline: Optional Pipeline or PipelineInterface instance.
        ingester: Optional FolderIngester instance.
        recursive: Whether to discover PDFs recursively in subdirectories.
        password: Optional explicit password for encrypted documents.
        verbose: Whether to display per-file status details.
        learn: Whether to enable one-time learning for unknown formats.
        out: Output stream for summary results.
        err: Error stream for failures and diagnostics.

    Returns:
        Exit code 0 when all documents succeed; non-zero if one or more fail.
    """
    dir_obj = Path(directory)
    if pipeline is not None:
        pipe = pipeline
        if learn and isinstance(pipe, Pipeline):
            pipe.config.enable_learning = True
            if pipe.learner is None:
                pipe.learner = Learner(
                    spec_store=pipe.spec_store,
                    engine=pipe.engine,
                    validator=pipe.validator,
                )
    else:
        config = PipelineConfig(
            enable_learning=learn,
            auto_activate_learned_spec=False,
        )
        pipe = Pipeline(
            config=config,
            learner=Learner() if learn else None,
        )
    folder_ingester = ingester or FolderIngester(
        pipeline=pipe,
        config=FolderIngestionConfig(recursive=recursive),
    )

    try:
        res = folder_ingester.ingest(
            directory=dir_obj,
            recursive=recursive,
            password=password,
        )
    except Exception as exc:
        print(f"Error: Folder ingestion failed on {dir_obj}: {exc}", file=err)
        if verbose:
            import traceback
            traceback.print_exc(file=err)
        return 1

    lines = [
        f"Directory: {res.directory}",
        f"Discovered: {res.total_discovered}",
        f"Processed: {res.processed}",
        f"Successful: {res.successful}",
        f"Duplicates: {res.duplicates}",
        f"Skipped: {res.skipped}",
        f"Quarantined: {res.quarantined}",
        f"Failed: {res.failed}",
    ]

    if res.errors:
        lines.append(f"Batch Errors: {len(res.errors)}")
        for e in res.errors:
            lines.append(f"  - {e}")

    if res.failed > 0:
        failed_items = [
            r
            for r in res.results
            if r.status in (DocumentStatus.FAILED, DocumentStatus.NEEDS_PASSWORD)
        ]
        if failed_items:
            lines.append(f"\nFailed Documents ({len(failed_items)}):")
            for r in failed_items:
                reason = f": {r.errors[0]}" if r.errors else ""
                lines.append(f"  - {Path(r.file_path).name} [{r.status.value}]{reason}")

    if verbose and res.results:
        lines.append(f"\nProcessed Documents ({len(res.results)}):")
        for r in res.results:
            lines.append(f"  - {Path(r.file_path).name}: {r.status.value}")

    print("\n".join(lines), file=out)

    if res.failed > 0:
        return 1
    return 0


def process_gmail(
    mailbox: str = "INBOX",
    pipeline: Pipeline | PipelineInterface | None = None,
    gmail_ingester: GmailIngester | None = None,
    search_criteria: str | None = None,
    limit: int | None = None,
    password: str | None = None,
    verbose: bool = False,
    learn: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Fetch and process statement attachments from a Gmail mailbox via IMAP.

    Args:
        mailbox: Target mailbox/folder name (defaults to 'INBOX').
        pipeline: Optional Pipeline instance.
        gmail_ingester: Optional GmailIngester instance (used for dependency injection/testing).
        search_criteria: IMAP search criteria (e.g. 'ALL', 'UNSEEN').
        limit: Optional maximum number of messages to fetch.
        password: Candidate password for encrypted PDF statements.
        verbose: Whether to display detailed per-document output.
        learn: Whether to enable LLM learning for unknown document formats.
        out: Output stream for results.
        err: Output stream for errors.

    Returns:
        0 on success, 1 on processing/connection/auth error.
    """
    if pipeline is not None:
        pipe = pipeline
        if learn and isinstance(pipe, Pipeline):
            pipe.config.enable_learning = True
            if pipe.learner is None:
                pipe.learner = Learner(
                    spec_store=pipe.spec_store,
                    engine=pipe.engine,
                    validator=pipe.validator,
                )
    else:
        config = PipelineConfig(
            enable_learning=learn,
            auto_activate_learned_spec=False,
        )
        pipe = Pipeline(
            config=config,
            learner=Learner() if learn else None,
        )

    if gmail_ingester is not None:
        ingester = gmail_ingester
    else:
        cfg = GmailIngestionConfig.from_env(
            mailbox=mailbox,
            search_criteria=search_criteria or "ALL",
            limit=limit,
            passwords=[password] if password else [],
        )
        if not cfg.username or not cfg.password:
            print(
                "Error: Gmail ingestion requires GMAIL_ADDRESS and GMAIL_APP_PASSWORD environment variables.",
                file=err,
            )
            return 1
        ingester = GmailIngester(pipeline=pipe, config=cfg)

    try:
        res = ingester.ingest(
            mailbox=mailbox,
            search_criteria=search_criteria,
            limit=limit,
            password=password,
        )
    except (IMAPAuthError, IMAPConnectionError, IMAPFetchError) as exc:
        print(f"Error: Gmail IMAP ingestion failed: {exc}", file=err)
        if verbose:
            import traceback
            traceback.print_exc(file=err)
        return 1
    except Exception as exc:
        print(f"Error: Unexpected failure during Gmail ingestion: {exc}", file=err)
        if verbose:
            import traceback
            traceback.print_exc(file=err)
        return 1

    lines = [
        f"Mailbox: {mailbox}",
        f"Emails Processed: {res.total_emails}",
        f"Attachments Inspected: {res.total_attachments}",
        f"Eligible Discovered: {res.total_discovered}",
        f"Processed: {res.processed}",
        f"Successful: {res.successful}",
        f"Duplicates: {res.duplicates}",
        f"Skipped: {res.skipped}",
        f"Quarantined: {res.quarantined}",
        f"Failed: {res.failed}",
    ]

    if res.errors:
        lines.append(f"Batch Errors: {len(res.errors)}")
        for e in res.errors:
            lines.append(f"  - {e}")

    if res.failed > 0:
        failed_items = [
            r
            for r in res.results
            if r.status in (DocumentStatus.FAILED, DocumentStatus.NEEDS_PASSWORD)
        ]
        if failed_items:
            lines.append(f"\nFailed Documents ({len(failed_items)}):")
            for r in failed_items:
                reason = f": {r.errors[0]}" if r.errors else ""
                lines.append(f"  - {Path(r.file_path).name} [{r.status.value}]{reason}")

    if verbose and res.results:
        lines.append(f"\nProcessed Documents ({len(res.results)}):")
        for r in res.results:
            lines.append(f"  - {Path(r.file_path).name}: {r.status.value}")

    print("\n".join(lines), file=out)

    if res.failed > 0:
        return 1
    return 0


def list_draft_specs(
    store: SpecStore | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """List all saved draft specifications pending review.

    Args:
        store: Optional SpecStore instance.
        out: Output stream for display.
        err: Error stream for failures.

    Returns:
        Exit code 0 on success.
    """
    spec_store = store or SpecStore()
    drafts = spec_store.list_drafts(ignore_invalid=True)
    if not drafts:
        print("No saved draft specifications found.", file=out)
        return 0

    lines: list[str] = [
        f"Saved Draft Proposals ({len(drafts)}):",
    ]
    for d in drafts:
        stype = d.statement_type.value if hasattr(d.statement_type, "value") else str(d.statement_type)
        status = d.metadata.get("status", "proposed")
        desc = f" - {d.description}" if d.description else ""
        lines.append(f"  • {d.format_id} [{stype}] (status: {status}){desc}")

    print("\n".join(lines), file=out)
    return 0


def approve_draft_spec(
    draft_id: str,
    overwrite: bool = False,
    store: SpecStore | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Approve a saved draft specification and activate it.

    Args:
        draft_id: Format ID of the draft specification.
        overwrite: Whether to overwrite an existing active specification.
        store: Optional SpecStore instance.
        out: Output stream for success messages.
        err: Error stream for error messages.

    Returns:
        Exit code 0 on success, non-zero on failure.
    """
    spec_store = store or SpecStore()
    try:
        approved_spec = spec_store.approve_draft(draft_id, overwrite=overwrite)
    except SpecNotFoundError:
        print(f"Error: Draft specification '{draft_id}' not found.", file=err)
        return 1
    except DuplicateSpecError as exc:
        print(f"Error: {exc}", file=err)
        print("Use --overwrite to replace the existing active specification.", file=err)
        return 1
    except (SpecValidationError, ValueError) as exc:
        print(f"Error: Failed to approve draft '{draft_id}': {exc}", file=err)
        return 1
    except Exception as exc:
        print(f"Error: Unexpected failure approving draft '{draft_id}': {exc}", file=err)
        return 1

    print(
        f"Successfully approved draft '{draft_id}' -> active specification '{approved_spec.format_id}'.",
        file=out,
    )
    return 0


def main(
    argv: list[str] | None = None,
    pipeline: Pipeline | PipelineInterface | None = None,
    ingester: FolderIngester | None = None,
    gmail_ingester: GmailIngester | None = None,
    spec_store: SpecStore | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Main CLI entry point for the PMS statement parser.

    Args:
        argv: Optional argument list (defaults to sys.argv[1:]).
        pipeline: Optional injectable Pipeline instance for testing.
        ingester: Optional injectable FolderIngester instance for testing.
        gmail_ingester: Optional injectable GmailIngester instance for testing.
        spec_store: Optional injectable SpecStore instance for testing.
        out: Optional stdout stream override.
        err: Optional stderr stream override.

    Returns:
        Exit code: 0 on success, 1 on processing/not found error, 2 on usage error.
    """
    out_stream = out or sys.stdout
    err_stream = err or sys.stderr

    parser = create_parser()

    if argv is None:
        argv = sys.argv[1:]

    # Normalize command / subcommand syntax into argument flags
    normalized_argv: list[str] = []
    for idx, arg in enumerate(argv):
        if idx == 0 and arg in ("list-drafts", "list_drafts", "drafts"):
            normalized_argv.append("--list-drafts")
        elif idx == 0 and arg == "approve":
            normalized_argv.append("--approve")
        else:
            normalized_argv.append(arg)

    try:
        with contextlib.redirect_stdout(out_stream), contextlib.redirect_stderr(err_stream):
            args = parser.parse_args(normalized_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0

    # Resolve SpecStore instance
    resolved_store = spec_store
    if resolved_store is None and isinstance(pipeline, Pipeline):
        resolved_store = pipeline.spec_store
    if resolved_store is None:
        resolved_store = SpecStore()

    # Mode: Stage 3 Integration Verification
    if getattr(args, "verify_ollama", False) or getattr(args, "verify_gmail", False):
        from verify_integrations import run_verification
        return run_verification(
            verify_ollama_flag=getattr(args, "verify_ollama", False),
            verify_gmail_flag=getattr(args, "verify_gmail", False),
            verbose=args.verbose,
            out=out_stream,
            err=err_stream,
        )

    # Mode: List drafts
    if getattr(args, "list_drafts", False):
        if args.path or args.folder or getattr(args, "approve", None) or getattr(args, "gmail", None):
            print("Error: Cannot combine --list-drafts with document paths or other operations.", file=err_stream)
            return 2
        return list_draft_specs(store=resolved_store, out=out_stream, err=err_stream)

    # Mode: Approve draft
    if getattr(args, "approve", None):
        if args.path or args.folder or getattr(args, "gmail", None):
            print("Error: Cannot specify a document path, --folder, or --gmail when approving a draft.", file=err_stream)
            return 2
        return approve_draft_spec(
            draft_id=args.approve,
            overwrite=getattr(args, "overwrite", False),
            store=resolved_store,
            out=out_stream,
            err=err_stream,
        )

    # Mode: Gmail IMAP ingestion
    gmail_target = getattr(args, "gmail", None)
    if gmail_target is not None:
        if args.path or args.folder:
            print("Error: Cannot specify both --gmail and a document path or --folder.", file=err_stream)
            return 2

        return process_gmail(
            mailbox=gmail_target,
            pipeline=pipeline,
            gmail_ingester=gmail_ingester,
            search_criteria=getattr(args, "search", None),
            limit=getattr(args, "limit", None),
            password=args.password,
            verbose=args.verbose,
            learn=getattr(args, "learn", False),
            out=out_stream,
            err=err_stream,
        )

    # Validation: Cannot specify both positional path and --folder
    if args.path and args.folder:
        print("Error: Cannot specify both a positional path and --folder.", file=err_stream)
        return 2

    # Validation: Must specify at least one target
    if not args.path and not args.folder:
        print("Error: Please provide a PDF file path, --folder directory path, or --gmail option.", file=err_stream)
        print("Use --help for usage instructions.", file=err_stream)
        return 2

    # Mode 1: Explicit --folder option
    if args.folder:
        folder_path = Path(args.folder)
        if not folder_path.exists():
            print(f"Error: Directory does not exist: {folder_path}", file=err_stream)
            return 1
        if not folder_path.is_dir():
            print(f"Error: Path is not a directory: {folder_path}", file=err_stream)
            return 1
        return process_folder(
            directory=folder_path,
            pipeline=pipeline,
            ingester=ingester,
            recursive=args.recursive,
            password=args.password,
            verbose=args.verbose,
            learn=getattr(args, "learn", False),
            out=out_stream,
            err=err_stream,
        )

    # Mode 2: Positional path (file or auto-detected directory)
    target_path = Path(args.path)
    if not target_path.exists():
        print(f"Error: Path does not exist: {target_path}", file=err_stream)
        return 1

    if target_path.is_dir():
        return process_folder(
            directory=target_path,
            pipeline=pipeline,
            ingester=ingester,
            recursive=args.recursive,
            password=args.password,
            verbose=args.verbose,
            learn=getattr(args, "learn", False),
            out=out_stream,
            err=err_stream,
        )

    # Must be a regular file with .pdf extension
    if target_path.suffix.lower() != ".pdf":
        print(
            f"Error: Unsupported file type: {target_path.name}. Only PDF files are supported.",
            file=err_stream,
        )
        return 1

    return process_single_file(
        file_path=target_path,
        pipeline=pipeline,
        password=args.password,
        verbose=args.verbose,
        learn=getattr(args, "learn", False),
        out=out_stream,
        err=err_stream,
    )


if __name__ == "__main__":
    sys.exit(main())
