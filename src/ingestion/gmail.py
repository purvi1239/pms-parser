"""Gmail IMAP ingestion adapter for fetching and parsing statement email attachments."""
from __future__ import annotations

import email
import imaplib
import socket
from typing import Any, Callable, Sequence

from src.ingestion.email import EmailIngester, parse_email_message
from src.ingestion.exceptions import (
    IMAPAuthError,
    IMAPConnectionError,
    IMAPFetchError,
)
from src.ingestion.folder import PipelineInterface
from src.ingestion.models import (
    EmailIngestionConfig,
    EmailIngestionResult,
    EmailMessageInput,
    GmailIngestionConfig,
)


class GmailIMAPClient:
    """Low-level SSL IMAP client for connecting, authenticating, and fetching messages from Gmail."""

    def __init__(
        self,
        config: GmailIngestionConfig,
        client_factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        """Initialize the IMAP client with configuration.

        Args:
            config: GmailIngestionConfig specifying host, port, credentials, and parameters.
            client_factory: Optional factory callable for creating IMAP4_SSL instances (used for testing).
        """
        self.config = config
        self._client_factory = client_factory or imaplib.IMAP4_SSL
        self._client: imaplib.IMAP4_SSL | None = None

    def connect(self) -> imaplib.IMAP4_SSL:
        """Establish SSL connection to IMAP server and authenticate.

        Returns:
            Authenticated imaplib.IMAP4_SSL instance.

        Raises:
            IMAPAuthError: If credentials are missing or authentication is rejected.
            IMAPConnectionError: If network connection or SSL handshake fails.
        """
        if not self.config.username or not self.config.password:
            raise IMAPAuthError(
                "Gmail credentials not provided. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
                "environment variables or pass them explicitly in GmailIngestionConfig."
            )

        try:
            client = self._client_factory(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout,
            )
            self._client = client
        except (socket.error, OSError, TimeoutError) as exc:
            raise IMAPConnectionError(
                f"Failed to connect to IMAP server {self.config.host}:{self.config.port}: {exc}"
            ) from exc
        except Exception as exc:
            raise IMAPConnectionError(
                f"Unexpected error establishing IMAP connection to {self.config.host}:{self.config.port}: {exc}"
            ) from exc

        try:
            typ, data = self._client.login(self.config.username, self.config.password)
            if typ != "OK":
                raise IMAPAuthError(
                    f"Authentication failed for user '{self.config.username}'. "
                    "Verify GMAIL_ADDRESS and GMAIL_APP_PASSWORD."
                )
        except imaplib.IMAP4.error as exc:
            raise IMAPAuthError(
                f"Authentication failed for user '{self.config.username}'. "
                f"Verify GMAIL_ADDRESS and GMAIL_APP_PASSWORD (detail: {exc})."
            ) from exc

        return self._client

    def fetch_raw_messages(
        self,
        mailbox: str | None = None,
        search_criteria: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, bytes]]:
        """Fetch raw RFC-822 message payloads from the selected mailbox.

        Args:
            mailbox: Mailbox folder name (defaults to config.mailbox).
            search_criteria: Search criteria query string (defaults to config.search_criteria).
            limit: Maximum number of recent messages to fetch (defaults to config.limit).

        Returns:
            List of (message_id_str, raw_message_bytes) tuples.

        Raises:
            IMAPFetchError: If mailbox selection or search/fetch operations fail.
        """
        if self._client is None:
            self.connect()

        target_mailbox = mailbox or self.config.mailbox
        try:
            typ, data = self._client.select(target_mailbox, readonly=True)
            if typ != "OK":
                raise IMAPFetchError(
                    f"Failed to select mailbox '{target_mailbox}': {data}"
                )
        except imaplib.IMAP4.error as exc:
            raise IMAPFetchError(
                f"Failed to select mailbox '{target_mailbox}': {exc}"
            ) from exc

        target_criteria = search_criteria or self.config.search_criteria
        try:
            typ, data = self._client.search(None, target_criteria)
            if typ != "OK":
                raise IMAPFetchError(
                    f"Search failed in mailbox '{target_mailbox}' with criteria '{target_criteria}': {data}"
                )
        except imaplib.IMAP4.error as exc:
            raise IMAPFetchError(
                f"Search failed in mailbox '{target_mailbox}' with criteria '{target_criteria}': {exc}"
            ) from exc

        if not data or not data[0]:
            return []

        msg_nums = data[0].split()
        target_limit = limit if limit is not None else self.config.limit
        if target_limit is not None and target_limit > 0:
            # Pick the newest messages (highest sequence numbers in IMAP)
            msg_nums = msg_nums[-target_limit:]

        messages: list[tuple[str, bytes]] = []
        for num_bytes in msg_nums:
            num_str = num_bytes.decode("ascii", errors="replace")
            try:
                typ, fetch_data = self._client.fetch(num_bytes, "(RFC822)")
                if typ != "OK" or not fetch_data:
                    continue

                raw_bytes: bytes | None = None
                for item in fetch_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                        raw_bytes = bytes(item[1])
                        break
                    elif isinstance(item, (bytes, bytearray)):
                        raw_bytes = bytes(item)

                if raw_bytes is not None:
                    messages.append((num_str, raw_bytes))
            except Exception as exc:
                # Log or capture error, but continue fetching other messages
                continue

        return messages

    def close(self) -> None:
        """Safely close and logout from the IMAP session."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> GmailIMAPClient:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class GmailIngester:
    """Ingests statement email attachments from Gmail via IMAP and feeds them to the Pipeline."""

    def __init__(
        self,
        pipeline: PipelineInterface,
        config: GmailIngestionConfig | None = None,
        email_config: EmailIngestionConfig | None = None,
        client_factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        """Initialize GmailIngester with a processing pipeline and configurations.

        Args:
            pipeline: Pipeline or PipelineInterface implementation.
            config: Gmail IMAP connection and query configuration.
            email_config: Email attachment filtering and deduplication configuration.
            client_factory: Optional injectable factory for testing IMAP connections.
        """
        self.pipeline = pipeline
        self.config = config or GmailIngestionConfig.from_env()
        self._email_config = email_config or EmailIngestionConfig(
            passwords=self.config.passwords,
        )
        self.email_ingester = EmailIngester(
            pipeline=self.pipeline,
            config=self._email_config,
        )
        self._client_factory = client_factory

    def fetch_emails(
        self,
        mailbox: str | None = None,
        search_criteria: str | None = None,
        limit: int | None = None,
    ) -> list[EmailMessageInput]:
        """Fetch and parse emails from the configured Gmail mailbox into EmailMessageInput objects.

        Args:
            mailbox: Optional mailbox override.
            search_criteria: Optional IMAP search criterion override.
            limit: Optional message count limit override.

        Returns:
            List of parsed EmailMessageInput instances.
        """
        client = GmailIMAPClient(self.config, client_factory=self._client_factory)
        with client:
            raw_messages = client.fetch_raw_messages(
                mailbox=mailbox,
                search_criteria=search_criteria,
                limit=limit,
            )

        parsed_emails: list[EmailMessageInput] = []
        for num_str, raw_bytes in raw_messages:
            try:
                email_obj = parse_email_message(raw_bytes)
                if not email_obj.email_id:
                    email_obj.email_id = f"gmail_{num_str}"
                parsed_emails.append(email_obj)
            except Exception:
                continue

        return parsed_emails

    def ingest(
        self,
        mailbox: str | None = None,
        search_criteria: str | None = None,
        limit: int | None = None,
        password: str | None = None,
    ) -> EmailIngestionResult:
        """Fetch emails from Gmail mailbox and process their statement PDF attachments.

        Args:
            mailbox: Optional mailbox override.
            search_criteria: Optional search criteria override.
            limit: Optional message count limit override.
            password: Optional candidate password for encrypted PDF statements.

        Returns:
            EmailIngestionResult summarizing discovery and pipeline outcomes.
        """
        emails = self.fetch_emails(
            mailbox=mailbox,
            search_criteria=search_criteria,
            limit=limit,
        )

        if not emails:
            return EmailIngestionResult(total_emails=0)

        effective_password = password
        if effective_password is None and self.config.passwords:
            effective_password = self.config.passwords[0]

        return self.email_ingester.ingest_emails(emails, password=effective_password)


# Aliases
GmailIMAPIngester = GmailIngester


def ingest_gmail(
    pipeline: PipelineInterface,
    config: GmailIngestionConfig | None = None,
    mailbox: str | None = None,
    search_criteria: str | None = None,
    limit: int | None = None,
    password: str | None = None,
    client_factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
) -> EmailIngestionResult:
    """Convenience function to ingest statement attachments from a Gmail mailbox.

    Args:
        pipeline: Pipeline instance or PipelineInterface implementation.
        config: Optional GmailIngestionConfig. Loaded from env if omitted.
        mailbox: Optional mailbox override (default: INBOX).
        search_criteria: Optional search criteria override (default: ALL).
        limit: Optional message count limit.
        password: Optional PDF decryption password.
        client_factory: Optional injectable factory for testing.

    Returns:
        EmailIngestionResult summarizing attachment ingestion outcomes.
    """
    ingester = GmailIngester(
        pipeline=pipeline,
        config=config,
        client_factory=client_factory,
    )
    return ingester.ingest(
        mailbox=mailbox,
        search_criteria=search_criteria,
        limit=limit,
        password=password,
    )
