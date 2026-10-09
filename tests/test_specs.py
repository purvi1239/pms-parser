"""Tests for Step 6: Deterministic Parsing Specifications and SpecStore.

Covers:
  1. LocatorType enum parsing and case-insensitivity.
  2. Anchor-relative field locators (value_right_of_anchor, value_below_anchor, value_matching_pattern).
  3. Absolute bounding-box fallback locators (explicit bounding_box and fallback_bbox).
  4. FieldLocator and TableLocator validation and error handling for missing/invalid inputs.
  5. ParsingSpec typed models and lossless JSON/dict serialization roundtrip.
  6. SpecStore save, load, get, exists, list_formats, and delete operations.
  7. SpecStore duplicate specification handling (DuplicateSpecError vs overwrite=True).
  8. SpecStore missing specification handling (SpecNotFoundError vs get returning None).
  9. SpecStore corrupted or invalid JSON file handling (SpecValidationError, ignore_invalid flag).
 10. SpecStore fingerprint-based lookup (get_by_fingerprint and match_spec_by_fingerprint).
 11. SpecStore save_from_dict and save_from_json convenience methods.
"""
from __future__ import annotations

from pathlib import Path
import pytest
from pydantic import ValidationError

from src.fingerprint.models import (
    FingerprintMatchResult,
    FingerprintMatchStatus,
    FormatFingerprint,
    LayoutStats,
)
from src.ir.models import BBox
from src.models.enums import StatementType
from src.specs import (
    DuplicateSpecError,
    FieldLocator,
    LocatorType,
    ParsingSpec,
    SpecError,
    SpecNotFoundError,
    SpecStore,
    SpecValidationError,
    TableLocator,
    get_default_specs_dir,
)


# ---------------------------------------------------------------------------
# 1. LocatorType enum tests
# ---------------------------------------------------------------------------

def test_locator_type_enum_members_and_case_insensitivity():
    """Verify all required locator types are defined and parse case-insensitively."""
    assert LocatorType.TABLE_AFTER_ANCHOR == "table_after_anchor"
    assert LocatorType.VALUE_RIGHT_OF_ANCHOR == "value_right_of_anchor"
    assert LocatorType.VALUE_BELOW_ANCHOR == "value_below_anchor"
    assert LocatorType.VALUE_MATCHING_PATTERN == "value_matching_pattern"
    assert LocatorType.BOUNDING_BOX == "bounding_box"

    # Case-insensitive / whitespace resolution via _missing_
    assert LocatorType("TABLE_AFTER_ANCHOR") == LocatorType.TABLE_AFTER_ANCHOR
    assert LocatorType("Value_Right_Of_Anchor") == LocatorType.VALUE_RIGHT_OF_ANCHOR
    assert LocatorType("  value_below_anchor  ") == LocatorType.VALUE_BELOW_ANCHOR
    assert LocatorType("value_matching_pattern") == LocatorType.VALUE_MATCHING_PATTERN
    assert LocatorType("BOUNDING_BOX") == LocatorType.BOUNDING_BOX


# ---------------------------------------------------------------------------
# 2. FieldLocator anchor-relative locators
# ---------------------------------------------------------------------------

def test_field_locator_anchor_relative_right():
    """value_right_of_anchor requires an anchor and is identified as anchor-relative."""
    loc = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Client ID:",
        page_number=1,
        offset_x=120.0,
        data_type="str",
        required=True,
    )
    assert loc.locator_type == LocatorType.VALUE_RIGHT_OF_ANCHOR
    assert loc.anchor == "Client ID:"
    assert loc.page_number == 1
    assert loc.offset_x == 120.0
    assert loc.is_anchor_relative is True
    assert loc.is_fallback is False


def test_field_locator_anchor_relative_below():
    """value_below_anchor locates values beneath a designated header label."""
    loc = FieldLocator(
        locator_type=LocatorType.VALUE_BELOW_ANCHOR,
        anchor="Total Portfolio Value",
        offset_y=30.0,
        data_type="decimal",
    )
    assert loc.locator_type == LocatorType.VALUE_BELOW_ANCHOR
    assert loc.anchor == "Total Portfolio Value"
    assert loc.is_anchor_relative is True
    assert loc.is_fallback is False


def test_field_locator_pattern_matching():
    """value_matching_pattern locates values matching a regex pattern."""
    loc = FieldLocator(
        locator_type=LocatorType.VALUE_MATCHING_PATTERN,
        pattern=r"INR\s+([0-9,]+\.[0-9]{2})",
        data_type="decimal",
    )
    assert loc.locator_type == LocatorType.VALUE_MATCHING_PATTERN
    assert loc.pattern == r"INR\s+([0-9,]+\.[0-9]{2})"
    assert loc.is_anchor_relative is True
    assert loc.is_fallback is False


# ---------------------------------------------------------------------------
# 3. Fallback bounding-box locators
# ---------------------------------------------------------------------------

def test_field_locator_bounding_box_fallback():
    """Allow absolute bounding-box locator as an explicit fallback."""
    bbox = BBox(x0=50.0, top=100.0, x1=250.0, bottom=120.0)
    loc = FieldLocator(
        locator_type=LocatorType.BOUNDING_BOX,
        bbox=bbox,
        page_number=1,
        data_type="str",
    )
    assert loc.locator_type == LocatorType.BOUNDING_BOX
    assert loc.bbox == bbox
    assert loc.is_anchor_relative is False
    assert loc.is_fallback is True


def test_field_locator_anchor_with_fallback_bbox():
    """Anchor-relative locator can specify fallback_bbox if anchor lookup fails."""
    loc = FieldLocator(
        locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
        anchor="Statement Period:",
        fallback_bbox=(50.0, 70.0, 300.0, 90.0),  # coerced from 4-tuple
    )
    assert loc.is_anchor_relative is True
    assert loc.is_fallback is True
    assert loc.fallback_bbox == BBox(x0=50.0, top=70.0, x1=300.0, bottom=90.0)


# ---------------------------------------------------------------------------
# 4. Locator validation and error cases
# ---------------------------------------------------------------------------

def test_field_locator_validation_errors():
    """Ensure invalid or incomplete locator configurations raise clear ValidationErrors."""
    # Missing anchor for value_right_of_anchor
    with pytest.raises(ValidationError) as exc:
        FieldLocator(locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR, anchor=None)
    assert "requires a non-empty 'anchor'" in str(exc.value)

    # Empty anchor string
    with pytest.raises(ValidationError) as exc:
        FieldLocator(locator_type=LocatorType.VALUE_BELOW_ANCHOR, anchor="   ")
    assert "requires a non-empty 'anchor'" in str(exc.value)

    # Missing pattern for value_matching_pattern
    with pytest.raises(ValidationError) as exc:
        FieldLocator(locator_type=LocatorType.VALUE_MATCHING_PATTERN, pattern=None)
    assert "requires a non-empty regex 'pattern'" in str(exc.value)

    # Invalid regex pattern syntax
    with pytest.raises(ValidationError) as exc:
        FieldLocator(locator_type=LocatorType.VALUE_MATCHING_PATTERN, pattern="(?P<invalid")
    assert "Invalid regex pattern" in str(exc.value)

    # Bounding box locator missing bbox
    with pytest.raises(ValidationError) as exc:
        FieldLocator(locator_type=LocatorType.BOUNDING_BOX, bbox=None)
    assert "requires a valid 'bbox'" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. TableLocator tests
# ---------------------------------------------------------------------------

def test_table_locator_after_anchor():
    """table_after_anchor locator captures table regions following section headers."""
    table_loc = TableLocator(
        locator_type=LocatorType.TABLE_AFTER_ANCHOR,
        anchor="Holdings",
        header_keywords=["Security Name", "ISIN", "Quantity", "Market Value"],
        column_mapping={
            "security_name": "Security Name",
            "isin": "ISIN",
            "quantity": "Quantity",
            "market_value": "Market Value",
        },
        stop_anchor="Total Holdings",
        multi_page=True,
        repeat_headers=True,
    )
    assert table_loc.locator_type == LocatorType.TABLE_AFTER_ANCHOR
    assert table_loc.anchor == "Holdings"
    assert table_loc.multi_page is True
    assert table_loc.repeat_headers is True
    assert table_loc.is_anchor_relative is True
    assert table_loc.is_fallback is False


def test_table_locator_fallback_bbox():
    """Table locator can use bounding box fallback or fallback_bbox."""
    # Absolute bbox locator
    bbox_table = TableLocator(
        locator_type=LocatorType.BOUNDING_BOX,
        bbox=(50.0, 200.0, 550.0, 700.0),
        column_mapping={"security_name": "0", "isin": "1"},
    )
    assert bbox_table.is_fallback is True
    assert bbox_table.is_anchor_relative is False
    assert bbox_table.bbox.width == 500.0

    # table_after_anchor with fallback_bbox
    rel_table = TableLocator(
        locator_type=LocatorType.TABLE_AFTER_ANCHOR,
        anchor="Transactions",
        fallback_bbox=(50.0, 400.0, 550.0, 750.0),
    )
    assert rel_table.is_anchor_relative is True
    assert rel_table.is_fallback is True


def test_table_locator_validation_errors():
    """Ensure incomplete table locator configs raise clear ValidationErrors."""
    # table_after_anchor without anchor or header_keywords
    with pytest.raises(ValidationError) as exc:
        TableLocator(locator_type=LocatorType.TABLE_AFTER_ANCHOR, anchor=None, header_keywords=[])
    assert "requires either an 'anchor' string or 'header_keywords'" in str(exc.value)

    # bounding_box table locator without bbox
    with pytest.raises(ValidationError) as exc:
        TableLocator(locator_type=LocatorType.BOUNDING_BOX, bbox=None)
    assert "requires a valid 'bbox'" in str(exc.value)


# ---------------------------------------------------------------------------
# 6. ParsingSpec model and JSON serialization roundtrip
# ---------------------------------------------------------------------------

def test_parsing_spec_json_roundtrip():
    """ParsingSpec fully serializes to JSON and deserializes back without data loss."""
    fp = FormatFingerprint(
        format_id="format_a",
        anchor_phrases=["portfolio management statement", "holdings", "transactions"],
        table_headers=[["security name", "isin", "qty", "cost", "mkt value", "weight"]],
        layout_stats=LayoutStats(page_count=2, table_count=2, column_counts=[6, 5]),
    )

    spec = ParsingSpec(
        format_id="format_a",
        version="1.2.0",
        statement_type=StatementType.PMS,
        description="Pinnacle Wealth PMS Format A Specification",
        fingerprint=fp,
        metadata_fields={
            "provider_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                pattern=r"(Pinnacle Wealth Management)",
                required=True,
            ),
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Client ID:",
                required=True,
            ),
            "statement_period": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Statement Period:",
            ),
            "currency": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Currency:",
                default="INR",
            ),
        },
        summary_fields={
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Portfolio Value",
                data_type="decimal",
            ),
            "closing_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Portfolio Value",
                data_type="decimal",
            ),
        },
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Holdings",
            header_keywords=["Security Name", "ISIN", "Quantity"],
            column_mapping={"security_name": "Security Name", "isin": "ISIN", "quantity": "Quantity"},
            multi_page=True,
            repeat_headers=True,
        ),
        transactions_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Transactions",
            header_keywords=["Date", "Type", "Security", "Qty", "Amount"],
            column_mapping={"date": "Date", "type": "Type", "security": "Security", "amount": "Amount"},
        ),
        metadata={"author": "Antigravity", "notes": "Production PMS spec"},
    )

    # Serialize to JSON and deserialize back
    json_str = spec.to_json(indent=2)
    assert isinstance(json_str, str)
    assert '"format_a"' in json_str

    loaded_spec = ParsingSpec.from_json(json_str)
    assert loaded_spec.format_id == spec.format_id
    assert loaded_spec.version == spec.version
    assert loaded_spec.statement_type == StatementType.PMS
    assert loaded_spec.description == spec.description
    assert loaded_spec.fingerprint.anchor_phrases == spec.fingerprint.anchor_phrases
    assert len(loaded_spec.metadata_fields) == 4
    assert loaded_spec.metadata_fields["client_identifier"].anchor == "Client ID:"
    assert loaded_spec.holdings_table.multi_page is True
    assert loaded_spec.transactions_table.anchor == "Transactions"

    # Dict roundtrip
    data_dict = spec.to_dict()
    assert isinstance(data_dict, dict)
    dict_loaded = ParsingSpec.from_dict(data_dict)
    assert dict_loaded.format_id == spec.format_id


def test_parsing_spec_invalid_format_id():
    """Empty or blank format_id must raise ValidationError."""
    with pytest.raises(ValidationError):
        ParsingSpec(format_id="")

    with pytest.raises(ValidationError):
        ParsingSpec(format_id="   ")


# ---------------------------------------------------------------------------
# 7. SpecStore save, load, and retrieval
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_store(tmp_path: Path) -> SpecStore:
    """Provide a clean SpecStore instance using an isolated temp directory."""
    store_dir = tmp_path / "specs_store"
    return SpecStore(specs_dir=store_dir)


@pytest.fixture
def sample_spec() -> ParsingSpec:
    """Create a minimal valid ParsingSpec for testing."""
    return ParsingSpec(
        format_id="test_format_pms",
        version="1.0.0",
        statement_type=StatementType.PMS,
        metadata_fields={
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Client ID:",
            )
        },
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            anchor="Holdings",
            header_keywords=["Security", "ISIN"],
        ),
    )


def test_spec_store_save_and_load(temp_store: SpecStore, sample_spec: ParsingSpec):
    """Save a specification to disk and load it back."""
    saved_path = temp_store.save(sample_spec)
    assert saved_path.is_file()
    assert saved_path.name == "test_format_pms.json"

    # Verify exists
    assert temp_store.exists("test_format_pms") is True
    assert temp_store.exists("non_existent") is False

    # Load and verify content
    loaded = temp_store.load("test_format_pms")
    assert loaded.format_id == "test_format_pms"
    assert loaded.statement_type == StatementType.PMS
    assert "client_identifier" in loaded.metadata_fields

    # get() returns the same spec
    got = temp_store.get("test_format_pms")
    assert got is not None
    assert got.format_id == loaded.format_id


# ---------------------------------------------------------------------------
# 8. Duplicate specification handling
# ---------------------------------------------------------------------------

def test_spec_store_duplicate_prevention(temp_store: SpecStore, sample_spec: ParsingSpec):
    """Attempting to save a duplicate format_id without overwrite raises DuplicateSpecError."""
    temp_store.save(sample_spec)

    # Second save with overwrite=False must fail
    with pytest.raises(DuplicateSpecError) as exc:
        temp_store.save(sample_spec, overwrite=False)
    assert "already exists" in str(exc.value)

    # Second save with overwrite=True must succeed
    sample_spec.version = "1.0.1"
    updated_path = temp_store.save(sample_spec, overwrite=True)
    assert updated_path.is_file()

    reloaded = temp_store.load("test_format_pms")
    assert reloaded.version == "1.0.1"


# ---------------------------------------------------------------------------
# 9. Missing specification handling
# ---------------------------------------------------------------------------

def test_spec_store_missing_specifications(temp_store: SpecStore):
    """Missing format_id raises SpecNotFoundError in load(), returns None in get()."""
    with pytest.raises(SpecNotFoundError) as exc:
        temp_store.load("missing_format")
    assert "missing_format" in str(exc.value)

    assert temp_store.get("missing_format") is None
    assert temp_store.exists("missing_format") is False
    assert temp_store.delete("missing_format") is False


# ---------------------------------------------------------------------------
# 10. Invalid, corrupt, and malformed specification handling
# ---------------------------------------------------------------------------

def test_spec_store_corrupted_json_file(temp_store: SpecStore):
    """Corrupted JSON file raises SpecValidationError."""
    corrupt_file = temp_store.specs_dir / "corrupted.json"
    corrupt_file.write_text("{malformed json string:", encoding="utf-8")

    with pytest.raises(SpecValidationError) as exc:
        temp_store.load("corrupted")
    assert "Corrupted or invalid JSON" in str(exc.value)

    with pytest.raises(SpecValidationError):
        temp_store.get("corrupted")


def test_spec_store_invalid_schema_file(temp_store: SpecStore):
    """JSON file with invalid schema raises SpecValidationError."""
    invalid_file = temp_store.specs_dir / "invalid_schema.json"
    invalid_file.write_text('{"missing_format_id": true}', encoding="utf-8")

    with pytest.raises(SpecValidationError) as exc:
        temp_store.load("invalid_schema")
    assert "schema validation failed" in str(exc.value)


def test_spec_store_list_specs_ignore_invalid(temp_store: SpecStore, sample_spec: ParsingSpec):
    """list_specs ignores invalid files when ignore_invalid=True, raises when False."""
    temp_store.save(sample_spec)

    # Introduce a broken file
    bad_file = temp_store.specs_dir / "bad.json"
    bad_file.write_text("invalid json...", encoding="utf-8")

    # With ignore_invalid=False, error is raised
    with pytest.raises(SpecValidationError):
        temp_store.list_specs(ignore_invalid=False)

    # With ignore_invalid=True, invalid file is skipped and valid specs are returned
    valid_specs = temp_store.list_specs(ignore_invalid=True)
    assert len(valid_specs) == 1
    assert valid_specs[0].format_id == "test_format_pms"


# ---------------------------------------------------------------------------
# 11. SpecStore listing and deletion
# ---------------------------------------------------------------------------

def test_spec_store_list_and_delete(temp_store: SpecStore):
    """list_formats enumerates format IDs and delete removes them."""
    spec_a = ParsingSpec(format_id="format_alpha")
    spec_b = ParsingSpec(format_id="format_beta")

    temp_store.save(spec_a)
    temp_store.save(spec_b)

    assert temp_store.list_formats() == ["format_alpha", "format_beta"]
    assert len(temp_store.list_specs()) == 2

    # Delete alpha
    assert temp_store.delete("format_alpha") is True
    assert temp_store.list_formats() == ["format_beta"]
    assert temp_store.exists("format_alpha") is False


# ---------------------------------------------------------------------------
# 12. SpecStore fingerprint matching and lookup
# ---------------------------------------------------------------------------

def test_spec_store_lookup_by_fingerprint(temp_store: SpecStore):
    """Retrieve specifications by matching against candidate document fingerprints."""
    fp_a = FormatFingerprint(
        format_id="fmt_a",
        anchor_phrases=["pms growth statement", "holdings", "transactions"],
        table_headers=[["security", "isin", "qty", "market value"]],
        layout_stats=LayoutStats(page_count=1, table_count=2, column_counts=[4, 3]),
    )
    fp_b = FormatFingerprint(
        format_id="fmt_b",
        anchor_phrases=["alternative investment fund", "cash ledger", "nav per unit"],
        table_headers=[["date", "description", "debit", "credit", "balance"]],
        layout_stats=LayoutStats(page_count=1, table_count=1, column_counts=[5]),
    )

    spec_a = ParsingSpec(format_id="fmt_a", statement_type=StatementType.PMS, fingerprint=fp_a)
    spec_b = ParsingSpec(format_id="fmt_b", statement_type=StatementType.AIF, fingerprint=fp_b)

    temp_store.save(spec_a)
    temp_store.save(spec_b)

    # 1. Query with exact/identical fingerprint for fmt_a -> retrieves spec_a
    matched_a = temp_store.get_by_fingerprint(fp_a)
    assert matched_a is not None
    assert matched_a.format_id == "fmt_a"

    # 2. Query with exact/identical fingerprint for fmt_b -> retrieves spec_b
    matched_b = temp_store.get_by_fingerprint(fp_b)
    assert matched_b is not None
    assert matched_b.format_id == "fmt_b"

    # 3. Query with completely unknown fingerprint -> returns None
    fp_unknown = FormatFingerprint(
        format_id="unknown",
        anchor_phrases=["unrelated invoice", "vendor", "total amount due"],
        table_headers=[["item", "rate", "subtotal"]],
        layout_stats=LayoutStats(page_count=1, table_count=1, column_counts=[3]),
    )
    assert temp_store.get_by_fingerprint(fp_unknown) is None

    # 4. Detailed match diagnostics via match_spec_by_fingerprint
    spec_res, match_info = temp_store.match_spec_by_fingerprint(fp_a)
    assert spec_res is not None
    assert isinstance(match_info, FingerprintMatchResult)
    assert match_info.status == FingerprintMatchStatus.KNOWN
    assert match_info.matched_format_id == "fmt_a"
    assert match_info.similarity_score == 1.0


# ---------------------------------------------------------------------------
# 13. SpecStore convenience save methods
# ---------------------------------------------------------------------------

def test_spec_store_save_from_dict_and_json(temp_store: SpecStore):
    """Test saving specifications directly from dictionary or JSON string."""
    data = {
        "format_id": "from_dict_format",
        "version": "2.0.0",
        "statement_type": "PMS",
        "metadata_fields": {
            "client_identifier": {
                "locator_type": "value_right_of_anchor",
                "anchor": "Client Number:",
            }
        },
    }

    path1 = temp_store.save_from_dict(data)
    assert path1.is_file()
    loaded1 = temp_store.load("from_dict_format")
    assert loaded1.version == "2.0.0"

    json_str = '{"format_id": "from_json_format", "statement_type": "AIF"}'
    path2 = temp_store.save_from_json(json_str)
    assert path2.is_file()
    loaded2 = temp_store.load("from_json_format")
    assert loaded2.statement_type == StatementType.AIF


def test_default_specs_dir():
    """Verify get_default_specs_dir resolves to <project_root>/specs."""
    default_dir = get_default_specs_dir()
    assert default_dir.name == "specs"
    assert default_dir.is_dir()


# ---------------------------------------------------------------------------
# 14. Path traversal and security validation tests
# ---------------------------------------------------------------------------

def test_spec_store_path_traversal_attempts_rejected(temp_store: SpecStore):
    """Path traversal attempts and unsafe IDs must be rejected without creating files."""
    unsafe_ids = [
        "../outside",
        "../../outside",
        "/outside",
        "/etc/passwd",
        "sub/directory",
        "nested\\slash",
        "..",
        ".",
        "./relative",
        "../format_evil",
        "format\0nullbyte",
        "format with spaces",
    ]

    outside_file = temp_store.specs_dir.parent / "outside.json"

    for bad_id in unsafe_ids:
        # load() must raise SpecValidationError
        with pytest.raises(SpecValidationError):
            temp_store.load(bad_id)

        # get() must raise SpecValidationError
        with pytest.raises(SpecValidationError):
            temp_store.get(bad_id)

        # exists() and delete() must safely return False
        assert temp_store.exists(bad_id) is False
        assert temp_store.delete(bad_id) is False

        # save() must raise SpecValidationError
        spec = ParsingSpec(format_id="placeholder")
        spec.format_id = bad_id
        with pytest.raises(SpecValidationError):
            temp_store.save(spec)

        # save_from_dict must raise SpecValidationError
        with pytest.raises(SpecValidationError):
            temp_store.save_from_dict({"format_id": bad_id})

    # Assert no file was created outside the configured specs directory
    assert not outside_file.exists()
    assert list(temp_store.specs_dir.parent.glob("outside*")) == []
    # Assert specs_dir itself remains clean
    assert len(list(temp_store.specs_dir.iterdir())) == 0


def test_spec_store_valid_format_ids_allowed(temp_store: SpecStore):
    """Ensure standard IDs with hyphens, underscores, numbers, and case variations succeed."""
    valid_ids = [
        "format_a",
        "test_format_pms",
        "strategy-growth-2026",
        "Format-A1-v2",
        "pms_123",
    ]

    for fid in valid_ids:
        spec = ParsingSpec(format_id=fid)
        saved_path = temp_store.save(spec)
        assert saved_path.is_file()
        assert saved_path.parent == temp_store.specs_dir
        assert temp_store.exists(fid) is True

        loaded = temp_store.load(fid)
        assert loaded.format_id == fid

    assert temp_store.list_formats() == sorted(valid_ids)
