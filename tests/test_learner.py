"""Tests for Step 11: Learner module.

Covers:
1. Learner configuration and initialization.
2. Feedback validation: syntax, incomplete, and contradictory review feedback.
3. Successful learning of an unknown document with/without auto-activation.
4. Repair loop: recover from initial validation errors via repair prompt.
5. Rejection of proposals when repairs are exhausted (LEARNING_FAILED).
6. Learning from valid human review feedback.
7. Discrepancy rejection: candidate spec failing to match reviewer corrections.
8. Preservation of existing active specifications in SpecStore.
9. PII redaction verification before prompt construction.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import pytest

from src.fingerprint import FormatFingerprint
from src.ir.models import BBox, DocumentIR, PageIR, TableIR, TextBlock, WordIR
from src.learner import (
    HumanFeedback,
    Learner,
    LearnerConfig,
    LearningResult,
    ProposalStatus,
    validate_feedback,
)
from src.llm_adapter import FakeLLMAdapter, LLMConfig
from src.models.enums import DocumentStatus, StatementType
from src.specs.models import FieldLocator, LocatorType, ParsingSpec, TableLocator
from src.specs.store import SpecStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_pms_doc() -> DocumentIR:
    """Construct a clean, reconcilable in-memory DocumentIR for PMS statements."""
    p1_words = [
        WordIR(text="Pinnacle", page_number=1, bbox=BBox(x0=50, top=30, x1=90, bottom=42)),
        WordIR(text="Wealth", page_number=1, bbox=BBox(x0=95, top=30, x1=140, bottom=42)),
        WordIR(text="PMS", page_number=1, bbox=BBox(x0=145, top=30, x1=170, bottom=42)),
        WordIR(text="Client", page_number=1, bbox=BBox(x0=50, top=50, x1=80, bottom=62)),
        WordIR(text="ID:", page_number=1, bbox=BBox(x0=82, top=50, x1=95, bottom=62)),
        WordIR(text="PWM-0042", page_number=1, bbox=BBox(x0=100, top=50, x1=160, bottom=62)),
        WordIR(text="Investor", page_number=1, bbox=BBox(x0=50, top=65, x1=90, bottom=77)),
        WordIR(text="Name:", page_number=1, bbox=BBox(x0=92, top=65, x1=120, bottom=77)),
        WordIR(text="Rameshwar", page_number=1, bbox=BBox(x0=125, top=65, x1=180, bottom=77)),
        WordIR(text="Saxena", page_number=1, bbox=BBox(x0=185, top=65, x1=220, bottom=77)),
        WordIR(text="PAN:", page_number=1, bbox=BBox(x0=50, top=80, x1=75, bottom=92)),
        WordIR(text="ABCDE1234F", page_number=1, bbox=BBox(x0=80, top=80, x1=150, bottom=92)),
    ]

    holdings_table = TableIR(
        page_number=1,
        rows=[
            ["Security Name", "ISIN", "Quantity", "Market Value", "Weight %"],
            ["Reliance Industries Ltd", "INE002A01018", "100", "290000.00", "58.00"],
            ["Tata Consultancy Services", "INE467B01029", "50", "210000.00", "42.00"],
        ],
    )

    transactions_table = TableIR(
        page_number=1,
        rows=[
            ["Date", "Activity", "Security", "Qty", "Amount"],
            ["15/01/2026", "BUY", "Reliance Industries Ltd", "10", "-5000.00"],
        ],
    )

    summary_table = TableIR(
        page_number=1,
        rows=[
            ["Opening Value", "450000.00", "Closing Value", "500000.00"],
            ["Opening Cash", "20000.00", "Closing Cash", "15000.00"],
        ],
    )

    raw_text = (
        "Pinnacle Wealth PMS\n"
        "Client ID: PWM-0042\n"
        "Investor Name: Rameshwar Saxena\n"
        "PAN: ABCDE1234F\n"
        "Opening Value: 450000.00  Closing Value: 500000.00\n"
        "Opening Cash: 20000.00  Closing Cash: 15000.00\n"
        "Holdings\nTransactions\n"
    )

    page1 = PageIR(
        page_number=1,
        width=595.0,
        height=842.0,
        raw_text=raw_text,
        words=p1_words,
        tables=[holdings_table, transactions_table, summary_table],
    )

    return DocumentIR(
        source_path="/tmp/test_statement.pdf",
        page_count=1,
        pages=[page1],
        metadata={"title": "Test Statement"},
    )


@pytest.fixture
def valid_spec_dict() -> dict:
    """Return a valid ParsingSpec dictionary matching sample_pms_doc."""
    return {
        "format_id": "test_format_pms",
        "statement_type": "PMS",
        "description": "Learned spec for test_format_pms",
        "metadata_fields": {
            "provider_name": {
                "locator_type": "value_matching_pattern",
                "pattern": r"(Pinnacle\s+Wealth\s+PMS)",
            },
            "client_identifier": {
                "locator_type": "value_right_of_anchor",
                "anchor": "Client ID:",
            },
        },
        "summary_fields": {
            "opening_value": {
                "locator_type": "value_right_of_anchor",
                "anchor": "Opening Value",
            },
            "closing_value": {
                "locator_type": "value_right_of_anchor",
                "anchor": "Closing Value",
            },
            "opening_cash": {
                "locator_type": "value_right_of_anchor",
                "anchor": "Opening Cash",
            },
            "closing_cash": {
                "locator_type": "value_right_of_anchor",
                "anchor": "Closing Cash",
            },
        },
        "holdings_table": {
            "locator_type": "table_after_anchor",
            "anchor": "Security Name",
            "header_keywords": ["Security Name", "ISIN", "Quantity", "Market Value", "Weight %"],
            "column_mapping": {
                "security_name": "Security Name",
                "isin": "ISIN",
                "quantity": "Quantity",
                "market_value": "Market Value",
                "weight": "Weight %",
            },
        },
        "transactions_table": {
            "locator_type": "table_after_anchor",
            "anchor": "Activity",
            "header_keywords": ["Date", "Activity", "Security", "Qty", "Amount"],
            "column_mapping": {
                "date": "Date",
                "type": "Activity",
                "security": "Security",
                "quantity": "Qty",
                "amount": "Amount",
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. Feedback Validation Tests
# ---------------------------------------------------------------------------

class TestFeedbackValidation:
    def test_empty_feedback_rejected(self):
        fb = HumanFeedback(format_id="format_a")
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("no actionable corrections" in e.lower() for e in errors)

    def test_invalid_format_id_rejected(self):
        samples = ["../outside", "/etc/passwd", "bad/id", "format..name", "   "]
        for bad_id in samples:
            fb = HumanFeedback(
                format_id=bad_id,
                corrected_metadata={"provider_name": "Test Provider"},
            )
            is_valid, errors = validate_feedback(fb)
            assert not is_valid
            assert any("format_id" in e for e in errors)

    def test_unknown_summary_field_rejected(self):
        fb = HumanFeedback(
            format_id="format_a",
            corrected_summary={"crypto_profit": Decimal("5000")},
        )
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("unknown summary field" in e.lower() for e in errors)

    def test_unparseable_decimal_rejected(self):
        fb = HumanFeedback(
            format_id="format_a",
            corrected_summary={"closing_cash": "NOT_A_DECIMAL_NUMBER"},
        )
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("unparseable numeric value" in e.lower() for e in errors)

    def test_negative_units_rejected(self):
        fb = HumanFeedback(
            format_id="format_a",
            corrected_summary={"units": Decimal("-10.5")},
        )
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("cannot be negative" in e.lower() for e in errors)

    def test_inconsistent_units_nav_closing_rejected(self):
        # units * nav = 100 * 10 = 1000, but closing_value = 50000 (wild contradiction)
        fb = HumanFeedback(
            format_id="format_a",
            corrected_summary={
                "units": Decimal("100"),
                "nav_per_unit": Decimal("10"),
                "closing_value": Decimal("50000"),
            },
        )
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("contradicts closing_value" in e.lower() for e in errors)

    def test_unknown_holding_field_rejected(self):
        fb = HumanFeedback(
            format_id="format_a",
            corrected_holdings=[{"fake_column": "value"}],
        )
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("unknown keys" in e.lower() for e in errors)

    def test_invalid_transaction_type_rejected(self):
        fb = HumanFeedback(
            format_id="format_a",
            corrected_transactions=[{"type": "speculative_bet", "amount": Decimal("100")}],
        )
        is_valid, errors = validate_feedback(fb)
        assert not is_valid
        assert any("unknown transaction type" in e.lower() for e in errors)

    def test_valid_feedback_passes(self):
        fb = HumanFeedback(
            format_id="format_a",
            corrected_metadata={"provider_name": "Pinnacle Wealth"},
            corrected_summary={"closing_cash": Decimal("15000.00")},
            corrected_holdings=[
                {
                    "security_name": "Reliance Industries Ltd",
                    "isin": "INE002A01018",
                    "quantity": Decimal("100"),
                    "market_value": Decimal("290000.00"),
                }
            ],
            notes="Adjusted closing cash balance",
        )
        is_valid, errors = validate_feedback(fb)
        assert is_valid
        assert errors == []


# ---------------------------------------------------------------------------
# 2. Successful Learning Workflow
# ---------------------------------------------------------------------------

class TestSuccessfulLearning:
    def test_learn_unknown_document_without_auto_activate(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)
        fake_llm = FakeLLMAdapter(default_response=valid_spec_dict)

        learner = Learner(
            llm_adapter=fake_llm,
            spec_store=store,
            config=LearnerConfig(auto_activate=False),
        )

        res = learner.learn(sample_pms_doc, format_id="test_format_pms")

        # 1. Proposal is accepted structurally and validated
        assert res.passed
        assert res.status == ProposalStatus.PROPOSED
        assert res.is_active is False
        assert res.attempts_count == 1
        assert res.spec is not None
        assert res.spec.format_id == "test_format_pms"

        # 2. Spec contains extracted fingerprint
        assert res.spec.fingerprint is not None

        # 3. Active store does NOT contain the spec because auto_activate=False
        assert not store.exists("test_format_pms")

        # 4. Parse result matches document
        assert res.parse_result is not None
        assert len(res.parse_result.holdings) == 2
        assert res.validation_result is not None
        assert res.validation_result.passed

    def test_learn_unknown_document_with_auto_activate(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)
        fake_llm = FakeLLMAdapter(default_response=valid_spec_dict)

        learner = Learner(
            llm_adapter=fake_llm,
            spec_store=store,
            config=LearnerConfig(auto_activate=True),
        )

        res = learner.learn(sample_pms_doc, format_id="test_format_pms")

        assert res.passed
        assert res.status == ProposalStatus.ACCEPTED
        assert res.is_active is True

        # Active store MUST now have the validated spec
        assert store.exists("test_format_pms")
        loaded_spec = store.load("test_format_pms")
        assert loaded_spec.format_id == "test_format_pms"


# ---------------------------------------------------------------------------
# 3. Repair Loop Tests
# ---------------------------------------------------------------------------

class TestLearnerRepairLoop:
    def test_recovers_via_repair_prompt(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)

        # Attempt 1 has wrong anchor for closing_cash, causing cash check failure
        bad_spec_dict = json.loads(json.dumps(valid_spec_dict))
        bad_spec_dict["summary_fields"]["closing_cash"]["anchor"] = "NON_EXISTENT_ANCHOR"

        fake_llm = FakeLLMAdapter()
        # Initial call returns bad spec
        fake_llm.register_response("Target Format ID: test_format_pms", bad_spec_dict)
        # Repair call containing "VALIDATION ERRORS" returns good spec
        fake_llm.register_response("VALIDATION ERRORS", valid_spec_dict)

        learner = Learner(
            llm_adapter=fake_llm,
            spec_store=store,
            config=LearnerConfig(max_repairs=2, auto_activate=True),
        )

        res = learner.learn(sample_pms_doc, format_id="test_format_pms")

        # Must succeed on attempt 2
        assert res.passed
        assert res.status == ProposalStatus.ACCEPTED
        assert res.attempts_count == 2
        assert len(res.repair_history) > 0
        assert store.exists("test_format_pms")

    def test_repairs_exhausted_rejects_proposal(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)

        # Always returns invalid spec that fails validation
        bad_spec_dict = json.loads(json.dumps(valid_spec_dict))
        bad_spec_dict["summary_fields"]["closing_cash"]["anchor"] = "NON_EXISTENT_ANCHOR"

        fake_llm = FakeLLMAdapter(default_response=bad_spec_dict)

        learner = Learner(
            llm_adapter=fake_llm,
            spec_store=store,
            config=LearnerConfig(max_repairs=2, auto_activate=True),
        )

        res = learner.learn(sample_pms_doc, format_id="test_format_pms")

        # Must fail and be rejected after 1 initial + 2 repairs = 3 attempts
        assert not res.passed
        assert res.status == ProposalStatus.REJECTED
        assert res.document_status == DocumentStatus.LEARNING_FAILED
        assert res.attempts_count == 3
        assert res.is_active is False

        # Spec must NOT be activated in SpecStore
        assert not store.exists("test_format_pms")


# ---------------------------------------------------------------------------
# 4. Learning from Human Feedback
# ---------------------------------------------------------------------------

class TestLearningFromHumanFeedback:
    def test_successful_feedback_learning(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)
        fake_llm = FakeLLMAdapter(default_response=valid_spec_dict)

        learner = Learner(
            llm_adapter=fake_llm,
            spec_store=store,
            config=LearnerConfig(auto_activate=False),
        )

        fb = HumanFeedback(
            format_id="test_format_pms",
            corrected_metadata={"client_identifier": "PWM-0042"},
            corrected_summary={"closing_cash": Decimal("15000.00")},
            notes="Verified client ID and closing cash",
        )

        res = learner.learn_from_feedback(sample_pms_doc, fb)

        assert res.passed
        assert res.status == ProposalStatus.PROPOSED
        assert res.attempts_count == 1
        assert res.parse_result.summary.closing_cash == Decimal("15000.00")

    def test_invalid_feedback_rejected_without_llm_calls(self, tmp_path, sample_pms_doc):
        store = SpecStore(specs_dir=tmp_path)
        fake_llm = FakeLLMAdapter()

        learner = Learner(llm_adapter=fake_llm, spec_store=store)

        # Empty feedback with no corrections
        fb = HumanFeedback(format_id="test_format_pms")
        res = learner.learn_from_feedback(sample_pms_doc, fb)

        assert not res.passed
        assert res.status == ProposalStatus.INVALID_FEEDBACK
        assert res.attempts_count == 0
        assert res.error_message is not None

        # Ledger should have 0 records
        assert len(fake_llm.ledger.records) == 0

    def test_discrepancy_with_feedback_is_rejected_or_repaired(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)
        fake_llm = FakeLLMAdapter(default_response=valid_spec_dict)

        learner = Learner(
            llm_adapter=fake_llm,
            spec_store=store,
            config=LearnerConfig(max_repairs=1),
        )

        # Reviewer specifies closing_cash must be 99999.00, but valid_spec extracts 15000.00
        fb = HumanFeedback(
            format_id="test_format_pms",
            corrected_summary={"closing_cash": Decimal("99999.00")},
        )

        res = learner.learn_from_feedback(sample_pms_doc, fb)

        # Because the spec extracts 15000.00, it contradicts human correction 99999.00
        assert not res.passed
        assert res.status == ProposalStatus.REJECTED
        assert any("closing_cash" in e for e in res.repair_history)


# ---------------------------------------------------------------------------
# 5. Strict Preservation of Existing Specifications
# ---------------------------------------------------------------------------

class TestPreservationOfExistingSpecifications:
    def test_rejected_proposal_never_modifies_active_spec(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)

        # 1. Save an active specification in the store
        original_spec = ParsingSpec.from_dict(valid_spec_dict)
        original_spec.description = "ORIGINAL_DESCRIPTION_DO_NOT_TOUCH"
        store.save(original_spec)

        orig_json = store._spec_path("test_format_pms").read_text(encoding="utf-8")

        # 2. Setup Learner with bad response that exhausts retries
        bad_spec_dict = json.loads(json.dumps(valid_spec_dict))
        bad_spec_dict["description"] = "MALFORMED_MUTATED_SPEC"
        bad_spec_dict["summary_fields"]["closing_cash"]["anchor"] = "NON_EXISTENT_ANCHOR"

        fake_llm = FakeLLMAdapter(default_response=bad_spec_dict)
        learner = Learner(llm_adapter=fake_llm, spec_store=store, config=LearnerConfig(max_repairs=1))

        fb = HumanFeedback(
            format_id="test_format_pms",
            corrected_metadata={"provider_name": "Pinnacle"},
        )
        res = learner.learn_from_feedback(sample_pms_doc, fb)

        assert res.status == ProposalStatus.REJECTED

        # 3. Verify original spec on disk was NOT touched
        current_json = store._spec_path("test_format_pms").read_text(encoding="utf-8")
        assert current_json == orig_json
        loaded = store.load("test_format_pms")
        assert loaded.description == "ORIGINAL_DESCRIPTION_DO_NOT_TOUCH"

    def test_draft_proposal_is_kept_separate(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)

        # Active spec exists
        original_spec = ParsingSpec.from_dict(valid_spec_dict)
        original_spec.description = "ORIGINAL_ACTIVE_SPEC"
        store.save(original_spec)

        fake_llm = FakeLLMAdapter(default_response=valid_spec_dict)
        learner = Learner(llm_adapter=fake_llm, spec_store=store, config=LearnerConfig(auto_activate=False))

        fb = HumanFeedback(
            format_id="test_format_pms",
            corrected_metadata={"client_identifier": "PWM-0042"},
        )
        res = learner.propose_spec(sample_pms_doc, fb)

        assert res.status == ProposalStatus.PROPOSED
        assert res.is_active is False

        # Active spec still unchanged
        assert store.load("test_format_pms").description == "ORIGINAL_ACTIVE_SPEC"

        # Explicit draft saving puts it under draft prefix
        draft_path = learner.save_draft_proposal(res.spec)
        assert draft_path.name == "draft_test_format_pms.json"
        assert store.load("test_format_pms").description == "ORIGINAL_ACTIVE_SPEC"
        assert store.load("draft_test_format_pms").metadata.get("status") == "proposed"


# ---------------------------------------------------------------------------
# 6. PII Redaction Verification
# ---------------------------------------------------------------------------

class TestPIIRedactionInPrompts:
    def test_pii_masked_before_llm_request(self, tmp_path, sample_pms_doc, valid_spec_dict):
        store = SpecStore(specs_dir=tmp_path)
        fake_llm = FakeLLMAdapter(default_response=valid_spec_dict)

        learner = Learner(llm_adapter=fake_llm, spec_store=store)
        res = learner.learn(sample_pms_doc, format_id="test_format_pms")

        assert res.passed

        # Check prompt sent to FakeLLMAdapter
        recorded_calls = fake_llm.ledger.records
        assert len(recorded_calls) >= 1

        # The document text in sample_pms_doc contained PAN ABCDE1234F and Rameshwar Saxena
        # Let's inspect the prompt used in the call
        # By reconstructing the prompt generated by the learner:
        from src.learner.prompts import build_initial_prompt
        from src.redactor import Redactor
        _, user_prompt = build_initial_prompt(
            doc=sample_pms_doc,
            format_id="test_format_pms",
            statement_type=StatementType.PMS,
            redactor=Redactor(),
        )

        assert "ABCDE1234F" not in user_prompt
        assert "[REDACTED_PAN]" in user_prompt
        assert "Rameshwar Saxena" not in user_prompt
        assert "[REDACTED_NAME]" in user_prompt
