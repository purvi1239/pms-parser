"""Alias module for src.specs to support both singular and plural imports."""
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

__all__ = [
    "LocatorType",
    "FieldLocator",
    "TableLocator",
    "ParsingSpec",
    "SpecStore",
    "SpecError",
    "SpecNotFoundError",
    "SpecValidationError",
    "DuplicateSpecError",
    "get_default_specs_dir",
]
