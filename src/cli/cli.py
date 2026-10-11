"""Command-line interface for the PMS & AIF Investment Statement Parser.

Coordinates single-file and folder-based document processing using the existing
Pipeline and FolderIngester subsystems.
"""
from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path
import sys
from typing import TextIO

from src.ingestion.folder import FolderIngester, PipelineInterface
from src.ingestion.models import FolderIngestionConfig
from src.models.enums import DocumentStatus
from src.pipeline.pipeline import Pipeline


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
            "  python -m src.cli statement.pdf --password secret\n"
            "  python -m src.cli --folder ./statements/\n"
            "  python -m src.cli --folder ./statements/ --recursive --verbose\n"
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
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed diagnostic output and per-document status information.",
    )
    return parser


def process_single_file(
    file_path: Path | str,
    pipeline: Pipeline | PipelineInterface | None = None,
    password: str | None = None,
    verbose: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Process an individual statement PDF through the Pipeline.

    Args:
        file_path: Path to the target PDF statement.
        pipeline: Optional Pipeline or PipelineInterface instance.
        password: Optional explicit password for encrypted documents.
        verbose: Whether to display extended diagnostics.
        out: Output stream for normal results.
        err: Error stream for failures and diagnostics.

    Returns:
        Exit code 0 on successful parse or known state; non-zero on failure.
    """
    path_obj = Path(file_path)
    pipe = pipeline or Pipeline()

    try:
        res = pipe.process_file(path_obj, password=password)
    except Exception as exc:
        print(f"Error: Pipeline execution failed on {path_obj.name}: {exc}", file=err)
        if verbose:
            import traceback

            traceback.print_exc(file=err)
        return 1

    lines = [
        f"File: {path_obj.name}",
        f"Status: {res.status.value}",
    ]
    if res.statement_type:
        lines.append(f"Statement Type: {res.statement_type.value}")
    if res.format_id:
        lines.append(f"Format ID: {res.format_id}")

    lines.append(f"Warnings: {len(res.warnings)}")
    for w in res.warnings:
        lines.append(f"  - Warning: {w}")

    lines.append(f"Errors: {len(res.errors)}")
    for e in res.errors:
        lines.append(f"  - Error: {e}")

    if verbose:
        lines.append(f"Document ID: {res.document_id}")
        if res.file_hash:
            lines.append(f"File Hash: {res.file_hash[:16]}...")
        if res.classification:
            lines.append(f"Classification: {res.classification.decision.value}")
        if res.is_duplicate:
            lines.append("Is Duplicate: True")
        if res.is_quarantined:
            lines.append("Is Quarantined: True")

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
        out: Output stream for summary results.
        err: Error stream for failures and diagnostics.

    Returns:
        Exit code 0 when all documents succeed; non-zero if one or more fail.
    """
    dir_obj = Path(directory)
    pipe = pipeline or Pipeline()
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


def main(
    argv: list[str] | None = None,
    pipeline: Pipeline | PipelineInterface | None = None,
    ingester: FolderIngester | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Main CLI entry point for the PMS statement parser.

    Args:
        argv: Optional argument list (defaults to sys.argv[1:]).
        pipeline: Optional injectable Pipeline instance for testing.
        ingester: Optional injectable FolderIngester instance for testing.
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

    try:
        with contextlib.redirect_stdout(out_stream), contextlib.redirect_stderr(err_stream):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0

    # Validation: Cannot specify both positional path and --folder
    if args.path and args.folder:
        print("Error: Cannot specify both a positional path and --folder.", file=err_stream)
        return 2

    # Validation: Must specify at least one target
    if not args.path and not args.folder:
        print("Error: Please provide a PDF file path or --folder directory path.", file=err_stream)
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
        out=out_stream,
        err=err_stream,
    )


if __name__ == "__main__":
    sys.exit(main())
