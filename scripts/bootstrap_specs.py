#!/usr/bin/env python3
"""Bootstrap the production specs/ directory with approved parsing specifications.

Reads each synthetic PDF in samples/synthetic/, extracts its fingerprint,
attaches it to the corresponding ParsingSpec, marks it active (is_active=True,
status='active'), and saves it to the production specs/ directory.

Safe to re-run: existing specs are overwritten atomically.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/bootstrap_specs.py
    PYTHONPATH=. .venv/bin/python scripts/bootstrap_specs.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the project root is on sys.path when running directly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.eval.synthetic_specs import build_synthetic_spec_store
from src.specs.store import SpecStore, get_default_specs_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed specs/ with approved synthetic parsing specifications.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Build specs in-memory and report what would be written without touching disk.",
    )
    parser.add_argument(
        "--specs-dir",
        metavar="DIR",
        default=None,
        help="Target specs directory (default: <project_root>/specs).",
    )
    args = parser.parse_args(argv)

    specs_dir = Path(args.specs_dir).resolve() if args.specs_dir else get_default_specs_dir()

    if args.dry_run:
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="pms_specs_dryrun_")
        store = SpecStore(specs_dir=tmpdir)
        print(f"[dry-run] Using temporary directory: {tmpdir}")
    else:
        store = SpecStore(specs_dir=specs_dir)
        print(f"Writing specs to: {specs_dir}")

    print("Extracting fingerprints from synthetic PDFs and building specs…")
    try:
        build_synthetic_spec_store(store)
    except Exception as exc:
        print(f"ERROR: Failed to build synthetic specs: {exc}", file=sys.stderr)
        return 1

    # Format A drift must NOT be registered as an active spec.
    # Preserving this variant as unregistered ensures the pipeline correctly detects
    # layout drift (DRIFT_SUSPECTED) and applies quarantine behavior instead of treating it as known.
    store.delete("synthetic-format-a-drift")

    # Approved synthetic demo formats to activate
    approved_formats = {
        "synthetic-format-a",
        "synthetic-format-a-messy",
        "synthetic-format-b",
        "synthetic-format-c",
    }

    # Ensure approved demo specs are marked active and machine-specific absolute paths
    # are removed from spec metadata.
    saved = store.list_specs(ignore_invalid=True)
    for spec in saved:
        if spec.format_id not in approved_formats:
            store.delete(spec.format_id)
            continue

        updated = spec.model_copy(deep=True)
        updated.metadata["is_active"] = True
        updated.metadata["status"] = "active"

        # Avoid machine-specific absolute paths in committed spec metadata
        if updated.fingerprint and "source_path" in updated.fingerprint.metadata:
            src_val = str(updated.fingerprint.metadata["source_path"])
            try:
                rel_path = Path(src_val).relative_to(_PROJECT_ROOT)
                updated.fingerprint.metadata["source_path"] = str(rel_path)
            except Exception:
                updated.fingerprint.metadata["source_path"] = f"samples/synthetic/{Path(src_val).name}"

        store.save(updated, overwrite=True)

    active_specs = [store.load(fid) for fid in sorted(store.list_formats())]

    print()
    print(f"Saved {len(active_specs)} active approved spec(s) to '{specs_dir}':")
    for spec in active_specs:
        s = spec.format_id
        fp_info = "with fingerprint" if spec.fingerprint else "NO fingerprint (PDF not found)"
        active_flag = "active" if spec.metadata.get("is_active") is True else "inactive"
        print(f"  {s:40s}  [{active_flag}]  {fp_info}")

    if args.dry_run:
        print("\n[dry-run] No files were written to the production specs/ directory.")
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print(f"\nDone. {len(active_specs)} approved spec(s) are now active in {specs_dir}")
        print("Run the pipeline to verify:")
        print("  PYTHONPATH=. .venv/bin/python -m src.cli samples/synthetic/format_a1.pdf")
        print("  PYTHONPATH=. .venv/bin/python -m src.cli samples/synthetic/format_b.pdf")
        print("  PYTHONPATH=. .venv/bin/python -m src.cli samples/synthetic/format_c.pdf")

    return 0


if __name__ == "__main__":
    sys.exit(main())
