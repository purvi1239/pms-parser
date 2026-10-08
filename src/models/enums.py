from enum import Enum
from typing import Any


class StatementType(str, Enum):
    PMS = "PMS"
    AIF = "AIF"

    @classmethod
    def _missing_(cls, value: Any) -> Any:
        if isinstance(value, str):
            val_upper = value.strip().upper()
            for member in cls:
                if member.name.upper() == val_upper or member.value.upper() == val_upper:
                    return member
        return None


class TransactionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    FEE = "fee"
    INFLOW = "inflow"
    OUTFLOW = "outflow"

    @classmethod
    def _missing_(cls, value: Any) -> Any:
        if isinstance(value, str):
            val_upper = value.strip().upper()
            for member in cls:
                if member.name.upper() == val_upper or member.value.upper() == val_upper:
                    return member
        return None


class DocumentStatus(str, Enum):
    DUPLICATE = "DUPLICATE"
    NEEDS_PASSWORD = "NEEDS_PASSWORD"
    SKIPPED = "SKIPPED"
    MAYBE = "MAYBE"
    CLASSIFIED = "CLASSIFIED"
    PARSED = "PARSED"
    PARSED_WITH_WARNINGS = "PARSED_WITH_WARNINGS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    DRIFT_SUSPECTED = "DRIFT_SUSPECTED"
    LEARNING_FAILED = "LEARNING_FAILED"
    FAILED = "FAILED"

    @classmethod
    def _missing_(cls, value: Any) -> Any:
        if isinstance(value, str):
            val_upper = value.strip().upper()
            for member in cls:
                if member.name.upper() == val_upper or member.value.upper() == val_upper:
                    return member
        return None
