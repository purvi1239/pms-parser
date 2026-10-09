"""Parsing specifications and SpecStore module for deterministic document extraction."""
from src.specs.exceptions import (
    DuplicateSpecError,
    SpecError,
    SpecNotFoundError,
    SpecValidationError,
)
from src.specs.models import (
    FieldLocator,
    LocatorType,
    ParsingSpec,
    TableLocator,
)
from src.specs.store import SpecStore, get_default_specs_dir

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
