"""Domain models for deterministic parsing specifications and locators."""
from __future__ import annotations

from enum import Enum
import json
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fingerprint.models import FormatFingerprint
from src.ir.models import BBox
from src.models.enums import StatementType


class LocatorType(str, Enum):
    """Supported locator strategies for identifying fields and tables in a document."""

    TABLE_AFTER_ANCHOR = "table_after_anchor"
    VALUE_RIGHT_OF_ANCHOR = "value_right_of_anchor"
    VALUE_BELOW_ANCHOR = "value_below_anchor"
    VALUE_MATCHING_PATTERN = "value_matching_pattern"
    BOUNDING_BOX = "bounding_box"
    TEXT_SECTION = "text_section"

    @classmethod
    def _missing_(cls, value: Any) -> Any:
        if isinstance(value, str):
            val_clean = value.strip().lower()
            for member in cls:
                if member.value.lower() == val_clean or member.name.lower() == val_clean:
                    return member
        return None


def _coerce_bbox(value: Any) -> BBox | None:
    """Helper to convert tuples, lists, or dicts into a BBox instance."""
    if value is None:
        return None
    if isinstance(value, BBox):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return BBox(x0=float(value[0]), top=float(value[1]), x1=float(value[2]), bottom=float(value[3]))
    if isinstance(value, dict):
        return BBox(**value)
    raise ValueError(f"Invalid bbox format: {value}. Expected BBox, dict, or (x0, top, x1, bottom) 4-tuple.")


class FieldLocator(BaseModel):
    """Specification for locating a single scalar field value in an extracted document."""

    model_config = ConfigDict(validate_assignment=True)

    locator_type: LocatorType = LocatorType.VALUE_RIGHT_OF_ANCHOR
    anchor: str | None = None
    pattern: str | None = None
    page_number: int | None = None  # 1-indexed; None indicates searching across all pages
    bbox: BBox | None = None        # Used primarily for BOUNDING_BOX locator type
    fallback_bbox: BBox | None = None  # Fallback bounding box if anchor-relative search fails
    offset_x: float | None = None   # Horizontal distance/tolerance limit in points
    offset_y: float | None = None   # Vertical distance/tolerance limit in points
    data_type: str | None = None    # Target data type hint (e.g. 'str', 'decimal', 'date')
    required: bool = False
    default: Any = None

    @field_validator("bbox", "fallback_bbox", mode="before")
    @classmethod
    def _validate_bbox(cls, v: Any) -> BBox | None:
        return _coerce_bbox(v)

    @field_validator("anchor", "pattern", mode="before")
    @classmethod
    def _strip_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_stripped = v.strip()
            return v_stripped if v_stripped else None
        return v

    @model_validator(mode="after")
    def _validate_locator_config(self) -> FieldLocator:
        if self.locator_type == LocatorType.BOUNDING_BOX:
            if self.bbox is None and self.fallback_bbox is not None:
                self.bbox = self.fallback_bbox
            if self.bbox is None:
                raise ValueError("Bounding-box field locator requires a valid 'bbox'.")
        elif self.locator_type in (LocatorType.VALUE_RIGHT_OF_ANCHOR, LocatorType.VALUE_BELOW_ANCHOR):
            if not self.anchor:
                raise ValueError(
                    f"Anchor-relative locator '{self.locator_type.value}' requires a non-empty 'anchor' string."
                )
        elif self.locator_type == LocatorType.VALUE_MATCHING_PATTERN:
            if not self.pattern:
                raise ValueError("Locator 'value_matching_pattern' requires a non-empty regex 'pattern'.")
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern '{self.pattern}': {e}") from e

        return self

    @property
    def is_anchor_relative(self) -> bool:
        """Return True if this locator uses an anchor-relative strategy."""
        return self.locator_type in (
            LocatorType.VALUE_RIGHT_OF_ANCHOR,
            LocatorType.VALUE_BELOW_ANCHOR,
            LocatorType.VALUE_MATCHING_PATTERN,
        )

    @property
    def is_fallback(self) -> bool:
        """Return True if this locator is an absolute bounding-box locator or contains a fallback bbox."""
        return self.locator_type == LocatorType.BOUNDING_BOX or self.fallback_bbox is not None


class TableLocator(BaseModel):
    """Specification for locating and extracting a structured tabular region."""

    model_config = ConfigDict(validate_assignment=True)

    locator_type: LocatorType = LocatorType.TABLE_AFTER_ANCHOR
    anchor: str | None = None
    page_number: int | None = None
    bbox: BBox | None = None
    fallback_bbox: BBox | None = None
    header_keywords: list[str] = Field(default_factory=list)
    column_mapping: dict[str, str] = Field(default_factory=dict)
    stop_anchor: str | None = None
    multi_page: bool = False
    repeat_headers: bool = False

    @field_validator("bbox", "fallback_bbox", mode="before")
    @classmethod
    def _validate_bbox(cls, v: Any) -> BBox | None:
        return _coerce_bbox(v)

    @field_validator("anchor", "stop_anchor", mode="before")
    @classmethod
    def _strip_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_stripped = v.strip()
            return v_stripped if v_stripped else None
        return v

    @model_validator(mode="after")
    def _validate_locator_config(self) -> TableLocator:
        if self.locator_type == LocatorType.BOUNDING_BOX:
            if self.bbox is None and self.fallback_bbox is not None:
                self.bbox = self.fallback_bbox
            if self.bbox is None:
                raise ValueError("Bounding-box table locator requires a valid 'bbox'.")
        elif self.locator_type == LocatorType.TABLE_AFTER_ANCHOR:
            if not self.anchor and not self.header_keywords:
                raise ValueError(
                    "Locator 'table_after_anchor' requires either an 'anchor' string or 'header_keywords'."
                )
        return self

    @property
    def is_anchor_relative(self) -> bool:
        """Return True if this locator uses an anchor-relative strategy."""
        return self.locator_type == LocatorType.TABLE_AFTER_ANCHOR

    @property
    def is_fallback(self) -> bool:
        """Return True if this locator is an absolute bounding-box locator or contains a fallback bbox."""
        return self.locator_type == LocatorType.BOUNDING_BOX or self.fallback_bbox is not None


class TextSectionLocator(BaseModel):
    """Locator for extracting holdings or transactions from plain-text document sections.

    Use this when the document does not contain usable PDF table objects and the
    data is formatted as structured plain text lines separated by consistent
    whitespace or delimiter characters.

    Fields
    ------
    anchor:
        Case-insensitive phrase that marks the **start** of the section.
        Data extraction begins on the line immediately following this phrase.
        If None, extraction starts from the beginning of the document (or page).
    stop_anchor:
        Case-insensitive phrase that marks the **end** of the section
        (exclusive).  Extraction stops when this phrase is encountered.
    row_pattern:
        A Python regex with **named groups** (``(?P<name>...)``) that is
        applied to each line in the sliced section.  Only lines that
        produce a match are included; unmatched lines (headers, totals,
        blank separators) are silently skipped.
    column_mapping:
        Maps model field names to the named-group names produced by
        *row_pattern*.  Supported keys for holdings:
        ``security_name``, ``isin``, ``quantity``, ``price``, ``cost``,
        ``market_value``, ``weight``.
        Supported keys for transactions:
        ``date``, ``type``, ``security``, ``quantity``, ``price``, ``amount``.
    page_number:
        Optional 1-indexed page to begin searching from.
    skip_patterns:
        Optional list of regex strings.  Any line matching one of these is
        excluded even if it also matches *row_pattern* (useful for skipping
        totals/summary rows).
    """

    model_config = ConfigDict(validate_assignment=True)

    locator_type: LocatorType = LocatorType.TEXT_SECTION
    anchor: str | None = None
    stop_anchor: str | None = None
    row_pattern: str = ""
    column_mapping: dict[str, str] = Field(default_factory=dict)
    page_number: int | None = None
    skip_patterns: list[str] = Field(default_factory=list)

    @field_validator("anchor", "stop_anchor", mode="before")
    @classmethod
    def _strip_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_stripped = v.strip()
            return v_stripped if v_stripped else None
        return v

    @model_validator(mode="after")
    def _validate_locator_config(self) -> TextSectionLocator:
        if not self.row_pattern:
            raise ValueError("TextSectionLocator requires a non-empty 'row_pattern' regex.")
        try:
            re.compile(self.row_pattern)
        except re.error as e:
            raise ValueError(f"Invalid row_pattern regex '{self.row_pattern}': {e}") from e
        for sp in self.skip_patterns:
            try:
                re.compile(sp)
            except re.error as e:
                raise ValueError(f"Invalid skip_pattern regex '{sp}': {e}") from e
        return self


class ParsingSpec(BaseModel):
    """Top-level deterministic parsing specification for a specific document format."""

    model_config = ConfigDict(validate_assignment=True)

    format_id: str
    version: str = "1.0.0"
    statement_type: StatementType = StatementType.PMS
    description: str | None = None
    fingerprint: FormatFingerprint | None = None
    metadata_fields: dict[str, FieldLocator] = Field(default_factory=dict)
    summary_fields: dict[str, FieldLocator] = Field(default_factory=dict)
    holdings_table: TableLocator | None = None
    transactions_table: TableLocator | None = None
    # Text-section extraction paths (used when no usable PDF table exists)
    text_holdings: TextSectionLocator | None = None
    text_transactions: TextSectionLocator | None = None
    custom_tables: dict[str, TableLocator] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("format_id")
    @classmethod
    def _validate_format_id(cls, v: str) -> str:
        v_clean = v.strip()
        if not v_clean:
            raise ValueError("format_id must not be empty.")
        return v_clean

    def to_dict(self) -> dict[str, Any]:
        """Convert specification to a plain JSON-compatible dictionary."""
        return json.loads(self.model_dump_json())

    def to_json(self, indent: int = 2) -> str:
        """Serialize specification to formatted JSON string."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsingSpec:
        """Construct specification from dictionary."""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> ParsingSpec:
        """Construct specification from JSON string."""
        return cls.model_validate_json(json_str)
