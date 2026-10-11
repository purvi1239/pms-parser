"""Tests for Step 12: Persistent SQLite StatementStore.

Covers:
1. Database initialization (in-memory and temporary file paths).
2. Document CRUD, filtering, pagination, and deletion.
3. Duplicate detection via SHA-256 file hashes.
4. Parsed Statement persistence, Decimal round-tripping, and JSON export.
5. Atomic save_parse_result operations.
6. ValidationResult persistence and audit retrieval.
7. SQLite LLM call ledger recording and cost aggregation.
8. Multi-attempt processing history and reparse tracking.
9. Transaction rollback on database failure.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import pytest

from src.fingerprint.models import FormatFingerprint, LayoutStats
from src.llm_adapter.models import LLMCallRecord
from src.models.enums import DocumentStatus, StatementType, TransactionType
from src.models.models import Holding, ParseResult, Statement, Summary, Transaction
from src.store import (
    DocumentNotFoundError,
    DocumentRecord,
    DuplicateDocumentError,
    StatementNotFoundError,
    StatementStore,
)
from src.validator.models import ValidationFinding, ValidationResult, ValidationSeverity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db_path(tmp_path) -> Path:
    """Return a path to a temporary SQLite database file."""
    return tmp_path / "test_pms_parser.db"


@pytest.fixture
def store(temp_db_path) -> StatementStore:
    """Return a StatementStore backed by a temporary file."""
    return StatementStore(db_path=temp_db_path)


@pytest.fixture
def memory_store() -> StatementStore:
    """Return an in-memory StatementStore."""
    store = StatementStore(db_path=":memory:")
    yield store
    store.close()


@pytest.fixture
def sample_statement() -> Statement:
    """Construct a sample Statement with Decimal financial values."""
    holdings = [
        Holding(
            security_name="Reliance Industries Ltd",
            isin="INE002A01018",
            quantity=Decimal("150"),
            price=Decimal("2500.00"),
            cost=Decimal("330000.00"),
            market_value=Decimal("375000.00"),
            weight=Decimal("15.50"),
        ),
        Holding(
            security_name="Tata Consultancy Services",
            isin="INE467B01029",
            quantity=Decimal("80"),
            price=Decimal("3900.00"),
            cost=Decimal("280000.00"),
            market_value=Decimal("312000.00"),
            weight=Decimal("12.90"),
        ),
    ]

    transactions = [
        Transaction(
            date=date(2026, 1, 15),
            type=TransactionType.BUY,
            security="Reliance Industries Ltd",
            quantity=Decimal("20"),
            price=Decimal("2250.00"),
            amount=Decimal("-45000.00"),
        ),
        Transaction(
            date="2026-02-20",
            type=TransactionType.DIVIDEND,
            security="Tata Consultancy Services",
            amount=Decimal("3500.00"),
        ),
    ]

    summary = Summary(
        opening_value=Decimal("1000000.00"),
        closing_value=Decimal("1050000.00"),
        opening_cash=Decimal("50000.00"),
        closing_cash=Decimal("15000.00"),
        fees_charged=Decimal("2500.00"),
        income=Decimal("3500.00"),
    )

    return Statement(
        provider_name="Pinnacle Wealth Management",
        strategy_name="India Growth Portfolio",
        client_identifier="PWM-CLIENT-0042",
        statement_period="01 January 2026 to 31 March 2026",
        currency="INR",
        statement_type=StatementType.PMS,
        holdings=holdings,
        transactions=transactions,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# 1. Database Initialization
# ---------------------------------------------------------------------------

class TestDatabaseInitialization:
    def test_creates_tables_and_indexes(self, store, temp_db_path):
        assert temp_db_path.exists()

        # Connect directly and inspect SQLite master
        with sqlite3.connect(str(temp_db_path)) as conn:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            assert "documents" in tables
            assert "statements" in tables
            assert "validation_results" in tables
            assert "llm_calls" in tables
            assert "processing_attempts" in tables

            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            indexes = {row[0] for row in cur.fetchall()}
            assert "idx_documents_file_hash" in indexes
            assert "idx_documents_status" in indexes
            assert "idx_statements_client_id" in indexes

    def test_in_memory_initialization(self, memory_store):
        doc = DocumentRecord(
            id="doc-mem-1",
            file_path="/tmp/doc.pdf",
            file_hash="hash123",
            status=DocumentStatus.CLASSIFIED,
        )
        memory_store.create_document(doc)
        retrieved = memory_store.get_document("doc-mem-1")
        assert retrieved is not None
        assert retrieved.id == "doc-mem-1"

    def test_reopening_existing_database_preserves_data(self, temp_db_path):
        # Open store 1, write record
        s1 = StatementStore(db_path=temp_db_path)
        doc = DocumentRecord(
            id="doc-persist-1",
            file_path="/tmp/file.pdf",
            file_hash="sha256-abc",
            status=DocumentStatus.PARSED,
        )
        s1.create_document(doc)

        # Open store 2 on same path, read record
        s2 = StatementStore(db_path=temp_db_path)
        retrieved = s2.get_document("doc-persist-1")
        assert retrieved is not None
        assert retrieved.file_hash == "sha256-abc"
        assert retrieved.status == DocumentStatus.PARSED


# ---------------------------------------------------------------------------
# 2. Document Operations & Duplicate Detection
# ---------------------------------------------------------------------------

class TestDocumentOperations:
    def test_create_and_get_document(self, store):
        fp = FormatFingerprint(
            format_id="format_a",
            anchor_phrases=["portfolio valuation"],
            table_headers=[["security", "isin", "qty"]],
            layout_stats=LayoutStats(page_count=2, table_count=1, column_counts=[3]),
        )

        doc = DocumentRecord(
            id="doc-001",
            file_path="/inbox/statement_q1.pdf",
            file_hash="sha256-hash-001",
            status=DocumentStatus.CLASSIFIED,
            statement_type=StatementType.PMS,
            format_id="format_a",
            spec_version_id="1.0.0",
            fingerprint=fp,
            warnings=["Initial warning"],
            errors=[],
            raw_metadata={"author": "Fund House"},
        )

        store.create_document(doc)
        retrieved = store.get_document_or_raise("doc-001")

        assert retrieved.id == "doc-001"
        assert retrieved.file_path == "/inbox/statement_q1.pdf"
        assert retrieved.file_hash == "sha256-hash-001"
        assert retrieved.status == DocumentStatus.CLASSIFIED
        assert retrieved.statement_type == StatementType.PMS
        assert retrieved.format_id == "format_a"
        assert retrieved.spec_version_id == "1.0.0"
        assert retrieved.fingerprint is not None
        assert retrieved.fingerprint.anchor_phrases == ["portfolio valuation"]
        assert retrieved.warnings == ["Initial warning"]
        assert retrieved.raw_metadata.get("author") == "Fund House"

    def test_get_missing_document(self, store):
        assert store.get_document("non-existent-id") is None
        with pytest.raises(DocumentNotFoundError):
            store.get_document_or_raise("non-existent-id")

    def test_duplicate_id_raises_error(self, store):
        doc1 = DocumentRecord(id="doc-dup", file_path="/p1.pdf", file_hash="hash1")
        doc2 = DocumentRecord(id="doc-dup", file_path="/p2.pdf", file_hash="hash2")

        store.create_document(doc1)
        with pytest.raises(DuplicateDocumentError):
            store.create_document(doc2)

    def test_find_by_file_hash(self, store):
        doc = DocumentRecord(
            id="doc-hash-1",
            file_path="/inbox/stmt.pdf",
            file_hash="unique-sha256-token",
            status=DocumentStatus.PARSED,
        )
        store.create_document(doc)

        found = store.find_by_file_hash("unique-sha256-token")
        assert found is not None
        assert found.id == "doc-hash-1"

        missing = store.find_by_file_hash("non-existent-hash")
        assert missing is None

    def test_update_status(self, store):
        doc = DocumentRecord(
            id="doc-status-1",
            file_path="/inbox/test.pdf",
            file_hash="hash-status",
            status=DocumentStatus.CLASSIFIED,
        )
        store.create_document(doc)

        updated = store.update_status(
            "doc-status-1",
            status=DocumentStatus.PARSED_WITH_WARNINGS,
            warnings=["Missing optional units"],
            errors=[],
        )

        assert updated.status == DocumentStatus.PARSED_WITH_WARNINGS
        assert updated.warnings == ["Missing optional units"]
        assert updated.updated_at >= doc.updated_at

    def test_update_document_fields(self, store):
        doc = DocumentRecord(
            id="doc-fields-1",
            file_path="/inbox/test.pdf",
            file_hash="hash-fields",
            status=DocumentStatus.CLASSIFIED,
        )
        store.create_document(doc)

        updated = store.update_document(
            "doc-fields-1",
            status=DocumentStatus.NEEDS_REVIEW,
            format_id="format_b",
            errors=["Cash reconciliation failed"],
        )

        assert updated.status == DocumentStatus.NEEDS_REVIEW
        assert updated.format_id == "format_b"
        assert updated.errors == ["Cash reconciliation failed"]

    def test_list_documents_filtering_and_pagination(self, store):
        store.create_document(DocumentRecord(id="d1", file_path="/1.pdf", file_hash="h1", status=DocumentStatus.PARSED, format_id="fmt_a"))
        store.create_document(DocumentRecord(id="d2", file_path="/2.pdf", file_hash="h2", status=DocumentStatus.PARSED, format_id="fmt_a"))
        store.create_document(DocumentRecord(id="d3", file_path="/3.pdf", file_hash="h3", status=DocumentStatus.NEEDS_REVIEW, format_id="fmt_a"))
        store.create_document(DocumentRecord(id="d4", file_path="/4.pdf", file_hash="h4", status=DocumentStatus.PARSED, format_id="fmt_b"))

        # Filter by status
        parsed = store.list_documents(status=DocumentStatus.PARSED)
        assert len(parsed) == 3

        # Filter by format
        fmt_a = store.list_documents(format_id="fmt_a")
        assert len(fmt_a) == 3

        # Filter by status and format
        parsed_fmt_a = store.list_documents(status=DocumentStatus.PARSED, format_id="fmt_a")
        assert len(parsed_fmt_a) == 2

        # Pagination
        page1 = store.list_documents(limit=2, offset=0)
        page2 = store.list_documents(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert {d.id for d in page1}.isdisjoint({d.id for d in page2})

    def test_delete_document_cascades(self, store, sample_statement):
        doc = DocumentRecord(id="doc-del", file_path="/del.pdf", file_hash="hdel")
        store.create_document(doc)
        store.save_statement("doc-del", sample_statement)
        store.record_processing_attempt("doc-del", status=DocumentStatus.PARSED)

        assert store.get_document("doc-del") is not None
        assert store.get_statement("doc-del") is not None
        assert len(store.list_processing_attempts("doc-del")) == 1

        deleted = store.delete_document("doc-del")
        assert deleted is True
        assert store.get_document("doc-del") is None
        assert store.get_statement("doc-del") is None
        assert len(store.list_processing_attempts("doc-del")) == 0


# ---------------------------------------------------------------------------
# 3. Statement Persistence & JSON Export
# ---------------------------------------------------------------------------

class TestStatementPersistenceAndExport:
    def test_save_and_retrieve_statement(self, store, sample_statement):
        doc = DocumentRecord(id="doc-stmt-1", file_path="/path.pdf", file_hash="hstmt1")
        store.create_document(doc)

        store.save_statement("doc-stmt-1", sample_statement)
        retrieved = store.get_statement_or_raise("doc-stmt-1")

        # Top-level fields
        assert retrieved.provider_name == "Pinnacle Wealth Management"
        assert retrieved.client_identifier == "PWM-CLIENT-0042"
        assert retrieved.currency == "INR"
        assert retrieved.statement_type == StatementType.PMS

        # Decimal precision check on Holdings
        assert len(retrieved.holdings) == 2
        h1 = retrieved.holdings[0]
        assert h1.security_name == "Reliance Industries Ltd"
        assert h1.isin == "INE002A01018"
        assert h1.quantity == Decimal("150")
        assert h1.market_value == Decimal("375000.00")
        assert h1.weight == Decimal("15.50")

        # Transactions
        assert len(retrieved.transactions) == 2
        t1 = retrieved.transactions[0]
        assert t1.type == TransactionType.BUY
        assert t1.amount == Decimal("-45000.00")

        # Summary
        assert retrieved.summary is not None
        assert retrieved.summary.opening_value == Decimal("1000000.00")
        assert retrieved.summary.closing_cash == Decimal("15000.00")

    def test_missing_statement_handling(self, store):
        doc = DocumentRecord(id="doc-nostmt", file_path="/path.pdf", file_hash="hnostmt")
        store.create_document(doc)

        assert store.get_statement("doc-nostmt") is None
        with pytest.raises(StatementNotFoundError):
            store.get_statement_or_raise("doc-nostmt")

    def test_save_statement_for_missing_doc_raises_error(self, store, sample_statement):
        with pytest.raises(DocumentNotFoundError):
            store.save_statement("missing-doc-id", sample_statement)

    def test_export_statement_matches_db(self, store, sample_statement):
        doc = DocumentRecord(id="doc-export-1", file_path="/path.pdf", file_hash="hexp")
        store.create_document(doc)
        store.save_statement("doc-export-1", sample_statement)

        # Export as dict
        stmt_dict = store.export_statement_dict("doc-export-1")
        assert isinstance(stmt_dict, dict)
        assert stmt_dict["provider_name"] == "Pinnacle Wealth Management"
        assert len(stmt_dict["holdings"]) == 2

        # Export as JSON string
        json_str = store.export_statement_json("doc-export-1")
        assert isinstance(json_str, str)
        reloaded = json.loads(json_str)
        assert reloaded["client_identifier"] == "PWM-CLIENT-0042"
        assert reloaded["summary"]["closing_cash"] == "15000.00"

    def test_atomic_save_parse_result(self, store, sample_statement):
        doc = DocumentRecord(id="doc-parse-1", file_path="/path.pdf", file_hash="hpr1")
        store.create_document(doc)

        parse_result = ParseResult(
            statement=sample_statement,
            holdings=sample_statement.holdings,
            transactions=sample_statement.transactions,
            summary=sample_statement.summary,
            status=DocumentStatus.PARSED_WITH_WARNINGS,
            warnings=["Slight weight rounding"],
            errors=[],
        )

        store.save_parse_result("doc-parse-1", parse_result, format_id="fmt_format_a")

        updated_doc = store.get_document_or_raise("doc-parse-1")
        assert updated_doc.status == DocumentStatus.PARSED_WITH_WARNINGS
        assert updated_doc.format_id == "fmt_format_a"
        assert updated_doc.warnings == ["Slight weight rounding"]

        saved_stmt = store.get_statement_or_raise("doc-parse-1")
        assert saved_stmt.provider_name == "Pinnacle Wealth Management"


# ---------------------------------------------------------------------------
# 4. Validation Result Persistence
# ---------------------------------------------------------------------------

class TestValidationResultPersistence:
    def test_save_and_retrieve_validation_result(self, store):
        doc = DocumentRecord(id="doc-val-1", file_path="/path.pdf", file_hash="hval1")
        store.create_document(doc)

        findings = [
            ValidationFinding(
                check="cash_reconciliation",
                severity=ValidationSeverity.ERROR,
                message="Cash discrepancy of 500.00 exceeds tolerance",
                field="summary.closing_cash",
                actual="15500.00",
                expected="15000.00",
            ),
            ValidationFinding(
                check="check_portfolio_weights",
                severity=ValidationSeverity.WARNING,
                message="Weights sum to 99.8%",
            ),
        ]

        val_result = ValidationResult(
            findings=findings,
            checks_run=["cash_reconciliation", "check_portfolio_weights", "isin_checksum"],
            checks_skipped=["nav_consistency"],
            final_status=DocumentStatus.NEEDS_REVIEW,
        )

        rec_id = store.save_validation_result("doc-val-1", val_result)
        assert rec_id > 0

        retrieved = store.get_validation_result("doc-val-1")
        assert retrieved is not None
        assert retrieved.final_status == DocumentStatus.NEEDS_REVIEW
        assert retrieved.passed is False
        assert len(retrieved.findings) == 2
        assert retrieved.findings[0].check == "cash_reconciliation"
        assert retrieved.findings[0].severity == ValidationSeverity.ERROR
        assert retrieved.checks_run == ["cash_reconciliation", "check_portfolio_weights", "isin_checksum"]
        assert retrieved.checks_skipped == ["nav_consistency"]

    def test_list_multiple_validation_records(self, store):
        doc = DocumentRecord(id="doc-val-multi", file_path="/p.pdf", file_hash="hmulti")
        store.create_document(doc)

        v1 = ValidationResult(findings=[], checks_run=["check1"], final_status=DocumentStatus.PARSED)
        v2 = ValidationResult(
            findings=[ValidationFinding(check="c2", severity=ValidationSeverity.ERROR, message="err")],
            checks_run=["check1", "c2"],
            final_status=DocumentStatus.NEEDS_REVIEW,
        )

        store.save_validation_result("doc-val-multi", v1)
        store.save_validation_result("doc-val-multi", v2)

        history = store.list_validation_records("doc-val-multi")
        assert len(history) == 2
        assert history[0].final_status == DocumentStatus.PARSED
        assert history[1].final_status == DocumentStatus.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 5. LLM Call Ledger Persistence
# ---------------------------------------------------------------------------

class TestLLMCallLedgerPersistence:
    def test_record_and_query_llm_calls(self, store):
        doc = DocumentRecord(id="doc-llm-1", file_path="/p.pdf", file_hash="hllm")
        store.create_document(doc)

        rec1 = LLMCallRecord(
            call_id="call-uuid-1",
            model="gpt-4o-mini",
            prompt_hash="hash111",
            prompt_tokens=500,
            completion_tokens=200,
            total_tokens=700,
            cost_usd=Decimal("0.000195"),
            latency_ms=125.5,
            success=True,
            metadata={"step": "initial_spec"},
        )

        rec2 = LLMCallRecord(
            call_id="call-uuid-2",
            model="gpt-4o-mini",
            prompt_hash="hash222",
            prompt_tokens=600,
            completion_tokens=250,
            total_tokens=850,
            cost_usd=Decimal("0.000240"),
            latency_ms=140.0,
            success=True,
            metadata={"step": "repair_spec"},
        )

        store.record_llm_call(rec1, doc_id="doc-llm-1")
        store.record_llm_call(rec2, doc_id="doc-llm-1")

        calls = store.list_llm_calls(doc_id="doc-llm-1")
        assert len(calls) == 2
        assert calls[0].call_id == "call-uuid-2"  # Ordered DESC by timestamp
        assert calls[1].call_id == "call-uuid-1"

        total_cost = store.get_total_llm_cost(doc_id="doc-llm-1")
        assert total_cost == Decimal("0.000435")


# ---------------------------------------------------------------------------
# 6. Processing Attempt History
# ---------------------------------------------------------------------------

class TestProcessingAttemptHistory:
    def test_processing_attempts_increment_and_audit(self, store):
        doc = DocumentRecord(id="doc-hist-1", file_path="/p.pdf", file_hash="hhist")
        store.create_document(doc)

        # Attempt 1: Drift suspected
        a1 = store.record_processing_attempt(
            "doc-hist-1",
            status=DocumentStatus.DRIFT_SUSPECTED,
            format_id="format_a",
            warnings=["Fingerprint near-match: 0.72"],
        )
        assert a1.attempt_number == 1
        assert a1.status == DocumentStatus.DRIFT_SUSPECTED

        # Attempt 2: Learning failed
        a2 = store.record_processing_attempt(
            "doc-hist-1",
            status=DocumentStatus.LEARNING_FAILED,
            format_id="format_a_drift",
            error_message="Repairs exhausted",
        )
        assert a2.attempt_number == 2
        assert a2.status == DocumentStatus.LEARNING_FAILED

        # Attempt 3: Reparse succeeded after spec approval
        a3 = store.record_processing_attempt(
            "doc-hist-1",
            status=DocumentStatus.PARSED,
            format_id="format_a_v2",
        )
        assert a3.attempt_number == 3
        assert a3.status == DocumentStatus.PARSED

        history = store.list_processing_attempts("doc-hist-1")
        assert len(history) == 3
        assert [h.attempt_number for h in history] == [1, 2, 3]
        assert history[0].status == DocumentStatus.DRIFT_SUSPECTED
        assert history[1].error_message == "Repairs exhausted"
        assert history[2].status == DocumentStatus.PARSED


# ---------------------------------------------------------------------------
# 7. Transaction Rollback & Error Handling
# ---------------------------------------------------------------------------

class TestTransactionRollback:
    def test_transaction_rollback_on_failure(self, store):
        doc = DocumentRecord(id="doc-tx-1", file_path="/p.pdf", file_hash="htx1")
        store.create_document(doc)

        # Intentionally cause a foreign key constraint or table error inside transaction
        with pytest.raises(sqlite3.IntegrityError):
            with store._get_connection() as conn:
                # Insert valid attempt
                conn.execute(
                    "INSERT INTO processing_attempts (document_id, attempt_number, status, created_at) VALUES (?, ?, ?, ?)",
                    ("doc-tx-1", 1, DocumentStatus.PARSED.value, datetime.now(timezone.utc).isoformat()),
                )
                # Intentionally insert invalid foreign key into statements
                conn.execute(
                    "INSERT INTO statements (document_id, statement_type, statement_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("non-existent-doc-id", "PMS", "{}", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
                )

        # Rollback should ensure attempt was NOT saved
        attempts = store.list_processing_attempts("doc-tx-1")
        assert len(attempts) == 0
