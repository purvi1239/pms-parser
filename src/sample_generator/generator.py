"""Top-level generator: produces all sample PDFs and ground-truth JSON files.

Usage
-----
python -m src.sample_generator               # writes to samples/synthetic/
python -m src.sample_generator --out /path   # writes to specified directory
"""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

from src.sample_generator._data import get_all_samples, SampleData
from src.sample_generator._ground_truth import write_ground_truth
from src.sample_generator.formats.format_a import build_format_a
from src.sample_generator.formats.format_a_messy import build_format_a_messy
from src.sample_generator.formats.format_a_drift import build_format_a_drift
from src.sample_generator.formats.format_b import build_format_b
from src.sample_generator.formats.format_c import build_format_c

# Password used for the encrypted sample — kept here (test-only config, never committed)
ENCRYPTED_SAMPLE_PASSWORD = "TestPass@123"
ENCRYPTED_SAMPLE_LABEL    = "format_a1_encrypted"

# Map each label to its PDF builder function
_BUILDERS = {
    "format_a1":      build_format_a,
    "format_a2":      build_format_a,
    "format_b":       build_format_b,
    "format_c":       build_format_c,
    "format_a_messy": build_format_a_messy,
    "format_a_drift": build_format_a_drift,
}


def _encrypt_pdf(plain_path: Path, encrypted_path: Path, password: str) -> None:
    """Encrypt a PDF with user password using PyPDF2."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(plain_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    with encrypted_path.open("wb") as f:
        writer.write(f)


def generate_all(out_dir: Path | str | None = None) -> Path:
    """Generate all sample PDFs and ground-truth JSON files.

    Args:
        out_dir: Directory to write samples into.  Defaults to
                 ``<project_root>/samples/synthetic``.

    Returns:
        The resolved output directory path.
    """
    if out_dir is None:
        # Resolve relative to this file: src/sample_generator/generator.py
        # → project root is ../../..
        project_root = Path(__file__).parent.parent.parent
        out_dir = project_root / "samples" / "synthetic"

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = get_all_samples()

    for label, data in samples.items():
        builder = _BUILDERS[label]
        pdf_path = out_dir / f"{label}.pdf"
        json_path = out_dir / f"{label}.json"

        builder(data, pdf_path)
        write_ground_truth(data, json_path)

    # ── Encrypted sample ─────────────────────────────────────────────────────
    plain_pdf  = out_dir / "format_a1.pdf"
    enc_pdf    = out_dir / f"{ENCRYPTED_SAMPLE_LABEL}.pdf"
    # Ground truth is identical to format_a1
    enc_json   = out_dir / f"{ENCRYPTED_SAMPLE_LABEL}.json"

    _encrypt_pdf(plain_pdf, enc_pdf, ENCRYPTED_SAMPLE_PASSWORD)

    # Write a ground-truth JSON copy (same data, different label)
    a1_data = samples["format_a1"]
    import dataclasses
    enc_data = dataclasses.replace(a1_data, label=ENCRYPTED_SAMPLE_LABEL)
    write_ground_truth(enc_data, enc_json)

    return out_dir

