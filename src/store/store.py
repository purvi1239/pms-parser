"""StatementStore: Persistent SQLite storage for documents, statements, validations, and audit logs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Generator

from src.fingerprint.models import FormatFingerprint
from src.llm_adapter.models import LLMCallRecord
from src.models.enums import DocumentStatus, StatementType
from src.models.models import ParseResult, Statement
from src.store.exceptions import (
    DocumentNotFoundError,
    DuplicateDocumentError,
    StatementNotFoundError,
    StoreError,
)
from src.store.models import (
    DocumentRecord,
    ProcessingAttemptRecord,
    ValidationRecord,
)
from src.store.schema import initialize_schema
from src.validator.models import ValidationFinding, ValidationResult


def get_default_db_path() -> Path:
    """Return default SQLite database path (<project_root>/data/pms_parser.db)."""
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "data" / "pms_parser.db"


class StatementStore:
    """Persistent storage for document processing states, parsed statements, validation results, and LLM calls."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Initialize StatementStore.

        Args:
            db_path: Path to SQLite file, ':memory:' for transient storage, or None for default.
        """
        if db_path is None:
            self.db_path: Path | str = get_default_db_path()
            self.is_memory = False
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif str(db_path) == ":memory:":
            self.db_path = ":memory:"
            self.is_memory = True
        else:
            self.db_path = Path(db_path).resolve()
            self.is_memory = False
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._mem_conn: sqlite3.Connection | None = None
        if self.is_memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            initialize_schema(self._mem_conn)
        else:
            with self._get_connection() as conn:
                initialize_schema(conn)

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager providing an active SQLite connection with transaction management."""
        if self.is_memory and self._mem_conn is not None:
            conn = self._mem_conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def close(self) -> None:
        """Close persistent memory connection if open."""
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    # -----------------------------------------------------------------------
    # Document Operations
    # -----------------------------------------------------------------------

    def create_document(self, record: DocumentRecord) -> DocumentRecord:
        """Insert a new document record.

        Raises:
            DuplicateDocumentError: If document ID already exists.
        """
        fp_json = record.fingerprint.model_dump_json() if record.fingerprint else None
        created_str = record.created_at.isoformat()
        updated_str = record.updated_at.isoformat()
        warn_json = json.dumps(record.warnings)
        err_json = json.dumps(record.errors)
        meta_json = json.dumps(record.raw_metadata)
        st_type = record.statement_type.value if record.statement_type else None

        with self._get_connection() as conn:
            # Check duplicate ID
            cur = conn.execute("SELECT id FROM documents WHERE id = ?", (record.id,))
            if cur.fetchone():
                raise DuplicateDocumentError(f"Document with ID '{record.id}' already exists.")

            conn.execute(
                """
                INSERT INTO documents (
                    id, file_path, file_hash, status, statement_type, format_id,
                    spec_version_id, fingerprint, created_at, updated_at,
                    warnings, errors, raw_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.file_path,
                    record.file_hash,
                    record.status.value,
                    st_type,
                    record.format_id,
                    record.spec_version_id,
                    fp_json,
                    created_str,
                    updated_str,
                    warn_json,
                    err_json,
                    meta_json,
                ),
            )
        return record

    def get_document(self, doc_id: str) -> DocumentRecord | None:
        """Retrieve a document record by ID, or None if not found."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_document_record(row)

    def get_document_or_raise(self, doc_id: str) -> DocumentRecord:
        """Retrieve a document record by ID, or raise DocumentNotFoundError."""
        doc = self.get_document(doc_id)
        if doc is None:
            raise DocumentNotFoundError(f"Document with ID '{doc_id}' not found.")
        return doc

    def find_by_file_hash(self, file_hash: str) -> DocumentRecord | None:
        """Find an existing document by its content SHA-256 hash."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM documents WHERE file_hash = ? ORDER BY created_at ASC LIMIT 1",
                (file_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_document_record(row)

    def update_status(
        self,
        doc_id: str,
        status: DocumentStatus,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> DocumentRecord:
        """Update processing status and optionally errors/warnings."""
        doc = self.get_document_or_raise(doc_id)
        now_str = datetime.now(timezone.utc).isoformat()

        updated_errors = errors if errors is not None else doc.errors
        updated_warnings = warnings if warnings is not None else doc.warnings

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = ?, errors = ?, warnings = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    json.dumps(updated_errors),
                    json.dumps(updated_warnings),
                    now_str,
                    doc_id,
                ),
            )

        return self.get_document_or_raise(doc_id)

    def update_document(
        self,
        doc_id: str,
        status: DocumentStatus | None = None,
        statement_type: StatementType | None = None,
        format_id: str | None = None,
        spec_version_id: str | None = None,
        fingerprint: FormatFingerprint | None = None,
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
        raw_metadata: dict[str, Any] | None = None,
    ) -> DocumentRecord:
        """Update multiple fields of a document record."""
        doc = self.get_document_or_raise(doc_id)
        now_str = datetime.now(timezone.utc).isoformat()

        new_status = status.value if status is not None else doc.status.value
        new_st_type = (
            statement_type.value
            if statement_type is not None
            else (doc.statement_type.value if doc.statement_type else None)
        )
        new_format_id = format_id if format_id is not None else doc.format_id
        new_spec_ver = spec_version_id if spec_version_id is not None else doc.spec_version_id
        new_fp = (
            fingerprint.model_dump_json()
            if fingerprint is not None
            else (doc.fingerprint.model_dump_json() if doc.fingerprint else None)
        )
        new_warns = json.dumps(warnings) if warnings is not None else json.dumps(doc.warnings)
        new_errs = json.dumps(errors) if errors is not None else json.dumps(doc.errors)
        new_meta = json.dumps(raw_metadata) if raw_metadata is not None else json.dumps(doc.raw_metadata)

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = ?, statement_type = ?, format_id = ?, spec_version_id = ?,
                    fingerprint = ?, warnings = ?, errors = ?, raw_metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    new_st_type,
                    new_format_id,
                    new_spec_ver,
                    new_fp,
                    new_warns,
                    new_errs,
                    new_meta,
                    now_str,
                    doc_id,
                ),
            )

        return self.get_document_or_raise(doc_id)

    def list_documents(
        self,
        status: DocumentStatus | None = None,
        format_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        """Query documents with optional filters and pagination."""
        query = "SELECT * FROM documents WHERE 1=1"
        params: list[Any] = []

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if format_id is not None:
            query += " AND format_id = ?"
            params.append(format_id)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            return [self._row_to_document_record(r) for r in cur.fetchall()]

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document record and its cascaded child entries."""
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            return cur.rowcount > 0

    # -----------------------------------------------------------------------
    # Statement Operations
    # -----------------------------------------------------------------------

    def save_statement(self, doc_id: str, statement: Statement) -> None:
        """Persist structured Statement data associated with a document.

        Raises:
            DocumentNotFoundError: If the document record does not exist.
        """
        # Ensure parent document exists
        self.get_document_or_raise(doc_id)

        now_str = datetime.now(timezone.utc).isoformat()
        statement_data = statement.model_dump_json(indent=2)
        st_type = statement.statement_type.value

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO statements (
                    document_id, provider_name, strategy_name, client_identifier,
                    statement_period, currency, statement_type, statement_data,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    provider_name = excluded.provider_name,
                    strategy_name = excluded.strategy_name,
                    client_identifier = excluded.client_identifier,
                    statement_period = excluded.statement_period,
                    currency = excluded.currency,
                    statement_type = excluded.statement_type,
                    statement_data = excluded.statement_data,
                    updated_at = excluded.updated_at
                """,
                (
                    doc_id,
                    statement.provider_name,
                    statement.strategy_name,
                    statement.client_identifier,
                    statement.statement_period,
                    statement.currency,
                    st_type,
                    statement_data,
                    now_str,
                    now_str,
                ),
            )

    def get_statement(self, doc_id: str) -> Statement | None:
        """Retrieve the parsed Statement for a document, or None if absent."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT statement_data FROM statements WHERE document_id = ?",
                (doc_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return Statement.model_validate_json(row["statement_data"])

    def get_statement_or_raise(self, doc_id: str) -> Statement:
        """Retrieve the parsed Statement for a document, or raise StatementNotFoundError."""
        st = self.get_statement(doc_id)
        if st is None:
            raise StatementNotFoundError(f"Statement data not found for document ID '{doc_id}'.")
        return st

    def save_parse_result(
        self,
        doc_id: str,
        parse_result: ParseResult,
        format_id: str | None = None,
    ) -> None:
        """Atomically persist statement and update document status and warnings/errors."""
        # Ensure document exists
        self.get_document_or_raise(doc_id)

        with self._get_connection() as conn:
            now_str = datetime.now(timezone.utc).isoformat()

            # 1. Update document
            doc_updates: list[str] = [
                "status = ?",
                "warnings = ?",
                "errors = ?",
                "updated_at = ?",
            ]
            params: list[Any] = [
                parse_result.status.value,
                json.dumps(parse_result.warnings),
                json.dumps(parse_result.errors),
                now_str,
            ]
            if format_id is not None:
                doc_updates.append("format_id = ?")
                params.append(format_id)

            if parse_result.statement:
                doc_updates.append("statement_type = ?")
                params.append(parse_result.statement.statement_type.value)

            params.append(doc_id)
            conn.execute(
                f"UPDATE documents SET {', '.join(doc_updates)} WHERE id = ?",
                params,
            )

            # 2. Save statement if present
            if parse_result.statement:
                st = parse_result.statement
                conn.execute(
                    """
                    INSERT INTO statements (
                        document_id, provider_name, strategy_name, client_identifier,
                        statement_period, currency, statement_type, statement_data,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        provider_name = excluded.provider_name,
                        strategy_name = excluded.strategy_name,
                        client_identifier = excluded.client_identifier,
                        statement_period = excluded.statement_period,
                        currency = excluded.currency,
                        statement_type = excluded.statement_type,
                        statement_data = excluded.statement_data,
                        updated_at = excluded.updated_at
                    """,
                    (
                        doc_id,
                        st.provider_name,
                        st.strategy_name,
                        st.client_identifier,
                        st.statement_period,
                        st.currency,
                        st.statement_type.value,
                        st.model_dump_json(indent=2),
                        now_str,
                        now_str,
                    ),
                )

    def export_statement_dict(self, doc_id: str) -> dict[str, Any]:
        """Export the parsed Statement as a plain JSON-compatible dictionary."""
        st = self.get_statement_or_raise(doc_id)
        return json.loads(st.model_dump_json())

    def export_statement_json(self, doc_id: str, indent: int = 2) -> str:
        """Export the parsed Statement as formatted JSON."""
        st = self.get_statement_or_raise(doc_id)
        return st.model_dump_json(indent=indent)

    # -----------------------------------------------------------------------
    # Validation Result Operations
    # -----------------------------------------------------------------------

    def save_validation_result(self, doc_id: str, val_result: ValidationResult) -> int:
        """Persist a ValidationResult for a document.

        Returns:
            The auto-incremented record ID.
        """
        self.get_document_or_raise(doc_id)

        now_str = datetime.now(timezone.utc).isoformat()
        checks_run_json = json.dumps(val_result.checks_run)
        checks_skip_json = json.dumps(val_result.checks_skipped)
        findings_json = json.dumps([f.model_dump() for f in val_result.findings])
        passed_int = 1 if val_result.passed else 0

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO validation_results (
                    document_id, final_status, passed, checks_run,
                    checks_skipped, findings, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    val_result.final_status.value,
                    passed_int,
                    checks_run_json,
                    checks_skip_json,
                    findings_json,
                    now_str,
                ),
            )
            return cur.lastrowid or 0

    def get_validation_result(self, doc_id: str) -> ValidationResult | None:
        """Retrieve the most recent ValidationResult for a document."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM validation_results
                WHERE document_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (doc_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            findings_list = [ValidationFinding(**f) for f in json.loads(row["findings"])]
            return ValidationResult(
                findings=findings_list,
                checks_run=json.loads(row["checks_run"]),
                checks_skipped=json.loads(row["checks_skipped"]),
                final_status=DocumentStatus(row["final_status"]),
            )

    def list_validation_records(self, doc_id: str) -> list[ValidationRecord]:
        """Retrieve all validation records for a document."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM validation_results
                WHERE document_id = ?
                ORDER BY id ASC
                """,
                (doc_id,),
            )
            records: list[ValidationRecord] = []
            for row in cur.fetchall():
                findings_list = [ValidationFinding(**f) for f in json.loads(row["findings"])]
                records.append(
                    ValidationRecord(
                        id=row["id"],
                        document_id=row["document_id"],
                        final_status=DocumentStatus(row["final_status"]),
                        passed=bool(row["passed"]),
                        checks_run=json.loads(row["checks_run"]),
                        checks_skipped=json.loads(row["checks_skipped"]),
                        findings=findings_list,
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                )
            return records

    # -----------------------------------------------------------------------
    # LLM Call Ledger Operations
    # -----------------------------------------------------------------------

    def record_llm_call(self, record: LLMCallRecord, doc_id: str | None = None) -> None:
        """Persist an LLM call record in the SQLite ledger."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_calls (
                    call_id, document_id, timestamp, model, prompt_hash,
                    prompt_tokens, completion_tokens, total_tokens, cost_usd,
                    latency_ms, success, error_message, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO NOTHING
                """,
                (
                    record.call_id,
                    doc_id,
                    record.timestamp.isoformat(),
                    record.model,
                    record.prompt_hash,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.total_tokens,
                    str(record.cost_usd),
                    record.latency_ms,
                    1 if record.success else 0,
                    record.error_message,
                    json.dumps(record.metadata),
                ),
            )

    def list_llm_calls(
        self,
        doc_id: str | None = None,
        limit: int = 100,
    ) -> list[LLMCallRecord]:
        """Retrieve LLM call records with optional document filter."""
        query = "SELECT * FROM llm_calls WHERE 1=1"
        params: list[Any] = []
        if doc_id is not None:
            query += " AND document_id = ?"
            params.append(doc_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            calls: list[LLMCallRecord] = []
            for row in cur.fetchall():
                calls.append(
                    LLMCallRecord(
                        call_id=row["call_id"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        model=row["model"],
                        prompt_hash=row["prompt_hash"],
                        prompt_tokens=row["prompt_tokens"],
                        completion_tokens=row["completion_tokens"],
                        total_tokens=row["total_tokens"],
                        cost_usd=Decimal(row["cost_usd"]),
                        latency_ms=row["latency_ms"],
                        success=bool(row["success"]),
                        error_message=row["error_message"],
                        metadata=json.loads(row["metadata"]),
                    )
                )
            return calls

    def get_total_llm_cost(self, doc_id: str | None = None) -> Decimal:
        """Calculate cumulative LLM cost in USD."""
        query = "SELECT cost_usd FROM llm_calls WHERE 1=1"
        params: list[Any] = []
        if doc_id is not None:
            query += " AND document_id = ?"
            params.append(doc_id)

        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            return sum((Decimal(row["cost_usd"]) for row in cur.fetchall()), Decimal("0.000000"))

    # -----------------------------------------------------------------------
    # Processing Attempt History
    # -----------------------------------------------------------------------

    def record_processing_attempt(
        self,
        doc_id: str,
        status: DocumentStatus,
        format_id: str | None = None,
        error_message: str | None = None,
        warnings: list[str] | None = None,
    ) -> ProcessingAttemptRecord:
        """Record an attempt entry tracking repeated processing or reparse runs."""
        self.get_document_or_raise(doc_id)

        with self._get_connection() as conn:
            # Determine next attempt number
            cur = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM processing_attempts WHERE document_id = ?",
                (doc_id,),
            )
            next_num = cur.fetchone()[0]

            now_str = datetime.now(timezone.utc).isoformat()
            warn_json = json.dumps(warnings or [])

            cur_ins = conn.execute(
                """
                INSERT INTO processing_attempts (
                    document_id, attempt_number, status, format_id,
                    error_message, warnings, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    next_num,
                    status.value,
                    format_id,
                    error_message,
                    warn_json,
                    now_str,
                ),
            )
            attempt_id = cur_ins.lastrowid or 0

            return ProcessingAttemptRecord(
                id=attempt_id,
                document_id=doc_id,
                attempt_number=next_num,
                status=status,
                format_id=format_id,
                error_message=error_message,
                warnings=warnings or [],
                created_at=datetime.fromisoformat(now_str),
            )

    def list_processing_attempts(self, doc_id: str) -> list[ProcessingAttemptRecord]:
        """Retrieve full processing history for a document in chronological order."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM processing_attempts
                WHERE document_id = ?
                ORDER BY attempt_number ASC
                """,
                (doc_id,),
            )
            attempts: list[ProcessingAttemptRecord] = []
            for row in cur.fetchall():
                attempts.append(
                    ProcessingAttemptRecord(
                        id=row["id"],
                        document_id=row["document_id"],
                        attempt_number=row["attempt_number"],
                        status=DocumentStatus(row["status"]),
                        format_id=row["format_id"],
                        error_message=row["error_message"],
                        warnings=json.loads(row["warnings"]),
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                )
            return attempts

    # -----------------------------------------------------------------------
    # Internal Helpers
    # -----------------------------------------------------------------------

    def _row_to_document_record(self, row: sqlite3.Row) -> DocumentRecord:
        """Convert a database row into a DocumentRecord domain model."""
        fp_data = json.loads(row["fingerprint"]) if row["fingerprint"] else None
        fingerprint = FormatFingerprint.model_validate(fp_data) if fp_data else None

        st_type = (
            StatementType(row["statement_type"])
            if row["statement_type"]
            else None
        )

        return DocumentRecord(
            id=row["id"],
            file_path=row["file_path"],
            file_hash=row["file_hash"],
            status=DocumentStatus(row["status"]),
            statement_type=st_type,
            format_id=row["format_id"],
            spec_version_id=row["spec_version_id"],
            fingerprint=fingerprint,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            warnings=json.loads(row["warnings"]),
            errors=json.loads(row["errors"]),
            raw_metadata=json.loads(row["raw_metadata"]),
        )
