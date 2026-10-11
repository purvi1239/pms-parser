"""Executable entry point for the PMS statement parser CLI: python -m src.cli"""
import sys

from src.cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
