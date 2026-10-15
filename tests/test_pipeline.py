"""Tests for Step 13: Pipeline Integration.

Covers:
1. End-to-end success path: classification -> fingerprinting -> parsing -> validation -> persistence.
2. Duplicate document handling via content hashing.
3. Password-protected document flow (NEEDS_PASSWORD and password recovery).
4. Classifier routing: SKIPPED for non-statements, MAYBE for ambiguous documents.
5. Template drift quarantine (DRIFT_SUSPECTED) without automatic learning.
6. Unknown format handling:
   - Without learner: quarantined as NEEDS_REVIEW.
   - With learner (auto-activate): learns, activates, and parses.
   - With learner (draft proposal): saved as draft, document marked NEEDS_REVIEW.
   - With learner (repair exhausted): marked LEARNING_FAILED.
7. Validation failures updating final status to NEEDS_REVIEW or PARSED_WITH_WARNINGS.
8. Batch processing with error isolation (failure in one document does not stop batch).
9. Reparse document workflow updating attempts history.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.classifier.models import ClassificationDecision, ClassificationResult
from src.fingerprint import FormatFingerprint, LayoutStats, extract_fingerprint
from src.fingerprint.models import FingerprintMatchResult, FingerprintMatchStatus
from src.ir.exceptions import PDFExtractionError
from src.ir.models import BBox, DocumentIR, PageIR, TableIR, TextBlock, WordIR
from src.learner.learner import Learner
from src.learner.models import LearnerConfig, LearningResult, ProposalStatus
from src.llm_adapter import FakeLLMAdapter
from src.models.enums import DocumentStatus, StatementType, TransactionType
from src.models.models import Holding, ParseResult, Statement, Summary, Transaction
from src.pipeline import (
    BatchPipelineResult,
    Pipeline,
    PipelineConfig,
    PipelineResult,
    compute_file_hash,
    compute_ir_hash,
)
from src.specs.models import FieldLocator, LocatorType, ParsingSpec, TableLocator
from src.specs.store import SpecStore
from src.store.models import DocumentRecord
from src.store.store import StatementStore
from src.validator.models import ValidationFinding, ValidationResult, ValidationSeverity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_specs_dir(tmp_path) -> Path:
    """Create a temporary specs directory."""
    d = tmp_path / "specs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def spec_store(temp_specs_dir) -> SpecStore:
    """Return a SpecStore backed by temporary directory."""
    return SpecStore(specs_dir=temp_specs_dir)


@pytest.fixture
def temp_db_path(tmp_path) -> Path:
    """Return a temporary SQLite database path."""
    return tmp_path / "pipeline_test.db"


@pytest.fixture
def store(temp_db_path) -> StatementStore:
    """Return a StatementStore backed by temporary database."""
    return StatementStore(db_path=temp_db_path)


@pytest.fixture
def sample_pms_ir() -> DocumentIR:
    """Create a clean, reconcilable in-memory DocumentIR for PMS statement."""
    p1_words = [
        WordIR(text="Kotak", page_number=1, bbox=BBox(x0=50, top=30, x1=90, bottom=42)),
        WordIR(text="Wealth", page_number=1, bbox=BBox(x0=95, top=30, x1=140, bottom=42)),
        WordIR(text="PMS", page_number=1, bbox=BBox(x0=145, top=30, x1=170, bottom=42)),
        WordIR(text="Client", page_number=1, bbox=BBox(x0=50, top=50, x1=80, bottom=62)),
        WordIR(text="ID:", page_number=1, bbox=BBox(x0=82, top=50, x1=95, bottom=62)),
        WordIR(text="KOTAK-9988", page_number=1, bbox=BBox(x0=100, top=50, x1=160, bottom=62)),
        WordIR(text="Period:", page_number=1, bbox=BBox(x0=50, top=65, x1=85, bottom=77)),
        WordIR(text="01/01/2026", page_number=1, bbox=BBox(x0=90, top=65, x1=140, bottom=77)),
        WordIR(text="to", page_number=1, bbox=BBox(x0=145, top=65, x1=155, bottom=77)),
        WordIR(text="31/03/2026", page_number=1, bbox=BBox(x0=160, top=65, x1=210, bottom=77)),
    ]

    holdings_table = TableIR(
        page_number=1,
        rows=[
            ["Security Name", "ISIN", "Quantity", "Price", "Cost", "Market Value", "Weight %"],
            ["Reliance Industries", "INE002A01018", "100", "2,500.00", "220,000.00", "250,000.00", "50.00%"],
            ["Infosys Ltd", "INE009A01021", "125", "2,000.00", "230,000.00", "250,000.00", "50.00%"],
        ],
    )

    summary_table = TableIR(
        page_number=1,
        rows=[
            ["Metric", "Amount (INR)"],
            ["Opening Portfolio Value", "450,000.00"],
            ["Closing Portfolio Value", "550,000.00"],
            ["Opening Cash Balance", "30,000.00"],
            ["Closing Cash Balance", "50,000.00"],
        ],
    )

    page = PageIR(
        page_number=1,
        width=595.0,
        height=842.0,
        words=p1_words,
        tables=[holdings_table, summary_table],
    )
    return DocumentIR(page_count=1, pages=[page], metadata={"Producer": "TestReporter"})


@pytest.fixture
def format_a_spec(sample_pms_ir) -> ParsingSpec:
    """Create a ParsingSpec matching sample_pms_ir with an attached fingerprint."""
    fp = extract_fingerprint(sample_pms_ir)
    fp.format_id = "format_kotak_pms"

    return ParsingSpec(
        format_id="format_kotak_pms",
        version="1.0.0",
        statement_type=StatementType.PMS,
        fingerprint=fp,
        metadata_fields={
            "provider_name": FieldLocator(
                locator_type=LocatorType.VALUE_MATCHING_PATTERN,
                pattern=r"(Kotak\s+Wealth\s+PMS)",
                default="Kotak Wealth PMS",
            ),
            "client_identifier": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Client ID:",
            ),
        },
        summary_fields={
            "opening_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Portfolio Value",
            ),
            "closing_value": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Portfolio Value",
            ),
            "opening_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Opening Cash Balance",
            ),
            "closing_cash": FieldLocator(
                locator_type=LocatorType.VALUE_RIGHT_OF_ANCHOR,
                anchor="Closing Cash Balance",
            ),
        },
        holdings_table=TableLocator(
            locator_type=LocatorType.TABLE_AFTER_ANCHOR,
            header_keywords=["Security Name", "ISIN", "Quantity", "Market Value"],
            column_mapping={
                "security_name": "Security Name",
                "isin": "ISIN",
                "quantity": "Quantity",
                "price": "Price",
                "cost": "Cost",
                "market_value": "Market Value",
                "weight": "Weight %",
            },
        ),
    )


# ---------------------------------------------------------------------------
# Test Suites
# ---------------------------------------------------------------------------

class TestPipelineSuccessFlow:
    """End-to-end success path tests."""

    def test_process_document_success(self, store, spec_store, format_a_spec, sample_pms_ir):
        """Clean document with existing spec parses, validates, and persists successfully."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        res = pipeline.process_document(sample_pms_ir, file_path="kotak_statement.pdf")

        assert res.success is True
        assert res.status in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS)
        assert res.format_id == "format_kotak_pms"
        assert res.is_duplicate is False

        # Verify statement extracted
        st = res.statement
        assert st is not None
        assert len(st.holdings) == 2
        assert st.holdings[0].security_name == "Reliance Industries"
        assert st.holdings[0].market_value == Decimal("250000.00")
        assert st.summary is not None
        assert st.summary.closing_value == Decimal("550000.00")
        assert st.summary.closing_cash == Decimal("50000.00")

        # Verify persisted in store
        doc_rec = store.get_document(res.document_id)
        assert doc_rec is not None
        assert doc_rec.format_id == "format_kotak_pms"
        assert doc_rec.status == res.status

        # Verify statement recoverable from store
        stored_st = store.get_statement(res.document_id)
        assert stored_st is not None
        assert stored_st.holdings[0].isin == "INE002A01018"

        # Verify attempts recorded
        attempts = store.list_processing_attempts(res.document_id)
        assert len(attempts) >= 1
        assert attempts[0].status == res.status

    def test_process_file_with_synthetic_pdf(self, store, spec_store, format_a_spec, sample_pms_ir, tmp_path):
        """Full process_file flow with mocked extract_pdf to test file hash and dispatch."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        fake_pdf = tmp_path / "sample_test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 test synthetic content byte stream")

        with patch("src.pipeline.pipeline.extract_pdf", return_value=sample_pms_ir):
            res = pipeline.process_file(fake_pdf)

        assert res.success is True
        assert res.format_id == "format_kotak_pms"
        assert res.file_hash == compute_file_hash(fake_pdf)
        assert res.document_id == f"doc_{res.file_hash[:16]}"


class TestPipelineDuplicateHandling:
    """Duplicate detection tests."""

    def test_duplicate_document_detected_and_recorded(self, store, spec_store, format_a_spec, sample_pms_ir):
        """Second attempt with identical content hash returns DUPLICATE status."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        # First run
        res1 = pipeline.process_document(sample_pms_ir, file_path="doc1.pdf")
        assert res1.success is True
        assert res1.is_duplicate is False

        # Second run with same document / hash
        res2 = pipeline.process_document(sample_pms_ir, file_path="doc2_copy.pdf")
        assert res2.is_duplicate is True
        assert res2.status == DocumentStatus.DUPLICATE
        assert res2.document_id == res1.document_id

        # Verify attempt history tracks the duplicate
        attempts = store.list_processing_attempts(res1.document_id)
        assert any(a.status == DocumentStatus.DUPLICATE for a in attempts)


class TestPipelinePasswordFlow:
    """Encrypted document and password handling tests."""

    def test_encrypted_pdf_without_password_marked_needs_password(self, store, spec_store, tmp_path):
        """Encrypted PDF without supplied password results in NEEDS_PASSWORD status."""
        pipeline = Pipeline(store=store, spec_store=spec_store)
        fake_pdf = tmp_path / "locked.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 encrypted content")

        with patch("src.pipeline.pipeline.extract_pdf", side_effect=PDFExtractionError("Password required")):
            res = pipeline.process_file(fake_pdf)

        assert res.status == DocumentStatus.NEEDS_PASSWORD
        assert "password" in res.errors[0].lower() or "password" in res.warnings[0].lower()

        # Document record created in store
        doc = store.get_document(res.document_id)
        assert doc is not None
        assert doc.status == DocumentStatus.NEEDS_PASSWORD

    def test_encrypted_pdf_recovers_via_config_password(
        self, store, spec_store, format_a_spec, sample_pms_ir, tmp_path
    ):
        """Encrypted PDF recovers when candidate password matches in config."""
        spec_store.save(format_a_spec)
        config = PipelineConfig(passwords=["SECRET_123", "WRONG_PWD"])
        pipeline = Pipeline(store=store, spec_store=spec_store, config=config)

        fake_pdf = tmp_path / "protected.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 encrypted content stream")

        def mock_extract(path, password=None):
            if password == "SECRET_123":
                return sample_pms_ir
            raise PDFExtractionError("Invalid password or encrypted PDF")

        with patch("src.pipeline.pipeline.extract_pdf", side_effect=mock_extract):
            res = pipeline.process_file(fake_pdf)

        assert res.success is True
        assert res.format_id == "format_kotak_pms"


class TestPipelineClassifierRouting:
    """Classification outcome routing tests."""

    def test_irrelevant_document_marked_skipped(self, store, spec_store):
        """Document classified as SKIP returns SKIPPED status."""
        empty_doc = DocumentIR(page_count=0, pages=[])
        pipeline = Pipeline(store=store, spec_store=spec_store)

        res = pipeline.process_document(empty_doc, file_path="spam.pdf")

        assert res.status == DocumentStatus.SKIPPED
        assert res.classification is not None
        assert res.classification.decision == ClassificationDecision.SKIP

        # Store reflects SKIPPED
        doc = store.get_document(res.document_id)
        assert doc.status == DocumentStatus.SKIPPED

    def test_ambiguous_document_routed_to_maybe(self, store, spec_store):
        """Ambiguous document with low confidence routed to MAYBE review queue."""
        words = [WordIR(text="Invoice", page_number=1, bbox=BBox(x0=10, top=10, x1=50, bottom=20))]
        doc = DocumentIR(page_count=1, pages=[PageIR(page_number=1, width=500, height=500, words=words)])

        pipeline = Pipeline(store=store, spec_store=spec_store)
        res = pipeline.process_document(doc, file_path="invoice.pdf")

        # Either SKIP or MAYBE for an invoice without financial tables
        assert res.status in (DocumentStatus.MAYBE, DocumentStatus.SKIPPED)


class TestPipelineDriftQuarantine:
    """Template drift detection and quarantine tests."""

    def test_near_match_triggers_drift_suspected_quarantine(self, store, spec_store, format_a_spec, sample_pms_ir):
        """Document with near-match fingerprint triggers DRIFT_SUSPECTED and is quarantined."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        # Mutate document layout to fall in near-match range [0.65, 0.90)
        drift_doc = DocumentIR(
            page_count=1,
            pages=[
                PageIR(
                    page_number=1,
                    width=sample_pms_ir.pages[0].width,
                    height=sample_pms_ir.pages[0].height,
                    words=sample_pms_ir.pages[0].words[:5],  # truncated words
                    tables=[
                        TableIR(
                            page_number=1,
                            rows=[
                                ["Holdings Breakdown", "ISIN", "Quantity", "Value"],
                                ["Reliance Industries", "INE002A01018", "100", "250,000.00"],
                            ],
                        )
                    ],
                )
            ],
        )

        # Force match_spec_by_fingerprint to return DRIFT_SUSPECTED
        fake_match = FingerprintMatchResult(
            status=FingerprintMatchStatus.DRIFT_SUSPECTED,
            similarity_score=0.75,
            matched_format_id="format_kotak_pms",
            reasons=["Table 0 has renamed headers (pos_ratio=0.70)"],
        )

        with patch.object(spec_store, "match_spec_by_fingerprint", return_value=(None, fake_match)):
            res = pipeline.process_document(drift_doc, file_path="drifted_statement.pdf")

        assert res.status == DocumentStatus.DRIFT_SUSPECTED
        assert res.is_quarantined is True
        assert res.format_id == "format_kotak_pms"

        # Verify persistent store record
        doc = store.get_document(res.document_id)
        assert doc.status == DocumentStatus.DRIFT_SUSPECTED


class TestPipelineUnknownFormatAndLearner:
    """Unknown format and learner integration tests."""

    def test_unknown_format_without_learner_held_in_review(self, store, spec_store, sample_pms_ir):
        """Unknown format when learner is disabled is marked NEEDS_REVIEW."""
        config = PipelineConfig(enable_learning=False)
        pipeline = Pipeline(store=store, spec_store=spec_store, config=config)

        res = pipeline.process_document(sample_pms_ir, file_path="unknown.pdf")

        assert res.status == DocumentStatus.NEEDS_REVIEW
        assert res.is_quarantined is True
        assert any("Unknown" in w for w in res.warnings)

    def test_unknown_format_with_learner_auto_activates(
        self, store, spec_store, format_a_spec, sample_pms_ir
    ):
        """Unknown format with learner enabled and auto_activate=True parses successfully."""
        config = PipelineConfig(enable_learning=True, auto_activate_learned_spec=True)

        fake_learner = MagicMock(spec=Learner)
        fake_learning_res = LearningResult(
            format_id="format_learned_pms",
            status=ProposalStatus.ACCEPTED,
            document_status=DocumentStatus.PARSED,
            spec=format_a_spec,
            is_active=True,
            attempts_count=1,
            parse_result=ParseResult(
                statement=Statement(provider_name="Kotak Wealth PMS"),
                status=DocumentStatus.PARSED,
            ),
            validation_result=ValidationResult(
                final_status=DocumentStatus.PARSED,
                passed=True,
                checks_run=["cash_reconciliation"],
                checks_skipped=[],
                findings=[],
            ),
        )
        fake_learner.learn.return_value = fake_learning_res

        pipeline = Pipeline(store=store, spec_store=spec_store, learner=fake_learner, config=config)
        res = pipeline.process_document(sample_pms_ir, file_path="new_format.pdf")

        fake_learner.learn.assert_called_once()
        assert res.success is True
        assert res.status == DocumentStatus.PARSED

    def test_unknown_format_learner_proposal_quarantined_as_needs_review(
        self, store, spec_store, format_a_spec, sample_pms_ir
    ):
        """Learner produces draft proposal (auto_activate=False) -> NEEDS_REVIEW."""
        config = PipelineConfig(enable_learning=True, auto_activate_learned_spec=False)

        fake_learner = MagicMock(spec=Learner)
        fake_learning_res = LearningResult(
            format_id="draft_learned_pms",
            status=ProposalStatus.PROPOSED,
            document_status=DocumentStatus.NEEDS_REVIEW,
            spec=format_a_spec,
            is_active=False,
            attempts_count=1,
        )
        fake_learner.learn.return_value = fake_learning_res

        pipeline = Pipeline(store=store, spec_store=spec_store, learner=fake_learner, config=config)
        res = pipeline.process_document(sample_pms_ir, file_path="draft_format.pdf")

        assert res.status == DocumentStatus.NEEDS_REVIEW
        assert res.is_quarantined is True

    def test_unknown_format_learner_failure_marked_needs_review(
        self, store, spec_store, sample_pms_ir
    ):
        """Learner rejects proposal after exhausted repairs -> returns NEEDS_REVIEW."""
        config = PipelineConfig(enable_learning=True)

        fake_learner = MagicMock(spec=Learner)
        fake_learning_res = LearningResult(
            format_id="format_failed",
            status=ProposalStatus.REJECTED,
            document_status=DocumentStatus.LEARNING_FAILED,
            error_message="Repairs exhausted; reconciliation failed",
            attempts_count=3,
        )
        fake_learner.learn.return_value = fake_learning_res

        pipeline = Pipeline(store=store, spec_store=spec_store, learner=fake_learner, config=config)
        res = pipeline.process_document(sample_pms_ir, file_path="failed_format.pdf")

        assert res.status == DocumentStatus.NEEDS_REVIEW
        assert res.is_quarantined is True
        assert res.learning_result.status == ProposalStatus.REJECTED


class TestPipelineValidationOutcomes:
    """Validation outcomes modifying final status."""

    def test_validation_errors_sets_needs_review(self, store, spec_store, format_a_spec, sample_pms_ir):
        """Parse result with mathematical error sets status to NEEDS_REVIEW."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        # Mock validator to return validation error
        finding = ValidationFinding(
            check="cash_reconciliation",
            severity=ValidationSeverity.ERROR,
            message="Cash reconciliation failed: closing cash mismatch",
            difference=Decimal("500.00"),
        )
        val_fail = ValidationResult(
            final_status=DocumentStatus.NEEDS_REVIEW,
            passed=False,
            checks_run=["cash_reconciliation"],
            checks_skipped=[],
            findings=[finding],
        )

        with patch.object(pipeline.validator, "validate", return_value=val_fail):
            res = pipeline.process_document(sample_pms_ir, file_path="mismatch.pdf")

        assert res.status == DocumentStatus.NEEDS_REVIEW
        assert res.validation_result.passed is False


class TestPipelineBatchAndReparse:
    """Batch processing and reparse workflow tests."""

    def test_process_batch_error_isolation(self, store, spec_store, format_a_spec, sample_pms_ir, tmp_path):
        """Batch continues processing when one file in the batch encounters an error."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        f_valid = tmp_path / "valid.pdf"
        f_valid.write_bytes(b"%PDF-1.4 valid 1")

        f_invalid = tmp_path / "invalid.pdf"
        f_invalid.write_bytes(b"%PDF-1.4 corrupted 2")

        f_dup = tmp_path / "duplicate.pdf"
        f_dup.write_bytes(b"%PDF-1.4 valid 1")  # identical content to f_valid

        def mock_extract(path, password=None):
            if "invalid" in str(path):
                raise PDFExtractionError("File corrupted")
            return sample_pms_ir

        with patch("src.pipeline.pipeline.extract_pdf", side_effect=mock_extract):
            batch_res = pipeline.process_batch([f_valid, f_invalid, f_dup])

        assert batch_res.total_documents == 3
        assert batch_res.successful == 1
        assert batch_res.failed == 1
        assert batch_res.duplicates == 1

    def test_reparse_document_updates_store(self, store, spec_store, format_a_spec, sample_pms_ir, tmp_path):
        """reparse_document refreshes document parse and appends to processing attempts."""
        spec_store.save(format_a_spec)
        pipeline = Pipeline(store=store, spec_store=spec_store)

        fake_pdf = tmp_path / "doc_to_reparse.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 reparse doc")

        with patch("src.pipeline.pipeline.extract_pdf", return_value=sample_pms_ir):
            initial_res = pipeline.process_file(fake_pdf)
            assert initial_res.success is True

            reparse_res = pipeline.reparse_document(initial_res.document_id)
            assert reparse_res.success is True

        attempts = store.list_processing_attempts(initial_res.document_id)
        assert len(attempts) >= 2
        assert attempts[-1].attempt_number == 2


class TestOneTimeLearningAndSpecReuse:
    """Core tests for one-time LLM learning and zero-LLM specification reuse."""

    def test_first_time_learning_persists_spec_and_reused_with_zero_llm_calls_after_restart(
        self, temp_specs_dir, temp_db_path, format_a_spec, sample_pms_ir
    ):
        """First statement learns spec and persists to disk; restart reuses it with 0 LLM calls."""
        # Step 1: Initial process with learning enabled on an unknown format
        spec_store_1 = SpecStore(specs_dir=temp_specs_dir)
        store_1 = StatementStore(db_path=temp_db_path)
        assert len(spec_store_1.list_formats()) == 0

        # Configure fake LLM that produces format_a_spec when prompted
        fake_llm_1 = FakeLLMAdapter(default_response=format_a_spec.to_dict())
        learner_1 = Learner(
            llm_adapter=fake_llm_1,
            spec_store=spec_store_1,
        )
        config_1 = PipelineConfig(enable_learning=True, auto_activate_learned_spec=True)
        pipeline_1 = Pipeline(
            store=store_1,
            spec_store=spec_store_1,
            learner=learner_1,
            config=config_1,
        )

        res1 = pipeline_1.process_document(sample_pms_ir, file_path="stmt_month_1.pdf")
        assert res1.status == DocumentStatus.PARSED
        assert res1.format_id == format_a_spec.format_id
        # LLM was invoked exactly once for the first statement
        assert len(fake_llm_1.ledger.records) == 1

        # Verify durable storage on disk
        persisted_file = temp_specs_dir / f"{format_a_spec.format_id}.json"
        assert persisted_file.exists()

        # Step 2: Simulate process restart / separate invocation
        # Create a completely fresh SpecStore and Pipeline from the persisted directory
        spec_store_2 = SpecStore(specs_dir=temp_specs_dir)
        store_2 = StatementStore(db_path=temp_db_path)
        assert format_a_spec.format_id in spec_store_2.list_formats()

        # Create a fresh LLM adapter that records any attempt to call it
        fake_llm_2 = FakeLLMAdapter()
        learner_2 = Learner(
            llm_adapter=fake_llm_2,
            spec_store=spec_store_2,
        )
        # Even with learning enabled on this new pipeline, it MUST NOT call LLM
        config_2 = PipelineConfig(enable_learning=True, auto_activate_learned_spec=True)
        pipeline_2 = Pipeline(
            store=store_2,
            spec_store=spec_store_2,
            learner=learner_2,
            config=config_2,
        )

        # Create a subsequent statement with the same layout/template but different values
        p2_words = [
            WordIR(text="Kotak", page_number=1, bbox=BBox(x0=50, top=30, x1=90, bottom=42)),
            WordIR(text="Wealth", page_number=1, bbox=BBox(x0=95, top=30, x1=140, bottom=42)),
            WordIR(text="PMS", page_number=1, bbox=BBox(x0=145, top=30, x1=170, bottom=42)),
            WordIR(text="Client", page_number=1, bbox=BBox(x0=50, top=50, x1=80, bottom=62)),
            WordIR(text="ID:", page_number=1, bbox=BBox(x0=82, top=50, x1=95, bottom=62)),
            WordIR(text="KOTAK-7711", page_number=1, bbox=BBox(x0=100, top=50, x1=160, bottom=62)),
            WordIR(text="Period:", page_number=1, bbox=BBox(x0=50, top=65, x1=85, bottom=77)),
            WordIR(text="01/04/2026", page_number=1, bbox=BBox(x0=90, top=65, x1=140, bottom=77)),
            WordIR(text="to", page_number=1, bbox=BBox(x0=145, top=65, x1=155, bottom=77)),
            WordIR(text="30/06/2026", page_number=1, bbox=BBox(x0=160, top=65, x1=210, bottom=77)),
        ]
        sample_pms_ir_2 = DocumentIR(
            page_count=1,
            pages=[
                PageIR(
                    page_number=1,
                    width=595.0,
                    height=842.0,
                    words=p2_words,
                    tables=sample_pms_ir.pages[0].tables,
                )
            ],
            metadata={"Producer": "TestReporter"},
        )

        res2 = pipeline_2.process_document(sample_pms_ir_2, file_path="stmt_month_2.pdf")
        assert res2.status == DocumentStatus.PARSED
        assert res2.format_id == format_a_spec.format_id
        # Zero additional LLM calls!
        assert len(fake_llm_2.ledger.records) == 0

        # Step 3: Subsequent statement with learning disabled entirely
        temp_db_path_3 = temp_db_path.parent / "pipeline_test_step3.db"
        store_3 = StatementStore(db_path=temp_db_path_3)
        fake_llm_3 = FakeLLMAdapter()
        learner_3 = Learner(
            llm_adapter=fake_llm_3,
            spec_store=spec_store_2,
        )
        pipeline_3 = Pipeline(
            store=store_3,
            spec_store=spec_store_2,
            learner=learner_3,
            config=PipelineConfig(enable_learning=False),
        )
        res3 = pipeline_3.process_document(sample_pms_ir_2, file_path="stmt_month_3.pdf")
        assert res3.status == DocumentStatus.PARSED
        assert res3.format_id == format_a_spec.format_id
        assert len(fake_llm_3.ledger.records) == 0

    def test_unknown_format_learning_disabled_returns_needs_review_zero_llm_calls(
        self, temp_specs_dir, temp_db_path, sample_pms_ir
    ):
        """Unknown format with learning disabled returns NEEDS_REVIEW with zero LLM calls."""
        spec_store = SpecStore(specs_dir=temp_specs_dir)
        store = StatementStore(db_path=temp_db_path)
        fake_llm = FakeLLMAdapter()
        learner = Learner(llm_adapter=fake_llm, spec_store=spec_store)

        pipeline = Pipeline(
            store=store,
            spec_store=spec_store,
            learner=learner,
            config=PipelineConfig(enable_learning=False),
        )

        res = pipeline.process_document(sample_pms_ir, file_path="unknown.pdf")
        assert res.status == DocumentStatus.NEEDS_REVIEW
        assert res.is_quarantined is True
        assert len(fake_llm.ledger.records) == 0
        assert len(spec_store.list_formats()) == 0

    def test_failed_learning_returns_needs_review_and_does_not_save_spec(
        self, temp_specs_dir, temp_db_path, sample_pms_ir
    ):
        """Learning failure returns NEEDS_REVIEW and never saves unvalidated specs."""
        spec_store = SpecStore(specs_dir=temp_specs_dir)
        store = StatementStore(db_path=temp_db_path)
        fake_llm = FakeLLMAdapter(default_response="MALFORMED_NON_JSON")
        learner = Learner(llm_adapter=fake_llm, spec_store=spec_store)

        pipeline = Pipeline(
            store=store,
            spec_store=spec_store,
            learner=learner,
            config=PipelineConfig(enable_learning=True, auto_activate_learned_spec=True),
        )

        res = pipeline.process_document(sample_pms_ir, file_path="unlearnable.pdf")
        assert res.status == DocumentStatus.NEEDS_REVIEW
        assert res.is_quarantined is True
        # SpecStore remains untouched
        assert len(spec_store.list_formats()) == 0

    def test_suspected_drift_quarantined_for_review_without_llm_call(
        self, temp_specs_dir, temp_db_path, format_a_spec, sample_pms_ir
    ):
        """Drift-suspected document is quarantined for review without invoking LLM."""
        spec_store = SpecStore(specs_dir=temp_specs_dir)
        spec_store.save(format_a_spec)
        store = StatementStore(db_path=temp_db_path)
        fake_llm = FakeLLMAdapter()
        learner = Learner(llm_adapter=fake_llm, spec_store=spec_store)

        pipeline = Pipeline(
            store=store,
            spec_store=spec_store,
            learner=learner,
            config=PipelineConfig(enable_learning=True, auto_activate_learned_spec=True),
        )

        drift_match = FingerprintMatchResult(
            status=FingerprintMatchStatus.DRIFT_SUSPECTED,
            similarity_score=0.72,
            matched_format_id=format_a_spec.format_id,
            reasons=["Table 0 headers partially changed"],
        )

        with patch.object(spec_store, "match_spec_by_fingerprint", return_value=(None, drift_match)):
            res = pipeline.process_document(sample_pms_ir, file_path="drifted.pdf")

        assert res.status == DocumentStatus.DRIFT_SUSPECTED
        assert res.is_quarantined is True
        assert res.format_id == format_a_spec.format_id
        # LLM was never called
        assert len(fake_llm.ledger.records) == 0
