"""Tests for Gmail IMAP ingestion adapter and CLI integration."""
from __future__ import annotations

from pathlib import Path
from email.message import EmailMessage
import imaplib
import io
import socket
from unittest.mock import MagicMock, patch
import pytest

from src.cli.cli import main, process_gmail
from src.ingestion.email import EmailIngester
from src.ingestion.exceptions import (
    IMAPAuthError,
    IMAPConnectionError,
    IMAPFetchError,
)
from src.ingestion.folder import PipelineInterface
from src.ingestion.gmail import (
    GmailIMAPClient,
    GmailIngester,
    ingest_gmail,
)
from src.ingestion.models import (
    EmailIngestionResult,
    GmailIngestionConfig,
)
from src.models.enums import DocumentStatus
from src.pipeline.models import PipelineResult


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

def _build_mime_email(
    subject: str = "Investment Statement",
    sender: str = "wealth@kotak.com",
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    """Build a raw RFC-822 formatted MIME email."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "investor@example.com"
    msg.set_content("Please find attached your investment portfolio statement.")
    if attachments:
        for filename, content, ctype in attachments:
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


class MockIMAPServer:
    """In-memory mock for imaplib.IMAP4_SSL to avoid real network calls."""

    def __init__(self, host="imap.gmail.com", port=993, timeout=30.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in_user: str | None = None
        self.selected_mailbox: str | None = None
        self.is_closed = False
        self.is_logged_out = False
        self.messages: dict[bytes, bytes] = {}

    def login(self, username, password):
        if password == "bad_password":
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in_user = username
        return "OK", [b"Success"]

    def select(self, mailbox="INBOX", readonly=True):
        if mailbox == "NONEXISTENT":
            return "NO", [b"Mailbox does not exist"]
        self.selected_mailbox = mailbox
        return "OK", [b"10"]

    def search(self, charset, criteria):
        if criteria == "INVALID_CRITERIA":
            return "NO", [b"Invalid search criteria"]
        if not self.messages:
            return "OK", [b""]
        # Return sorted message sequence numbers
        keys = sorted(self.messages.keys(), key=lambda k: int(k))
        return "OK", [b" ".join(keys)]

    def fetch(self, num_bytes, query):
        if num_bytes in self.messages:
            raw = self.messages[num_bytes]
            return "OK", [(b"%s (RFC822 {%d}" % (num_bytes, len(raw)), raw), b")"]
        return "NO", [b"Message not found"]

    def close(self):
        self.is_closed = True
        return "OK", [b"Closed"]

    def logout(self):
        self.is_logged_out = True
        return "BYE", [b"Logged out"]


def _make_dummy_pipeline_result(file_path: str | Path, status: DocumentStatus = DocumentStatus.PARSED) -> PipelineResult:
    return PipelineResult(
        document_id="doc_test_123",
        status=status,
        file_path=str(file_path),
        file_hash="hash_test_123",
        errors=[],
        warnings=[],
    )


# ---------------------------------------------------------------------------
# 1. Configuration & Security Tests
# ---------------------------------------------------------------------------

class TestGmailIngestionConfig:
    def test_default_config(self):
        cfg = GmailIngestionConfig()
        assert cfg.host == "imap.gmail.com"
        assert cfg.port == 993
        assert cfg.mailbox == "INBOX"
        assert cfg.search_criteria == "ALL"
        assert cfg.limit is None
        assert cfg.username is None
        assert cfg.password is None

    def test_config_from_env(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "investor@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret_app_password_1234")
        monkeypatch.setenv("GMAIL_MAILBOX", "Statements")
        monkeypatch.setenv("GMAIL_SEARCH_CRITERIA", "UNSEEN")
        monkeypatch.setenv("GMAIL_LIMIT", "5")

        cfg = GmailIngestionConfig.from_env()
        assert cfg.username == "investor@gmail.com"
        assert cfg.password == "secret_app_password_1234"
        assert cfg.mailbox == "Statements"
        assert cfg.search_criteria == "UNSEEN"
        assert cfg.limit == 5

    def test_password_is_never_leaked_in_repr(self):
        cfg = GmailIngestionConfig(
            username="investor@gmail.com",
            password="ultra_secret_app_password",
        )
        repr_str = repr(cfg)
        assert "ultra_secret_app_password" not in repr_str
        assert "***" in repr_str
        assert "investor@gmail.com" in repr_str


# ---------------------------------------------------------------------------
# 2. Connection, Authentication & Lifecycle Tests
# ---------------------------------------------------------------------------

class TestGmailIMAPClientLifecycle:
    def test_missing_credentials_raises_auth_error(self):
        cfg = GmailIngestionConfig(username=None, password=None)
        client = GmailIMAPClient(cfg)
        with pytest.raises(IMAPAuthError, match="Gmail credentials not provided"):
            client.connect()

    def test_network_connection_error_raises_imap_connection_error(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="pwd")

        def failing_factory(*args, **kwargs):
            raise socket.error("Connection refused")

        client = GmailIMAPClient(cfg, client_factory=failing_factory)
        with pytest.raises(IMAPConnectionError, match="Failed to connect"):
            client.connect()

    def test_auth_failure_raises_imap_auth_error_without_leaking_password(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="bad_password")
        mock_server = MockIMAPServer()
        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)

        with pytest.raises(IMAPAuthError) as exc_info:
            client.connect()

        err_msg = str(exc_info.value)
        assert "Authentication failed" in err_msg
        assert "bad_password" not in err_msg
        assert "test@gmail.com" in err_msg

    def test_context_manager_always_closes_and_logouts(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="good_password")
        mock_server = MockIMAPServer()
        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)

        with client:
            assert client._client is not None
            assert mock_server.logged_in_user == "test@gmail.com"

        assert mock_server.is_closed is True
        assert mock_server.is_logged_out is True
        assert client._client is None

    def test_context_manager_closes_on_exception(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="good_password")
        mock_server = MockIMAPServer()
        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)

        with pytest.raises(ValueError, match="Boom"):
            with client:
                raise ValueError("Boom")

        assert mock_server.is_closed is True
        assert mock_server.is_logged_out is True


# ---------------------------------------------------------------------------
# 3. Mailbox Selection, Search & Limits
# ---------------------------------------------------------------------------

class TestGmailIMAPMailboxOperations:
    def test_select_mailbox_success(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="pwd", mailbox="INBOX")
        mock_server = MockIMAPServer()
        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)

        with client:
            msgs = client.fetch_raw_messages()
            assert msgs == []
            assert mock_server.selected_mailbox == "INBOX"

    def test_select_mailbox_failure_raises_imap_fetch_error(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="pwd", mailbox="NONEXISTENT")
        mock_server = MockIMAPServer()
        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)

        with pytest.raises(IMAPFetchError, match="Failed to select mailbox"):
            with client:
                client.fetch_raw_messages()

    def test_search_failure_raises_imap_fetch_error(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="pwd", search_criteria="INVALID_CRITERIA")
        mock_server = MockIMAPServer()
        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)

        with pytest.raises(IMAPFetchError, match="Search failed in mailbox"):
            with client:
                client.fetch_raw_messages()

    def test_message_limit_fetches_newest_messages(self):
        cfg = GmailIngestionConfig(username="test@gmail.com", password="pwd", limit=2)
        mock_server = MockIMAPServer()
        # Seed 5 messages: 1, 2, 3, 4, 5
        for i in range(1, 6):
            mock_server.messages[str(i).encode()] = _build_mime_email(subject=f"Statement {i}")

        client = GmailIMAPClient(cfg, client_factory=lambda *a, **k: mock_server)
        with client:
            msgs = client.fetch_raw_messages()

        assert len(msgs) == 2
        # Fetched the last 2 messages (4 and 5)
        msg_ids = [m[0] for m in msgs]
        assert msg_ids == ["4", "5"]


# ---------------------------------------------------------------------------
# 4. Ingestion Pipeline Delegation & Attachment Processing
# ---------------------------------------------------------------------------

class TestGmailIngestionPipelineDelegation:
    def test_valid_pdf_attachment_processed_by_pipeline(self):
        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_dummy_pipeline_result(f)

        email_bytes = _build_mime_email(
            attachments=[("pms_statement.pdf", b"%PDF-1.4 statement content", "application/pdf")]
        )
        mock_server = MockIMAPServer()
        mock_server.messages[b"1"] = email_bytes

        cfg = GmailIngestionConfig(username="user@gmail.com", password="pwd")
        ingester = GmailIngester(
            pipeline=mock_pipeline,
            config=cfg,
            client_factory=lambda *a, **k: mock_server,
        )

        res = ingester.ingest()
        assert isinstance(res, EmailIngestionResult)
        assert res.total_emails == 1
        assert res.total_discovered == 1
        assert res.successful == 1
        assert res.failed == 0
        assert mock_pipeline.process_file.call_count == 1

    def test_non_pdf_attachments_are_skipped(self):
        mock_pipeline = MagicMock(spec=PipelineInterface)
        email_bytes = _build_mime_email(
            attachments=[
                ("logo.png", b"PNG_FAKE_DATA", "image/png"),
                ("terms.txt", b"Plain text terms", "text/plain"),
            ]
        )
        mock_server = MockIMAPServer()
        mock_server.messages[b"1"] = email_bytes

        cfg = GmailIngestionConfig(username="user@gmail.com", password="pwd")
        ingester = GmailIngester(
            pipeline=mock_pipeline,
            config=cfg,
            client_factory=lambda *a, **k: mock_server,
        )

        res = ingester.ingest()
        assert res.total_emails == 1
        assert res.total_discovered == 0
        assert res.skipped == 2
        assert mock_pipeline.process_file.call_count == 0

    def test_candidate_password_forwarded_to_pipeline(self):
        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_dummy_pipeline_result(f)

        email_bytes = _build_mime_email(
            attachments=[("encrypted.pdf", b"%PDF-1.4 encrypted", "application/pdf")]
        )
        mock_server = MockIMAPServer()
        mock_server.messages[b"1"] = email_bytes

        cfg = GmailIngestionConfig(
            username="user@gmail.com",
            password="pwd",
            passwords=["secret_pms_password"],
        )
        ingester = GmailIngester(
            pipeline=mock_pipeline,
            config=cfg,
            client_factory=lambda *a, **k: mock_server,
        )

        res = ingester.ingest()
        assert res.successful == 1
        call_kwargs = mock_pipeline.process_file.call_args[1]
        assert call_kwargs.get("password") == "secret_pms_password"

    def test_convenience_function_ingest_gmail(self):
        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_dummy_pipeline_result(f)

        email_bytes = _build_mime_email(
            attachments=[("report.pdf", b"%PDF-1.4 content", "application/pdf")]
        )
        mock_server = MockIMAPServer()
        mock_server.messages[b"1"] = email_bytes

        cfg = GmailIngestionConfig(username="user@gmail.com", password="pwd")
        res = ingest_gmail(
            pipeline=mock_pipeline,
            config=cfg,
            client_factory=lambda *a, **k: mock_server,
        )
        assert res.successful == 1


# ---------------------------------------------------------------------------
# 5. CLI Integration Tests
# ---------------------------------------------------------------------------

class TestGmailCLIIntegration:
    def test_cli_gmail_invokes_ingestion_successfully(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "cli_user@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "cli_secret_pwd")

        mock_server = MockIMAPServer()
        mock_server.messages[b"1"] = _build_mime_email(
            attachments=[("cli_statement.pdf", b"%PDF-1.4 statement", "application/pdf")]
        )

        mock_pipeline = MagicMock(spec=PipelineInterface)
        mock_pipeline.process_file.side_effect = lambda f, password=None: _make_dummy_pipeline_result(f)

        cfg = GmailIngestionConfig.from_env()
        mock_gmail_ingester = GmailIngester(
            pipeline=mock_pipeline,
            config=cfg,
            client_factory=lambda *a, **k: mock_server,
        )

        out_buf = io.StringIO()
        err_buf = io.StringIO()

        code = main(
            argv=["--gmail"],
            pipeline=mock_pipeline,
            gmail_ingester=mock_gmail_ingester,
            out=out_buf,
            err=err_buf,
        )

        assert code == 0
        output = out_buf.getvalue()
        assert "Mailbox: INBOX" in output
        assert "Emails Processed: 1" in output
        assert "Successful: 1" in output

    def test_cli_gmail_missing_credentials_fails_gracefully(self, monkeypatch):
        monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        out_buf = io.StringIO()
        err_buf = io.StringIO()

        code = main(
            argv=["--gmail"],
            out=out_buf,
            err=err_buf,
        )

        assert code == 1
        err_output = err_buf.getvalue()
        assert "GMAIL_ADDRESS and GMAIL_APP_PASSWORD" in err_output

    def test_cli_gmail_conflicting_with_folder_returns_usage_error(self):
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        code = main(
            argv=["--gmail", "--folder", "/tmp/statements"],
            out=out_buf,
            err=err_buf,
        )

        assert code == 2
        assert "Cannot specify both --gmail and a document path or --folder" in err_buf.getvalue()

    def test_cli_gmail_conflicting_with_file_path_returns_usage_error(self):
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        code = main(
            argv=["statement.pdf", "--gmail"],
            out=out_buf,
            err=err_buf,
        )

        assert code == 2
        assert "Cannot specify both --gmail and a document path or --folder" in err_buf.getvalue()
