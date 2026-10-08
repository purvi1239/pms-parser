"""CLI entry point: python -m src.sample_generator [--out <dir>]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate all synthetic PMS/AIF sample PDFs and ground-truth JSON files."
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        default=None,
        help="Output directory (default: samples/synthetic/ in project root)",
    )
    args = parser.parse_args()

    from src.sample_generator.generator import generate_all
    out = generate_all(out_dir=args.out)
    print(f"Samples written to: {out}")


if __name__ == "__main__":
    main()
