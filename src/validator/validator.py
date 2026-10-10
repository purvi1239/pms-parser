"""Validator: runs all applicable checks against a ParseResult and returns a ValidationResult.

Usage
-----
    from src.validator import Validator, ValidatorConfig

    config = ValidatorConfig(cash_tolerance=Decimal("2.00"))
    validator = Validator(config)
    result = validator.validate(parse_result)

    if result.passed:
        print("Validation passed")
    else:
        for finding in result.errors:
            print(f"ERROR [{finding.check}]: {finding.message}")

The Validator:
- Runs checks in a fixed order (see checks.ALL_CHECKS).
- Accumulates all findings before computing the final status.
- Never mutates the input ParseResult.
- Updates final_status:
    NEEDS_REVIEW          → any ERROR finding
    PARSED_WITH_WARNINGS  → any WARNING finding, no errors
    PARSED                → no findings
"""
from __future__ import annotations

from src.models.enums import DocumentStatus
from src.models.models import ParseResult
from src.validator.checks import ALL_CHECKS
from src.validator.config import ValidatorConfig
from src.validator.models import ValidationResult, ValidationSeverity


class Validator:
    """Runs all configured validation checks against a ParseResult."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        """
        Args:
            config: Tolerance configuration.  Uses project-plan defaults when None.
        """
        self.config = config or ValidatorConfig()

    def validate(self, result: ParseResult) -> ValidationResult:
        """Execute all applicable checks and return a structured ValidationResult.

        Args:
            result: The ParseResult produced by the parsing engine.  Not modified.

        Returns:
            ValidationResult with findings, check lists, and recommended final_status.
        """
        all_findings = []
        checks_run: list[str] = []
        checks_skipped: list[str] = []

        for check_fn in ALL_CHECKS:
            check_name = check_fn.__name__
            try:
                findings, was_run = check_fn(result, self.config)
            except Exception as exc:  # pragma: no cover — defensive only
                # A bug in a check must not crash the entire validation pipeline.
                findings = []
                was_run = False
                from src.validator.models import ValidationFinding
                findings.append(
                    ValidationFinding(
                        check=check_name,
                        severity=ValidationSeverity.WARNING,
                        message=f"Check raised an unexpected error and was aborted: {exc}",
                    )
                )

            all_findings.extend(findings)
            if was_run:
                checks_run.append(check_name)
            else:
                checks_skipped.append(check_name)

        # Determine recommended final status
        has_errors = any(f.severity == ValidationSeverity.ERROR for f in all_findings)
        has_warnings = any(f.severity == ValidationSeverity.WARNING for f in all_findings)

        if has_errors:
            final_status = DocumentStatus.NEEDS_REVIEW
        elif has_warnings:
            final_status = DocumentStatus.PARSED_WITH_WARNINGS
        else:
            final_status = DocumentStatus.PARSED

        return ValidationResult(
            findings=all_findings,
            checks_run=checks_run,
            checks_skipped=checks_skipped,
            final_status=final_status,
        )


def validate_result(
    result: ParseResult,
    config: ValidatorConfig | None = None,
) -> ValidationResult:
    """Convenience function: validate a ParseResult with default or custom config.

    Args:
        result:  ParseResult to validate.
        config:  Optional tolerance config; defaults applied when None.

    Returns:
        ValidationResult.
    """
    return Validator(config).validate(result)
