"""Learner module orchestrating parsing specification generation, repair loops, and human feedback.

Workflow:
1. Unknown DocumentIR / Drifted document → Redact PII → Prompt LLM.
2. Execute candidate ParsingSpec against the source DocumentIR using ParsingEngine.
3. Validate ParseResult using Validator (cash reconciliation, portfolio weights, ISIN, etc.).
4. If valid → auto-activate into SpecStore (if enabled) or return as separate proposal.
5. If invalid → enter repair loop: pass validation errors back to LLM (up to max_repairs times).
6. If retries exhausted → reject proposal, set LEARNING_FAILED, and preserve existing specifications untouched.
7. Support learning from human review feedback with strict feedback validation and discrepancy checking.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any

from src.engine.converters import to_decimal
from src.engine.engine import ParsingEngine
from src.fingerprint.extractor import extract_fingerprint
from src.ir.models import DocumentIR
from src.llm_adapter.adapter import LLMAdapter, _extract_json_payload
from src.llm_adapter.exceptions import LLMAdapterError, LLMResponseValidationError
from src.llm_adapter.models import LLMRequest
from src.models.enums import DocumentStatus, StatementType
from src.models.models import ParseResult
from src.redactor.redactor import Redactor
from src.specs.exceptions import SpecValidationError
from src.specs.models import ParsingSpec
from src.specs.store import SpecStore
from src.validator.models import ValidationResult
from src.validator.validator import Validator

from src.learner.feedback_validator import validate_feedback
from src.learner.models import (
    HumanFeedback,
    LearnerConfig,
    LearningAttempt,
    LearningResult,
    ProposalStatus,
)
from src.learner.prompts import (
    build_feedback_prompt,
    build_initial_prompt,
    build_repair_prompt,
)


def _clean_format_id(val: Any) -> str | None:
    """Validate and sanitize a format identifier."""
    if not val or not isinstance(val, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", val.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    if cleaned and re.match(r"^[A-Za-z0-9_-]+(?:[.][A-Za-z0-9_-]+)*$", cleaned):
        return cleaned
    return None


class Learner:
    """Orchestrates deterministic parsing specification learning, validation, and repair."""

    def __init__(
        self,
        llm_adapter: LLMAdapter | None = None,
        spec_store: SpecStore | None = None,
        engine: ParsingEngine | None = None,
        validator: Validator | None = None,
        redactor: Redactor | None = None,
        config: LearnerConfig | None = None,
    ) -> None:
        self.llm_adapter = llm_adapter or LLMAdapter()
        self.spec_store = spec_store or SpecStore()
        self.engine = engine or ParsingEngine()
        self.validator = validator or Validator()
        self.redactor = redactor or Redactor()
        self.config = config or LearnerConfig()

    def learn(
        self,
        doc: DocumentIR,
        format_id: str | None = None,
        statement_type: StatementType = StatementType.PMS,
        auto_activate: bool | None = None,
    ) -> LearningResult:
        """Learn a new parsing specification from an unknown document.

        Args:
            doc: Intermediate representation of the unknown document.
            format_id: Identifier for the new format (defaults to 'format_<timestamp>').
            statement_type: Expected statement type (PMS or AIF).
            auto_activate: Whether to activate valid spec into active SpecStore.
                           Defaults to self.config.auto_activate.

        Returns:
            LearningResult containing status, spec, parse result, and validation findings.
        """
        return self._execute_learning_loop(
            doc=doc,
            format_id=format_id,
            statement_type=statement_type,
            feedback=None,
            base_spec=None,
            auto_activate=auto_activate,
        )

    def learn_from_feedback(
        self,
        doc: DocumentIR,
        feedback: HumanFeedback,
        base_spec: ParsingSpec | None = None,
        auto_activate: bool | None = None,
    ) -> LearningResult:
        """Learn or update a parsing specification from human review feedback.

        Validates feedback before execution. If feedback is incomplete, malformed,
        or inconsistent, safely returns an INVALID_FEEDBACK outcome without
        invoking the LLM or modifying active specifications.

        Args:
            doc: Source document intermediate representation.
            feedback: Structured human review corrections.
            base_spec: Existing base specification to refine (optional).
            auto_activate: Whether to auto-activate the updated spec if valid.

        Returns:
            LearningResult detailing proposal evaluation or rejection.
        """
        # 1. Strictly validate feedback before any LLM dispatch or processing
        is_valid, validation_errors = validate_feedback(feedback, doc=doc)
        if not is_valid:
            return LearningResult(
                format_id=feedback.format_id,
                status=ProposalStatus.INVALID_FEEDBACK,
                document_status=DocumentStatus.NEEDS_REVIEW,
                spec=base_spec,
                is_active=False,
                attempts_count=0,
                error_message="; ".join(validation_errors),
                repair_history=validation_errors,
                attempts=[],
            )

        # 2. Resolve existing base specification if not supplied
        active_base = base_spec
        if active_base is None and self.spec_store.exists(feedback.format_id):
            try:
                active_base = self.spec_store.load(feedback.format_id)
            except Exception:
                active_base = None

        statement_type = (
            active_base.statement_type
            if active_base
            else StatementType.PMS
        )

        return self._execute_learning_loop(
            doc=doc,
            format_id=feedback.format_id,
            statement_type=statement_type,
            feedback=feedback,
            base_spec=active_base,
            auto_activate=auto_activate,
        )

    def propose_spec(
        self,
        doc: DocumentIR,
        feedback: HumanFeedback,
        base_spec: ParsingSpec | None = None,
    ) -> LearningResult:
        """Generate a proposed specification from human feedback without auto-activating.

        Guarantees that active specifications are never overwritten.
        """
        return self.learn_from_feedback(
            doc=doc,
            feedback=feedback,
            base_spec=base_spec,
            auto_activate=False,
        )

    def save_draft_proposal(self, spec: ParsingSpec) -> Path:
        """Save a validated proposal separately with draft prefix to preserve active specs."""
        prefix = self.config.draft_prefix
        if spec.format_id.startswith(prefix):
            draft_id = spec.format_id
        else:
            draft_id = f"{prefix}{spec.format_id}"
        draft_spec = spec.model_copy(deep=True)
        draft_spec.format_id = draft_id
        draft_spec.metadata["is_active"] = False
        draft_spec.metadata["status"] = "proposed"
        if draft_spec.fingerprint is not None:
            draft_spec.fingerprint = draft_spec.fingerprint.model_copy(
                update={"format_id": draft_id}
            )
        return self.spec_store.save(draft_spec, overwrite=True)

    def _execute_learning_loop(
        self,
        doc: DocumentIR,
        format_id: str | None = None,
        statement_type: StatementType = StatementType.PMS,
        feedback: HumanFeedback | None = None,
        base_spec: ParsingSpec | None = None,
        auto_activate: bool | None = None,
    ) -> LearningResult:
        """Core iterative learning and repair loop."""
        should_auto_activate = (
            self.config.auto_activate
            if auto_activate is None
            else auto_activate
        )

        max_attempts = 1 + max(0, self.config.max_repairs)
        attempts: list[LearningAttempt] = []

        last_spec: ParsingSpec | None = base_spec
        last_parse_result: ParseResult | None = None
        last_val_result: ValidationResult | None = None
        last_errors: list[str] = []

        for attempt_idx in range(1, max_attempts + 1):
            # 1. Build prompt
            prompt_fid = (
                last_spec.format_id
                if (last_spec and last_spec.format_id)
                else (_clean_format_id(format_id) or "format_unknown")
            )
            if attempt_idx == 1:
                if feedback is not None:
                    sys_prompt, user_prompt = build_feedback_prompt(
                        doc=doc,
                        format_id=prompt_fid,
                        feedback=feedback,
                        base_spec_json=base_spec.to_json() if base_spec else None,
                        redactor=self.redactor,
                    )
                else:
                    sys_prompt, user_prompt = build_initial_prompt(
                        doc=doc,
                        format_id=prompt_fid,
                        statement_type=statement_type,
                        redactor=self.redactor,
                    )
            else:
                # Repair iteration
                prev_json = last_spec.to_json() if last_spec else "{}"
                prev_warns = last_parse_result.warnings if last_parse_result else []
                sys_prompt, user_prompt = build_repair_prompt(
                    doc=doc,
                    format_id=prompt_fid,
                    previous_spec_json=prev_json,
                    errors=last_errors,
                    warnings=prev_warns,
                    redactor=self.redactor,
                    repair_attempt=attempt_idx - 1,
                )

            # 2. Call LLM adapter
            request = LLMRequest(
                prompt=user_prompt,
                system_prompt=sys_prompt,
                temperature=self.config.temperature,
                model=self.config.model,
                metadata={"format_id": prompt_fid, "attempt": attempt_idx},
            )

            try:
                response = self.llm_adapter.complete_request(request)
            except LLMAdapterError as exc:
                err_msg = f"LLM adapter failure on attempt {attempt_idx}: {exc}"
                attempts.append(
                    LearningAttempt(
                        attempt_number=attempt_idx,
                        errors=[err_msg],
                        success=False,
                    )
                )
                last_errors = [err_msg]
                continue

            # 3. Parse LLM response into candidate ParsingSpec
            candidate_spec: ParsingSpec | None = None
            schema_errors: list[str] = []

            raw_dict = response.structured
            if raw_dict is None and response.content:
                try:
                    raw_dict = _extract_json_payload(response.content)
                except LLMResponseValidationError as err:
                    schema_errors.append(f"Response is not valid JSON: {err}")

            if raw_dict is not None:
                llm_candidate_fid = _clean_format_id(raw_dict.get("format_id"))
                requested_clean_fid = _clean_format_id(format_id)

                if llm_candidate_fid:
                    resolved_fid = llm_candidate_fid
                elif requested_clean_fid:
                    resolved_fid = requested_clean_fid
                else:
                    resolved_fid = f"format_{int(time.time())}"

                raw_dict["format_id"] = resolved_fid

                if not raw_dict.get("statement_type"):
                    raw_dict["statement_type"] = statement_type.value

                try:
                    candidate_spec = ParsingSpec.from_dict(raw_dict)
                except (ValueError, SpecValidationError, Exception) as exc:
                    schema_errors.append(f"ParsingSpec validation failed: {exc}")

            if candidate_spec is None:
                last_errors = schema_errors or ["Failed to parse candidate specification."]
                attempts.append(
                    LearningAttempt(
                        attempt_number=attempt_idx,
                        raw_response=response.content,
                        errors=last_errors,
                        success=False,
                    )
                )
                continue

            # Attach extracted fingerprint to the candidate spec
            try:
                candidate_spec.fingerprint = extract_fingerprint(doc, format_id=candidate_spec.format_id)
                candidate_spec.fingerprint.format_id = candidate_spec.format_id
            except Exception:
                pass

            last_spec = candidate_spec

            # 4. Test candidate spec against source document with ParsingEngine
            parse_result = self.engine.parse(doc, candidate_spec)
            last_parse_result = parse_result

            # 5. Run Validator checks
            val_result = self.validator.validate(parse_result)
            last_val_result = val_result

            attempt_errors: list[str] = []

            # Check validator errors
            if val_result.errors:
                attempt_errors.extend(f"[{f.check}] {f.message}" for f in val_result.errors)

            # Learner-specific check: verify proposed declared locators actually resolve against the training document
            locator_errors = self._verify_declared_locators(doc, candidate_spec, parse_result)
            if locator_errors:
                attempt_errors.extend(locator_errors)

            # If feedback provided, verify that extracted data satisfies reviewer corrections
            if feedback is not None:
                discrepancies = self._verify_feedback_match(parse_result, feedback)
                attempt_errors.extend(discrepancies)

            last_errors = attempt_errors
            is_attempt_successful = (len(attempt_errors) == 0)

            attempts.append(
                LearningAttempt(
                    attempt_number=attempt_idx,
                    raw_response=raw_dict or response.content,
                    spec=candidate_spec,
                    parse_result=parse_result,
                    validation_result=val_result,
                    errors=attempt_errors,
                    success=is_attempt_successful,
                )
            )

            # 6. Evaluation outcome
            if is_attempt_successful:
                if should_auto_activate:
                    # Save to active store
                    self.spec_store.save(candidate_spec, overwrite=True)
                    proposal_status = ProposalStatus.ACCEPTED
                    doc_status = val_result.final_status
                    is_active = True
                else:
                    # Mark as proposed draft; DO NOT overwrite active spec
                    candidate_spec.metadata["status"] = "proposed"
                    candidate_spec.metadata["is_active"] = False
                    self.save_draft_proposal(candidate_spec)
                    proposal_status = ProposalStatus.PROPOSED
                    doc_status = DocumentStatus.NEEDS_REVIEW
                    is_active = False

                return LearningResult(
                    format_id=candidate_spec.format_id,
                    status=proposal_status,
                    document_status=doc_status,
                    spec=candidate_spec,
                    is_active=is_active,
                    attempts_count=attempt_idx,
                    parse_result=parse_result,
                    validation_result=val_result,
                    error_message=None,
                    repair_history=[e for att in attempts[:-1] for e in att.errors],
                    attempts=attempts,
                )

        # Retries exhausted without satisfying all checks
        all_repair_history = [e for att in attempts for e in att.errors]
        failed_fid = (
            last_spec.format_id
            if (last_spec and last_spec.format_id)
            else (_clean_format_id(format_id) or f"format_{int(time.time())}")
        )
        return LearningResult(
            format_id=failed_fid,
            status=ProposalStatus.REJECTED,
            document_status=DocumentStatus.LEARNING_FAILED,
            spec=last_spec,
            is_active=False,
            attempts_count=len(attempts),
            parse_result=last_parse_result,
            validation_result=last_val_result,
            error_message="; ".join(last_errors) if last_errors else "Learning repair retries exhausted.",
            repair_history=all_repair_history,
            attempts=attempts,
        )

    def _verify_feedback_match(
        self,
        parse_result: ParseResult,
        feedback: HumanFeedback,
    ) -> list[str]:
        """Verify that parsed results accurately reflect the human review corrections."""
        discrepancies: list[str] = []

        # Check metadata corrections
        for k, expected in feedback.corrected_metadata.items():
            if expected is None:
                continue
            actual = None
            if parse_result.statement:
                actual = getattr(parse_result.statement, k, None)

            if actual is None:
                discrepancies.append(
                    f"Metadata correction '{k}': expected '{expected}', but was not extracted."
                )
            elif str(actual).strip().lower() != str(expected).strip().lower():
                discrepancies.append(
                    f"Metadata correction '{k}': expected '{expected}', extracted '{actual}'."
                )

        # Check summary corrections
        for k, expected in feedback.corrected_summary.items():
            if expected is None:
                continue
            if parse_result.summary is None:
                discrepancies.append(
                    f"Summary correction '{k}': expected {expected}, but summary was not extracted."
                )
                continue

            actual = getattr(parse_result.summary, k, None)
            if actual is None:
                discrepancies.append(
                    f"Summary correction '{k}': expected {expected}, but field was None."
                )
            else:
                try:
                    exp_dec = to_decimal(expected)
                    act_dec = to_decimal(actual)
                    if exp_dec != act_dec:
                        discrepancies.append(
                            f"Summary correction '{k}': expected {exp_dec}, extracted {act_dec}."
                        )
                except Exception:
                    if str(actual) != str(expected):
                        discrepancies.append(
                            f"Summary correction '{k}': expected '{expected}', extracted '{actual}'."
                        )

        # Check holdings row count if supplied
        if feedback.corrected_holdings:
            if len(parse_result.holdings) != len(feedback.corrected_holdings):
                discrepancies.append(
                    f"Holdings count mismatch: expected {len(feedback.corrected_holdings)}, extracted {len(parse_result.holdings)}."
                )

        # Check transactions row count if supplied
        if feedback.corrected_transactions:
            if len(parse_result.transactions) != len(feedback.corrected_transactions):
                discrepancies.append(
                    f"Transactions count mismatch: expected {len(feedback.corrected_transactions)}, extracted {len(parse_result.transactions)}."
                )

        return discrepancies

    def _verify_declared_locators(
        self,
        doc: DocumentIR,
        spec: ParsingSpec,
        parse_result: ParseResult,
    ) -> list[str]:
        """Verify that proposed declared locators actually resolve against the training document.

        A candidate specification proposed for a training document should not contain
        locators that fail to extract values from that document.
        """
        errors: list[str] = []

        # 1. Summary fields: check that declared summary locators extracted a value
        for field_name, locator in spec.summary_fields.items():
            val = None
            if parse_result.summary:
                val = getattr(parse_result.summary, field_name, None)
            if val is None and locator.default is None:
                desc = (
                    f"anchor '{locator.anchor}'"
                    if locator.anchor
                    else (
                        f"pattern '{locator.pattern}'"
                        if locator.pattern
                        else f"type '{locator.locator_type.value}'"
                    )
                )
                errors.append(
                    f"Proposed locator for summary field '{field_name}' ({desc}) failed to resolve against the document."
                )

        # 2. Metadata fields: check that declared metadata locators extracted a value
        for field_name, locator in spec.metadata_fields.items():
            val = None
            if parse_result.statement:
                val = getattr(parse_result.statement, field_name, None)
            if val is None and locator.default is None:
                desc = (
                    f"anchor '{locator.anchor}'"
                    if locator.anchor
                    else (
                        f"pattern '{locator.pattern}'"
                        if locator.pattern
                        else f"type '{locator.locator_type.value}'"
                    )
                )
                errors.append(
                    f"Proposed locator for metadata field '{field_name}' ({desc}) failed to resolve against the document."
                )

        # 3. Holdings table: if declared, verify that rows were extracted (except AIF with empty holdings)
        if spec.holdings_table is not None:
            if not parse_result.holdings and spec.statement_type != StatementType.AIF:
                desc = f"anchor '{spec.holdings_table.anchor}'" if spec.holdings_table.anchor else "table locator"
                errors.append(
                    f"Proposed holdings table locator ({desc}) failed to extract any holdings rows from the document."
                )

        # 4. Check for unlocated anchors reported in parser warnings
        for warn in parse_result.warnings:
            if "not found in document" in warn.lower() or "could not be located" in warn.lower():
                if not any(warn in e for e in errors):
                    errors.append(warn)

        return errors
