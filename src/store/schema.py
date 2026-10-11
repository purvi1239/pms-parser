"""Database schema definitions and DDL initialization for SQLite."""
from __future__ import annotations

import sqlite3

from src.store.exceptions import DatabaseSchemaError

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- Documents table: core ingestion and processing state tracking
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    statement_type TEXT,
    format_id TEXT,
    spec_version_id TEXT,
    fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    warnings TEXT NOT NULL DEFAULT '[]',
    errors TEXT NOT NULL DEFAULT '[]',
    raw_metadata TEXT NOT NULL DEFAULT '{}'
);

-- Parsed statements table: structured statement domain models
CREATE TABLE IF NOT EXISTS statements (
    document_id TEXT PRIMARY KEY,
    provider_name TEXT,
    strategy_name TEXT,
    client_identifier TEXT,
    statement_period TEXT,
    currency TEXT,
    statement_type TEXT NOT NULL,
    statement_data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

-- Validation results table: financial and identity check audits
CREATE TABLE IF NOT EXISTS validation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    final_status TEXT NOT NULL,
    passed INTEGER NOT NULL,
    checks_run TEXT NOT NULL DEFAULT '[]',
    checks_skipped TEXT NOT NULL DEFAULT '[]',
    findings TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

-- LLM call ledger table: audit log for tokens, latency, cost, and prompt hashes
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    document_id TEXT,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd TEXT NOT NULL DEFAULT '0.000000',
    latency_ms REAL NOT NULL DEFAULT 0.0,
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE SET NULL
);

-- Processing history: audits repeated processing and reparse attempts
CREATE TABLE IF NOT EXISTS processing_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    format_id TEXT,
    error_message TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

-- Indexes for efficient querying by status, hash, format, and document relationships
CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents(file_hash);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_format_id ON documents(format_id);
CREATE INDEX IF NOT EXISTS idx_statements_client_id ON statements(client_identifier);
CREATE INDEX IF NOT EXISTS idx_validation_results_doc_id ON validation_results(document_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_doc_id ON llm_calls(document_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_timestamp ON llm_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_doc_id ON processing_attempts(document_id);
"""


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Initialize SQLite database tables, constraints, and indexes.

    Args:
        conn: SQLite connection.

    Raises:
        DatabaseSchemaError: If DDL execution fails.
    """
    try:
        conn.executescript(SCHEMA_SQL)
    except sqlite3.Error as exc:
        raise DatabaseSchemaError(f"Failed to initialize database schema: {exc}") from exc
