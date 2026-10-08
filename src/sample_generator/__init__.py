"""Sample generator package.

Run as a module to regenerate all synthetic samples:

    python -m src.sample_generator [--out <directory>]
"""
from src.sample_generator.generator import generate_all, ENCRYPTED_SAMPLE_PASSWORD, ENCRYPTED_SAMPLE_LABEL

__all__ = ["generate_all", "ENCRYPTED_SAMPLE_PASSWORD", "ENCRYPTED_SAMPLE_LABEL"]
