"""Filesystem-backed specification store for managing deterministic parsing specs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from pydantic import ValidationError

from src.fingerprint import (
    FingerprintMatchResult,
    FingerprintMatchStatus,
    FormatFingerprint,
    match_fingerprint,
)
from src.specs.exceptions import (
    DuplicateSpecError,
    SpecNotFoundError,
    SpecValidationError,
)
from src.specs.models import ParsingSpec


def get_default_specs_dir() -> Path:
    """Return the default specs directory (<project_root>/specs)."""
    # src/specs/store.py -> parent.parent.parent is project root
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "specs"


class SpecStore:
    """Repository for saving, loading, listing, and fingerprint-matching parsing specifications."""

    def __init__(self, specs_dir: Path | str | None = None) -> None:
        """Initialize SpecStore.

        Args:
            specs_dir: Directory where specification JSON files are stored.
                       Defaults to '<project_root>/specs'.
        """
        if specs_dir is None:
            self.specs_dir = get_default_specs_dir()
        else:
            self.specs_dir = Path(specs_dir).resolve()

        self.specs_dir.mkdir(parents=True, exist_ok=True)

    def _spec_path(self, format_id: str) -> Path:
        """Resolve the file path for a given format_id safely within specs_dir."""
        if not format_id or not isinstance(format_id, str):
            raise SpecValidationError("format_id must be a non-empty string.")
        clean_id = format_id.strip()
        if not clean_id:
            raise SpecValidationError("format_id must not be blank.")

        # Reject path separators, directory traversal, and null bytes
        if "/" in clean_id or "\\" in clean_id or "\0" in clean_id:
            raise SpecValidationError(
                f"Invalid format_id '{format_id}': path separators are not permitted."
            )

        # Reject '.' or '..' path traversal components
        if clean_id in (".", "..") or clean_id.startswith(".") or ".." in clean_id:
            raise SpecValidationError(
                f"Invalid format_id '{format_id}': '.' or '..' path components are not permitted."
            )

        # Allow normal alphanumeric IDs with underscores, hyphens, and safe internal dots
        if not re.match(r"^[A-Za-z0-9_-]+(?:[.][A-Za-z0-9_-]+)*$", clean_id):
            raise SpecValidationError(
                f"Invalid format_id '{format_id}': contains unsafe characters."
            )

        candidate = (self.specs_dir / f"{clean_id}.json").resolve()
        specs_dir_resolved = self.specs_dir.resolve()
        if candidate.parent != specs_dir_resolved or not candidate.is_relative_to(specs_dir_resolved):
            raise SpecValidationError(
                f"Invalid format_id '{format_id}': path escapes the specs directory."
            )

        return self.specs_dir / f"{clean_id}.json"

    def exists(self, format_id: str) -> bool:
        """Check whether a specification exists for the given format_id."""
        try:
            return self._spec_path(format_id).is_file()
        except SpecValidationError:
            return False

    def save(self, spec: ParsingSpec, overwrite: bool = False) -> Path:
        """Save a parsing specification to disk as JSON.

        Args:
            spec: The ParsingSpec instance to persist.
            overwrite: If False and the specification already exists,
                       raises DuplicateSpecError. If True, overwrites.

        Returns:
            The Path where the specification file was written.

        Raises:
            DuplicateSpecError: If a spec with this format_id exists and overwrite=False.
            SpecValidationError: If the spec is invalid.
        """
        if not isinstance(spec, ParsingSpec):
            raise SpecValidationError(f"Expected ParsingSpec instance, got {type(spec).__name__}.")

        target_path = self._spec_path(spec.format_id)

        if target_path.exists() and not overwrite:
            raise DuplicateSpecError(
                f"Specification for format '{spec.format_id}' already exists at {target_path}. "
                "Set overwrite=True to replace it."
            )

        try:
            json_text = spec.to_json(indent=2)
        except Exception as e:
            raise SpecValidationError(f"Failed to serialize specification '{spec.format_id}': {e}") from e

        # Atomic write via temporary file
        temp_path = self.specs_dir / f".{spec.format_id}.tmp"
        try:
            temp_path.write_text(json_text, encoding="utf-8")
            os.replace(temp_path, target_path)
        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise SpecValidationError(f"Failed to write specification to {target_path}: {e}") from e

        return target_path

    def save_from_dict(self, data: dict[str, Any], overwrite: bool = False) -> Path:
        """Validate and save a specification defined as a dictionary."""
        try:
            spec = ParsingSpec.from_dict(data)
        except (ValidationError, ValueError) as e:
            raise SpecValidationError(f"Invalid specification schema: {e}") from e
        return self.save(spec, overwrite=overwrite)

    def save_from_json(self, json_str: str, overwrite: bool = False) -> Path:
        """Validate and save a specification defined as a JSON string."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SpecValidationError(f"Invalid specification JSON: {e}") from e
        try:
            spec = ParsingSpec.from_dict(data)
        except (ValidationError, ValueError) as e:
            raise SpecValidationError(f"Invalid specification schema: {e}") from e
        return self.save(spec, overwrite=overwrite)

    def load(self, format_id: str) -> ParsingSpec:
        """Load and validate a specification by its format_id.

        Args:
            format_id: Unique identifier of the format specification.

        Returns:
            The validated ParsingSpec instance.

        Raises:
            SpecNotFoundError: If no file exists for format_id.
            SpecValidationError: If the file is malformed JSON or fails schema validation.
        """
        target_path = self._spec_path(format_id)

        if not target_path.is_file():
            raise SpecNotFoundError(
                f"Specification for format '{format_id}' not found at {target_path}."
            )

        try:
            content = target_path.read_text(encoding="utf-8")
        except Exception as e:
            raise SpecValidationError(f"Failed to read specification file {target_path}: {e}") from e

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise SpecValidationError(
                f"Corrupted or invalid JSON in specification file {target_path}: {e}"
            ) from e

        try:
            return ParsingSpec.from_dict(data)
        except (ValidationError, ValueError) as e:
            raise SpecValidationError(
                f"Specification schema validation failed for {target_path}: {e}"
            ) from e

    def get(self, format_id: str) -> ParsingSpec | None:
        """Safely retrieve a specification by format_id, returning None if missing.

        Raises:
            SpecValidationError: If the file exists but has invalid JSON or schema.
        """
        try:
            return self.load(format_id)
        except SpecNotFoundError:
            return None

    def delete(self, format_id: str) -> bool:
        """Delete a specification file by format_id.

        Returns:
            True if file was deleted, False if it did not exist.
        """
        try:
            path = self._spec_path(format_id)
        except SpecValidationError:
            return False

        if path.is_file():
            path.unlink()
            return True
        return False

    def list_formats(self) -> list[str]:
        """Return a sorted list of all stored format IDs."""
        formats: list[str] = []
        for p in self.specs_dir.glob("*.json"):
            if not p.name.startswith("."):
                formats.append(p.stem)
        return sorted(formats)

    def list_specs(self, ignore_invalid: bool = False) -> list[ParsingSpec]:
        """Load and return all stored specifications.

        Args:
            ignore_invalid: If True, skip files that fail parsing or validation.
                            If False, raise SpecValidationError on first invalid file.
        """
        specs: list[ParsingSpec] = []
        for format_id in self.list_formats():
            try:
                specs.append(self.load(format_id))
            except SpecValidationError:
                if not ignore_invalid:
                    raise
        return specs

    def get_by_fingerprint(
        self,
        query_fingerprint: FormatFingerprint,
        match_threshold: float = 0.90,
    ) -> ParsingSpec | None:
        """Retrieve a specification whose associated fingerprint matches the query document.

        Args:
            query_fingerprint: The extracted fingerprint from an unknown or candidate document.
            match_threshold: Minimum similarity score required to declare a KNOWN match (default 0.90).

        Returns:
            The matched ParsingSpec if a KNOWN match is found, otherwise None.
        """
        spec, _ = self.match_spec_by_fingerprint(query_fingerprint, match_threshold=match_threshold)
        return spec

    def match_spec_by_fingerprint(
        self,
        query_fingerprint: FormatFingerprint,
        match_threshold: float = 0.90,
        near_match_threshold: float = 0.65,
    ) -> tuple[ParsingSpec | None, FingerprintMatchResult]:
        """Match a document fingerprint against all stored specifications having fingerprints.

        Args:
            query_fingerprint: FormatFingerprint of the query document.
            match_threshold: Threshold for KNOWN match.
            near_match_threshold: Threshold for DRIFT_SUSPECTED.

        Returns:
            A tuple of (matched_spec_or_None, FingerprintMatchResult).
        """
        library: dict[str, FormatFingerprint] = {}
        for spec in self.list_specs(ignore_invalid=True):
            if spec.fingerprint is not None:
                # Use format_id as library key
                library[spec.format_id] = spec.fingerprint

        if not library:
            empty_result = FingerprintMatchResult(
                status=FingerprintMatchStatus.UNKNOWN,
                similarity_score=0.0,
                matched_format_id=None,
                reasons=["SpecStore contains no specifications with registered fingerprints."],
            )
            return None, empty_result

        result = match_fingerprint(
            query_fingerprint,
            library,
            match_threshold=match_threshold,
            near_match_threshold=near_match_threshold,
        )

        matched_spec: ParsingSpec | None = None
        if result.status == FingerprintMatchStatus.KNOWN and result.matched_format_id:
            matched_spec = self.get(result.matched_format_id)

        return matched_spec, result
