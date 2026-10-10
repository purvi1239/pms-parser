"""Tests for Step 10: PII Redactor.

Validates detection, masking, repeated values, false positives, custom sensitive values,
empty inputs, and clean financial statement text preservation.
"""
from __future__ import annotations

import pytest

from src.redactor import (
    EntityType,
    RedactionConfig,
    RedactionItem,
    RedactionResult,
    Redactor,
    redact,
)


# ---------------------------------------------------------------------------
# 1. Empty and Clean Text
# ---------------------------------------------------------------------------

class TestEmptyAndCleanText:
    def test_empty_string(self):
        res = redact("")
        assert res.redacted_text == ""
        assert res.total_redactions == 0
        assert res.items == []
        assert res.has_redactions is False

    def test_whitespace_string(self):
        res = redact("   \n\t  ")
        assert res.redacted_text == "   \n\t  "
        assert res.total_redactions == 0
        assert res.has_redactions is False

    def test_clean_financial_headers(self):
        text = "Security Name | ISIN | Quantity | Cost Price | Market Value | Weight %"
        res = redact(text)
        assert res.redacted_text == text
        assert res.total_redactions == 0
        assert res.has_redactions is False


# ---------------------------------------------------------------------------
# 2. Email Address Redaction
# ---------------------------------------------------------------------------

class TestEmailRedaction:
    def test_single_email(self):
        text = "Please reach out to client.support@wealthfirm.co.in for queries."
        res = redact(text)
        assert "[REDACTED_EMAIL]" in res.redacted_text
        assert "client.support@wealthfirm.co.in" not in res.redacted_text
        assert res.counts_by_type.get("email") == 1
        assert res.total_redactions == 1

    def test_multiple_distinct_emails(self):
        text = "Primary: john.doe@example.com, CC: jane_smith+tax@domain.org."
        res = redact(text)
        assert res.counts_by_type.get("email") == 2
        assert "john.doe@example.com" not in res.redacted_text
        assert "jane_smith+tax@domain.org" not in res.redacted_text


# ---------------------------------------------------------------------------
# 3. PAN Identifiers & ISIN Preservation Guard
# ---------------------------------------------------------------------------

class TestPANRedaction:
    def test_valid_pan_masked(self):
        text = "PAN: ABCDE1234F recorded for tax filing."
        res = redact(text)
        assert "PAN: [REDACTED_PAN] recorded for tax filing." == res.redacted_text
        assert res.counts_by_type.get("pan") == 1

    def test_isin_is_never_masked_as_pan(self):
        """ISINs (12 chars like INE002A01018) must never be corrupted as 10-char PANs."""
        text = "Holdings: Reliance (INE002A01018), TCS (INE467B01029), Apple (US0378331005)."
        res = redact(text)
        assert "INE002A01018" in res.redacted_text
        assert "INE467B01029" in res.redacted_text
        assert "US0378331005" in res.redacted_text
        assert res.counts_by_type.get("pan", 0) == 0


# ---------------------------------------------------------------------------
# 4. Indian Phone Numbers & Monetary Values Guard
# ---------------------------------------------------------------------------

class TestPhoneRedaction:
    def test_mobile_numbers(self):
        samples = [
            "Call +91 9876543210 today",
            "Mobile: 98765 43210",
            "Contact: +91-9123456789",
            "Tel: 09876543210",
        ]
        for s in samples:
            res = redact(s)
            assert "[REDACTED_PHONE]" in res.redacted_text
            assert res.counts_by_type.get("phone") == 1

    def test_financial_amounts_not_masked_as_phones(self):
        """Amounts like 10,00,000.00 or 1250000.75 must not trigger phone detection."""
        text = "Closing cash is ₹ 10,00,000.00 and market value is 1250000.75 INR. Total: 9500000.00"
        res = redact(text)
        assert "10,00,000.00" in res.redacted_text
        assert "1250000.75" in res.redacted_text
        assert "9500000.00" in res.redacted_text
        assert res.counts_by_type.get("phone", 0) == 0


# ---------------------------------------------------------------------------
# 5. Aadhaar Identifiers
# ---------------------------------------------------------------------------

class TestAadhaarRedaction:
    def test_formatted_aadhaar_masked(self):
        text = "Aadhaar verified: 5432 1234 5678 on 2026-01-15."
        res = redact(text)
        assert "[REDACTED_AADHAAR]" in res.redacted_text
        assert "5432 1234 5678" not in res.redacted_text
        assert "2026-01-15" in res.redacted_text  # Date preserved

    def test_labeled_unformatted_aadhaar_masked(self):
        text = "UID: 987654321098"
        res = redact(text)
        assert "UID: [REDACTED_AADHAAR]" == res.redacted_text


# ---------------------------------------------------------------------------
# 6. Bank Account Numbers
# ---------------------------------------------------------------------------

class TestBankAccountRedaction:
    def test_labeled_bank_account(self):
        text = "Settlement Bank A/c No: 50100234567890 at HDFC Bank."
        res = redact(text)
        assert "Settlement Bank A/c No: [REDACTED_BANK_ACCOUNT] at HDFC Bank." == res.redacted_text
        assert "50100234567890" not in res.redacted_text
        assert res.counts_by_type.get("bank_account") == 1

    def test_masked_bank_account(self):
        text = "Savings A/c: XXXXXXXX1234 registered."
        res = redact(text)
        assert "Savings A/c: [REDACTED_BANK_ACCOUNT] registered." == res.redacted_text


# ---------------------------------------------------------------------------
# 7. Demat Account Identifiers
# ---------------------------------------------------------------------------

class TestDematAccountRedaction:
    def test_labeled_cdsl_demat(self):
        text = "Demat A/c: 1208160012345678 registered."
        res = redact(text)
        assert "Demat A/c: [REDACTED_DEMAT_ACCOUNT] registered." == res.redacted_text
        assert "1208160012345678" not in res.redacted_text

    def test_labeled_nsdl_dp_id(self):
        text = "DP ID / Client ID: IN30012312345678"
        res = redact(text)
        assert "[REDACTED_DEMAT_ACCOUNT]" in res.redacted_text
        assert "IN30012312345678" not in res.redacted_text

    def test_standalone_nsdl_account(self):
        text = "Securities credited to IN30012312345678 in morning."
        res = redact(text)
        assert "Securities credited to [REDACTED_DEMAT_ACCOUNT] in morning." == res.redacted_text


# ---------------------------------------------------------------------------
# 8. Investor / Client Names
# ---------------------------------------------------------------------------

class TestNameRedaction:
    def test_labeled_investor_name(self):
        text = "Investor Name: Rajesh Kumar Sharma\nStrategy: Growth"
        res = redact(text)
        assert "Investor Name: [REDACTED_NAME]\nStrategy: Growth" == res.redacted_text
        assert "Rajesh Kumar Sharma" not in res.redacted_text
        assert res.counts_by_type.get("name") == 1

    def test_labeled_client_name(self):
        text = "Client Name: Pooja Patel, Account Status: Active"
        res = redact(text)
        assert "Client Name: [REDACTED_NAME], Account Status: Active" == res.redacted_text


# ---------------------------------------------------------------------------
# 9. Caller-Supplied Custom Sensitive Values
# ---------------------------------------------------------------------------

class TestCustomSensitiveValues:
    def test_custom_values_in_call(self):
        text = "Portfolio statement for VIP Client CLI-9876 managed under Alpha High Growth."
        res = redact(text, custom_sensitive_values=["CLI-9876", "VIP Client"])
        assert "CLI-9876" not in res.redacted_text
        assert "VIP Client" not in res.redacted_text
        assert "[REDACTED_CUSTOM]" in res.redacted_text
        assert res.counts_by_type.get("custom") == 2

    def test_custom_values_in_config(self):
        cfg = RedactionConfig(custom_sensitive_values=["SecretFundName", "CLI-0042"])
        redactor = Redactor(cfg)
        text = "Fund: SecretFundName, ID: CLI-0042."
        res = redactor.redact(text)
        assert "SecretFundName" not in res.redacted_text
        assert "CLI-0042" not in res.redacted_text
        assert res.total_redactions == 2


# ---------------------------------------------------------------------------
# 10. Repeated Occurrences & Consistent Masking
# ---------------------------------------------------------------------------

class TestConsistentMasking:
    def test_repeated_values_masked_consistently_standard_tokens(self):
        text = (
            "Contact user@test.com regarding PAN ABCDE1234F. "
            "Confirmation sent to user@test.com for ABCDE1234F."
        )
        res = redact(text)
        expected = (
            "Contact [REDACTED_EMAIL] regarding PAN [REDACTED_PAN]. "
            "Confirmation sent to [REDACTED_EMAIL] for [REDACTED_PAN]."
        )
        assert res.redacted_text == expected
        assert res.counts_by_type.get("email") == 2
        assert res.counts_by_type.get("pan") == 2

    def test_repeated_values_with_indexed_tokens(self):
        cfg = RedactionConfig(use_indexed_tokens=True)
        redactor = Redactor(cfg)
        text = (
            "User 1: user1@test.com, User 2: user2@test.com. "
            "Reminder to user1@test.com."
        )
        res = redactor.redact(text)
        assert "user1@test.com" not in res.redacted_text
        assert "user2@test.com" not in res.redacted_text

        # First email gets index 1 consistently on both occurrences
        assert "[REDACTED_EMAIL_1]" in res.redacted_text
        assert "[REDACTED_EMAIL_2]" in res.redacted_text
        # user1@test.com appeared twice -> [REDACTED_EMAIL_1] should appear twice
        assert res.redacted_text.count("[REDACTED_EMAIL_1]") == 2
        assert res.redacted_text.count("[REDACTED_EMAIL_2]") == 1


# ---------------------------------------------------------------------------
# 11. Security: Zero Sensitive Value Leakage in Metadata
# ---------------------------------------------------------------------------

class TestNoSensitiveValueLeakage:
    def test_metadata_excludes_raw_pii(self):
        raw_email = "supersecret.investor@privatebank.com"
        raw_pan = "ZZZZZ9999Z"
        text = f"Investor: {raw_email}, PAN: {raw_pan}"

        res = redact(text)
        dump_dict = res.model_dump()
        dump_json = res.model_dump_json()

        # Raw values must NEVER appear in serialized metadata
        assert raw_email not in dump_json
        assert raw_pan not in dump_json
        assert raw_email not in str(dump_dict)
        assert raw_pan not in str(dump_dict)

        # Check item metadata fields
        for item in res.items:
            assert hasattr(item, "value_hash")
            assert not hasattr(item, "raw_val")
            assert len(item.value_hash) > 0


# ---------------------------------------------------------------------------
# 12. Configurable Toggles & Token Templates
# ---------------------------------------------------------------------------

class TestConfigurableToggles:
    def test_disable_email_and_pan(self):
        cfg = RedactionConfig(redact_email=False, redact_pan=False)
        text = "Email: test@example.com, PAN: ABCDE1234F, Phone: 9876543210"
        res = redact(text, config=cfg)

        assert "test@example.com" in res.redacted_text  # not redacted
        assert "ABCDE1234F" in res.redacted_text        # not redacted
        assert "[REDACTED_PHONE]" in res.redacted_text   # redacted

    def test_custom_token_templates(self):
        cfg = RedactionConfig(
            token_templates={
                EntityType.PAN: "[PAN_MASKED]",
                EntityType.EMAIL: "[EMAIL_MASKED]",
            }
        )
        text = "PAN ABCDE1234F, Email hello@world.com"
        res = redact(text, config=cfg)
        assert "[PAN_MASKED]" in res.redacted_text
        assert "[EMAIL_MASKED]" in res.redacted_text


# ---------------------------------------------------------------------------
# 13. Realistic PMS Statement Redaction & Financial Value Preservation
# ---------------------------------------------------------------------------

class TestRealisticStatementPreservation:
    def test_statement_redaction_preserves_financials_and_locators(self):
        statement_snippet = """
Pinnacle Wealth PMS
Client Name: Rameshwar Nath Saxena
Client ID: PWM-CLIENT-0042
PAN: BCDPS1234G
Mobile: +91 9820012345
Email: rameshwar.saxena@gmail.com
Bank Account Number: 50100456789012
Demat A/c: 1208160098765432
Statement Period: 01 January 2026 to 31 March 2026

PORTFOLIO HOLDINGS
Security Name                ISIN          Quantity   Cost Price   Market Value   Weight %
Reliance Industries Ltd      INE002A01018       150    330000.00      375000.00      15.50
Tata Consultancy Services    INE467B01029        80    280000.00      312000.00      12.90

TRANSACTIONS
Date          Activity   Security                   Qty      Amount
15/01/2026    BUY        Reliance Industries Ltd     20   -45000.00
20/02/2026    DIVIDEND   Tata Consultancy Services    -     3500.00

SUMMARY
Opening Value: 1000000.00
Closing Value: 1050000.00
Opening Cash:   50000.00
Closing Cash:   15000.00
"""
        res = redact(statement_snippet, custom_sensitive_values=["PWM-CLIENT-0042"])

        redacted = res.redacted_text

        # 1. PII is redacted
        assert "Rameshwar Nath Saxena" not in redacted
        assert "BCDPS1234G" not in redacted
        assert "9820012345" not in redacted
        assert "rameshwar.saxena@gmail.com" not in redacted
        assert "50100456789012" not in redacted
        assert "1208160098765432" not in redacted
        assert "PWM-CLIENT-0042" not in redacted

        # 2. Structural anchors, locators, and headers are PRESERVED
        assert "PORTFOLIO HOLDINGS" in redacted
        assert "Security Name" in redacted
        assert "ISIN" in redacted
        assert "Quantity" in redacted
        assert "Market Value" in redacted
        assert "TRANSACTIONS" in redacted
        assert "Date" in redacted
        assert "Activity" in redacted
        assert "SUMMARY" in redacted
        assert "Opening Value:" in redacted
        assert "Closing Value:" in redacted

        # 3. Financial data and ISINs are PRESERVED
        assert "INE002A01018" in redacted
        assert "INE467B01029" in redacted
        assert "375000.00" in redacted
        assert "312000.00" in redacted
        assert "-45000.00" in redacted
        assert "3500.00" in redacted
        assert "1000000.00" in redacted
        assert "1050000.00" in redacted
        assert "01 January 2026 to 31 March 2026" in redacted
        assert "15/01/2026" in redacted
        assert "15.50" in redacted
