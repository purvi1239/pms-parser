"""Tests for Build Step 2: sample_generator.

Covers:
  1. PDFs are generated successfully (file exists, non-zero size, starts with %PDF).
  2. Ground-truth JSON files are generated.
  3. All expected sample variants exist.
  4. Ground-truth JSON can be loaded.
  5. Financial consistency checks on loaded JSON data.
  6. Encrypted PDF is actually encrypted (PyPDF2 reports it needs a password).
  7. ISIN check digits are valid.
  8. generate_all() is idempotent (re-run produces no error).
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from src.sample_generator import generate_all, ENCRYPTED_SAMPLE_PASSWORD, ENCRYPTED_SAMPLE_LABEL
from src.sample_generator._isin import validate_isin, make_isin, SYNTHETIC_ISINS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sample_dir(tmp_path_factory) -> Path:
    """Generate all samples into a temp directory once per test module."""
    out = tmp_path_factory.mktemp("synthetic")
    generate_all(out_dir=out)
    return out


EXPECTED_LABELS = [
    "format_a1",
    "format_a2",
    "format_b",
    "format_c",
    "format_a_messy",
    "format_a_drift",
    ENCRYPTED_SAMPLE_LABEL,
]


# ---------------------------------------------------------------------------
# 1 & 3. PDFs exist and are valid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", EXPECTED_LABELS)
def test_pdf_exists_and_is_valid(sample_dir, label):
    """Each label produces a non-zero PDF starting with the %%PDF header."""
    pdf = sample_dir / f"{label}.pdf"
    assert pdf.exists(), f"Missing PDF: {pdf}"
    assert pdf.stat().st_size > 0, f"Empty PDF: {pdf}"
    header = pdf.read_bytes()[:5]
    assert header == b"%PDF-", f"Not a PDF (header={header!r}): {pdf}"


# ---------------------------------------------------------------------------
# 2 & 4. Ground-truth JSON exists and is loadable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", EXPECTED_LABELS)
def test_ground_truth_json_exists_and_loadable(sample_dir, label):
    json_path = sample_dir / f"{label}.json"
    assert json_path.exists(), f"Missing JSON: {json_path}"
    assert json_path.stat().st_size > 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "label" in data
    assert "summary" in data
    assert "holdings" in data
    assert "transactions" in data


# ---------------------------------------------------------------------------
# 5. Financial consistency checks
# ---------------------------------------------------------------------------

def _d(s) -> Decimal:
    return Decimal(str(s))


def _load(sample_dir: Path, label: str) -> dict:
    return json.loads((sample_dir / f"{label}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("label", [
    "format_a1", "format_a2", "format_b", "format_c",
    "format_a_messy", "format_a_drift",
])
def test_cash_identity(sample_dir, label):
    """closing_cash = opening_cash + sum(signed transaction amounts)."""
    data = _load(sample_dir, label)
    s = data["summary"]
    opening_cash = _d(s["opening_cash"])
    closing_cash = _d(s["closing_cash"])
    txn_sum = sum(_d(t["amount"]) for t in data["transactions"])
    computed = (opening_cash + txn_sum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert computed == closing_cash.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), (
        f"{label}: cash identity fails: {opening_cash} + {txn_sum} = {computed} ≠ {closing_cash}"
    )


@pytest.mark.parametrize("label", ["format_a1", "format_a2", "format_b", "format_a_messy", "format_a_drift"])
def test_holdings_weights_sum_to_100(sample_dir, label):
    """Holdings weights should sum to approximately 100%."""
    data = _load(sample_dir, label)
    if not data["holdings"]:
        pytest.skip("No holdings to check")
    total_weight = sum(_d(h["weight"]) for h in data["holdings"])
    assert abs(total_weight - _d(100)) <= _d("0.1"), (
        f"{label}: weights sum {total_weight} not ≈ 100%"
    )


def test_aif_nav_identity(sample_dir):
    """AIF: units × nav_per_unit ≈ closing_value (within 1%)."""
    data = _load(sample_dir, "format_c")
    s = data["summary"]
    units = _d(s["units"])
    nav = _d(s["nav_per_unit"])
    closing = _d(s["closing_value"])
    implied = (units * nav).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tol = (closing * _d("0.01")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    diff = abs(implied - closing)
    assert diff <= tol, f"AIF NAV identity fails: {units} × {nav} = {implied}, closing = {closing}"


def test_aif_has_no_holdings(sample_dir):
    """Format C (AIF) must have zero holdings."""
    data = _load(sample_dir, "format_c")
    assert data["holdings"] == [], "AIF should have empty holdings"


@pytest.mark.parametrize("label", ["format_a1", "format_a2", "format_b", "format_a_messy"])
def test_pms_has_holdings(sample_dir, label):
    """PMS formats must have at least one holding."""
    data = _load(sample_dir, label)
    assert len(data["holdings"]) > 0, f"{label} PMS has no holdings"


@pytest.mark.parametrize("label", ["format_a1", "format_a2", "format_b", "format_a_messy"])
def test_holding_isins_are_valid(sample_dir, label):
    """All ISINs in holdings must pass the Luhn check digit validation."""
    data = _load(sample_dir, label)
    for h in data["holdings"]:
        assert validate_isin(h["isin"]), (
            f"{label}: invalid ISIN {h['isin']!r} for {h['security_name']!r}"
        )


@pytest.mark.parametrize("label", ["format_a1", "format_a2", "format_b", "format_a_messy", "format_a_drift"])
def test_transaction_sign_convention(sample_dir, label):
    """Verify sign convention: inflow/sell/dividend → positive; buy/fee/outflow → negative."""
    MUST_BE_POSITIVE = {"inflow", "sell", "dividend"}
    MUST_BE_NEGATIVE = {"buy", "fee", "outflow"}
    data = _load(sample_dir, label)
    for t in data["transactions"]:
        amt = _d(t["amount"])
        txn_type = t["type"]
        if txn_type in MUST_BE_POSITIVE:
            assert amt > 0, f"{label}: {txn_type} amount {amt} should be positive"
        elif txn_type in MUST_BE_NEGATIVE:
            assert amt < 0, f"{label}: {txn_type} amount {amt} should be negative"


# ---------------------------------------------------------------------------
# 6. Encrypted PDF check
# ---------------------------------------------------------------------------

def test_encrypted_pdf_requires_password(sample_dir):
    """The encrypted PDF should report it is encrypted without a password."""
    from pypdf import PdfReader
    enc_pdf = sample_dir / f"{ENCRYPTED_SAMPLE_LABEL}.pdf"
    reader = PdfReader(str(enc_pdf))
    assert reader.is_encrypted, "Encrypted PDF should report is_encrypted=True"
    # Should fail without password
    result = reader.decrypt("")
    assert result.name == "NOT_DECRYPTED"


def test_encrypted_pdf_decrypts_with_correct_password(sample_dir):
    """The encrypted PDF should decrypt successfully with the correct password."""
    from pypdf import PdfReader
    enc_pdf = sample_dir / f"{ENCRYPTED_SAMPLE_LABEL}.pdf"
    reader = PdfReader(str(enc_pdf))
    result = reader.decrypt(ENCRYPTED_SAMPLE_PASSWORD)
    assert result.name != "NOT_DECRYPTED", "Correct password should decrypt the PDF"
    assert len(reader.pages) > 0


# ---------------------------------------------------------------------------
# 7. ISIN utility tests
# ---------------------------------------------------------------------------

def test_make_isin_produces_valid_isins():
    """make_isin() should always produce a 12-char ISIN with a valid check digit."""
    for key, isin in SYNTHETIC_ISINS.items():
        assert len(isin) == 12, f"ISIN {isin!r} for {key} is not 12 chars"
        assert validate_isin(isin), f"Invalid check digit in ISIN {isin!r} for {key}"


def test_validate_isin_rejects_bad_isins():
    known_good = make_isin("IN", "E002A01018")
    # Flip last digit
    bad = known_good[:-1] + str((int(known_good[-1]) + 1) % 10)
    assert not validate_isin(bad), f"Should be invalid: {bad}"
    assert not validate_isin("TOOLONG12345")
    assert not validate_isin("SHORT")


# ---------------------------------------------------------------------------
# 8. Idempotency
# ---------------------------------------------------------------------------

def test_generate_all_idempotent(sample_dir):
    """Re-running generate_all() on the same dir should not raise."""
    result = generate_all(out_dir=sample_dir)
    assert result == sample_dir
    for label in EXPECTED_LABELS:
        assert (sample_dir / f"{label}.pdf").exists()
        assert (sample_dir / f"{label}.json").exists()


# ---------------------------------------------------------------------------
# 9. Format A-messy has more holdings than Format A (multi-page coverage)
# ---------------------------------------------------------------------------

def test_format_a_messy_has_more_holdings_than_a1(sample_dir):
    """A-messy must have more holdings than A1 (multi-page holdings test)."""
    a1_count = len(_load(sample_dir, "format_a1")["holdings"])
    messy_count = len(_load(sample_dir, "format_a_messy")["holdings"])
    assert messy_count > a1_count, (
        f"A-messy should have more holdings ({messy_count}) than A1 ({a1_count})"
    )


def test_format_a_messy_has_wrapped_name(sample_dir):
    """A-messy must contain a security name long enough to require wrapping (>40 chars)."""
    data = _load(sample_dir, "format_a_messy")
    long_names = [h["security_name"] for h in data["holdings"] if len(h["security_name"]) > 40]
    assert long_names, "A-messy should have at least one long (wrapped) security name"
