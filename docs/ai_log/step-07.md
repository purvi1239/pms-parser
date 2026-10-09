# Step 07: Deterministic Parsing Specifications and SpecStore (Build Step 6)

## What Was Implemented

Implemented the deterministic parsing specification framework and `SpecStore` in `src/specs/` (with convenience alias `src/spec/`), strictly adhering to Project Plan v4 and the Step 6 requirements:

1. **Typed Parsing Specification Models (`src/specs/models.py`)**:
   - `LocatorType`: String-backed enumeration defining locator strategies:
     - `TABLE_AFTER_ANCHOR = "table_after_anchor"`
     - `VALUE_RIGHT_OF_ANCHOR = "value_right_of_anchor"`
     - `VALUE_BELOW_ANCHOR = "value_below_anchor"`
     - `VALUE_MATCHING_PATTERN = "value_matching_pattern"`
     - `BOUNDING_BOX = "bounding_box"` (absolute fallback)
     - Implements `_missing_` for case-insensitive and trimmed string input resolution.
   - `FieldLocator`: Pydantic model for locating scalar fields:
     - Properties `is_anchor_relative` and `is_fallback`.
     - Validates that anchor-relative types supply non-empty `anchor`.
     - Validates that pattern types supply a compilable regex `pattern`.
     - Supports `fallback_bbox` on anchor-relative locators.
     - Coerces 4-element tuples/lists/dicts into `BBox` instances.
   - `TableLocator`: Pydantic model for locating structured tabular sections:
     - Configures `anchor`, `header_keywords`, `column_mapping`, `stop_anchor`, `multi_page`, and `repeat_headers`.
     - Allows `bounding_box` as an explicit fallback and `fallback_bbox` for anchor-relative strategies.
   - `ParsingSpec`: Top-level specification entity:
     - Contains `format_id`, `version`, `statement_type` (`StatementType.PMS` or `StatementType.AIF`), `description`, optional `fingerprint` (`FormatFingerprint`), `metadata_fields`, `summary_fields`, `holdings_table`, `transactions_table`, `custom_tables`, and `metadata`.
     - Provides lossless serialization helpers: `to_json()`, `from_json()`, `to_dict()`, and `from_dict()`.

2. **Specification Exceptions (`src/specs/exceptions.py`)**:
   - `SpecError`: Base exception for all specification domain errors.
   - `SpecNotFoundError`: Raised when a requested specification file does not exist on disk.
   - `SpecValidationError`: Raised when JSON is malformed, required fields are missing, or schema validation fails.
   - `DuplicateSpecError`: Raised when attempting to save a specification whose `format_id` already exists without `overwrite=True`.

3. **Filesystem SpecStore Repository (`src/specs/store.py`)**:
   - Manages specification JSON files in the project root's `specs/` directory (`specs/<format_id>.json`).
   - `save(spec, overwrite=False)`: Atomically writes formatted JSON (via temporary file swap) and prevents unintended overwrites.
   - `save_from_dict(data, overwrite=False)` & `save_from_json(json_str, overwrite=False)`: Convenience methods with upfront schema validation.
   - `load(format_id)`: Loads and validates specification from disk, raising `SpecNotFoundError` or `SpecValidationError`.
   - `get(format_id)`: Safe lookup returning `ParsingSpec | None` without raising on missing specs.
   - `delete(format_id)` & `exists(format_id)`: Safe filesystem operations.
   - `list_formats()` & `list_specs(ignore_invalid=False)`: Enumerates available specifications, with an `ignore_invalid` safety option.
   - `get_by_fingerprint(query_fingerprint, match_threshold=0.90)`: Directly looks up a matching `ParsingSpec` using the Step 5 fingerprint comparator (`match_fingerprint`) across all stored specs having registered fingerprints.
   - `match_spec_by_fingerprint(...)`: Returns `(ParsingSpec | None, FingerprintMatchResult)` providing full similarity diagnostics and drift classification.

## Files Created / Modified

**Created:**
- `src/specs/__init__.py`: Primary module exports (`ParsingSpec`, `FieldLocator`, `TableLocator`, `LocatorType`, `SpecStore`, exceptions).
- `src/specs/models.py`: Pydantic models for specifications and locators.
- `src/specs/store.py`: `SpecStore` implementation.
- `src/specs/exceptions.py`: Custom exception hierarchy.
- `src/spec/__init__.py`: Package alias re-exporting `src.specs` for import flexibility.
- `tests/test_specs.py`: Focused test suite with 22 test cases.
- `docs/ai_log/step-07.md`: Step documentation.

**Modified:**
- None (existing models, IR extractor, classifier, fingerprinting, and sample generator were left completely intact).

## Design Decisions

1. **Anchor-Relative Primacy & Fallback Bounding Boxes**:
   - Locators prioritize anchor-relative coordinates (`value_right_of_anchor`, `value_below_anchor`, `table_after_anchor`) or regex pattern matching (`value_matching_pattern`) so that layout adjustments (font resizing, header spacing, pagination shifts) do not break field extraction.
   - Absolute bounding boxes (`bounding_box` locator type and `fallback_bbox` attribute) are available exclusively as fallbacks when stable text anchors cannot be established.
2. **Safe Atomic Storage**:
   - `SpecStore.save()` writes to a hidden temporary file before performing an atomic rename via `os.replace`. This eliminates the risk of truncated or corrupted specification files during interruptions.
3. **Fingerprint Integration**:
   - `SpecStore` links seamlessly with the Step 5 `FormatFingerprint` module: candidate documents can be fingerprinted and queried against the store, retrieving the exact specification for `KNOWN` templates while detecting `DRIFT_SUSPECTED` templates for review.
4. **Resilient Error Classification**:
   - Syntax-level JSON errors (malformed files) and schema validation errors (missing required fields, invalid regexes) are wrapped in `SpecValidationError` with informative diagnostic messages.

## Tests Run

Executed:
```bash
PYTHONPATH=. ./.venv/bin/pytest tests/test_specs.py -v
```

Output:
```
======================== 24 passed in 104.02s (0:01:44) ========================
```

Test coverage includes:
- `test_locator_type_enum_members_and_case_insensitivity`: Case-insensitive parsing and enum validation.
- `test_field_locator_anchor_relative_right`: Horizontal anchor-relative locator.
- `test_field_locator_anchor_relative_below`: Vertical anchor-relative locator.
- `test_field_locator_pattern_matching`: Regex pattern locator.
- `test_field_locator_bounding_box_fallback`: Fallback bounding box locator.
- `test_field_locator_anchor_with_fallback_bbox`: Anchor locator with fallback bounding box.
- `test_field_locator_validation_errors`: Validates mandatory anchor, pattern, and bbox configurations.
- `test_table_locator_after_anchor`: Multi-page table locator configuration.
- `test_table_locator_fallback_bbox`: Fallback bounding box for table locators.
- `test_table_locator_validation_errors`: Incomplete table locator configuration validation.
- `test_parsing_spec_json_roundtrip`: Full lossless serialization roundtrip to/from JSON and dict.
- `test_parsing_spec_invalid_format_id`: Rejection of blank format IDs.
- `test_spec_store_save_and_load`: Disk persistence and retrieval.
- `test_spec_store_duplicate_prevention`: Rejection of duplicate saves without `overwrite=True`.
- `test_spec_store_missing_specifications`: Safe handling of non-existent specifications.
- `test_spec_store_corrupted_json_file`: Exception handling for corrupted JSON files.
- `test_spec_store_invalid_schema_file`: Exception handling for invalid schema files.
- `test_spec_store_list_specs_ignore_invalid`: Tolerant catalog enumeration with `ignore_invalid=True`.
- `test_spec_store_list_and_delete`: Format listing and safe file removal.
- `test_spec_store_lookup_by_fingerprint`: Fingerprint matching and retrieval against candidate documents.
- `test_spec_store_save_from_dict_and_json`: Convenience loaders with schema validation.
- `test_default_specs_dir`: Proper default storage path resolution.
- `test_spec_store_path_traversal_attempts_rejected`: Verifies rejection of directory traversal (`../outside`, `/outside`, null bytes, etc.) and confirms zero files created outside `specs_dir`.
- `test_spec_store_valid_format_ids_allowed`: Verifies standard format IDs with hyphens, underscores, numbers, and case variations succeed.
