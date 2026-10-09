"""Rule definitions and keyword dictionaries for document classification."""
from __future__ import annotations

# High-confidence phrases specific to PMS statements
PMS_STRONG_PHRASES = [
    "portfolio management service",
    "portfolio management statement",
    "pms statement",
    "portfolio holdings",
    "client portfolio report",
    "client portfolio",
    "discretionary portfolio",
    "non-discretionary portfolio",
    "pms account",
    "statement type: pms",
    "account type: pms",
]

# High-confidence phrases specific to AIF statements
AIF_STRONG_PHRASES = [
    "alternative investment fund",
    "aif statement",
    "category i aif",
    "category ii aif",
    "category iii aif",
    "fund performance summary",
    "commitment period",
    "drawdown notice",
    "capital call",
    "nav per unit",
    "units held",
    "fund overview",
    "fund category",
]

# Structural statement anchor phrases common to financial statements
STATEMENT_ANCHORS = [
    "statement period",
    "report period",
    "statement of account",
    "holding statement",
    "portfolio summary",
    "fund summary",
    "account summary",
    "client identifier",
    "client id",
    "account number",
    "investor id",
    "closing portfolio value",
    "opening portfolio value",
    "closing value",
    "opening value",
]

# Supporting financial terms (scored in combination, never enough alone)
SUPPORTING_FINANCIAL_TERMS = [
    "holdings",
    "transactions",
    "realised gains",
    "unrealised gains",
    "opening cash",
    "closing cash",
    "fees charged",
    "isin",
    "dividend",
    "market value",
    "purchase cost",
    "units",
]

# Negative indicators suggesting non-statement documents (invoices, resumes, receipts, etc.)
IRRELEVANT_INDICATORS = [
    "tax invoice",
    "bill to",
    "shipping address",
    "curriculum vitae",
    "resume",
    "order confirmation",
    "receipt number",
    "delivery address",
    "hotel reservation",
    "flight itinerary",
    "boarding pass",
]

# Column header keyword matches for tables
HOLDINGS_HEADER_KEYWORDS = {
    "isin", "security", "scrip", "instrument", "quantity", "qty",
    "cost", "mkt value", "market value", "current value", "weight", "allocation"
}

TRANSACTION_HEADER_KEYWORDS = {
    "date", "trade date", "type", "activity", "security", "instrument",
    "qty", "units", "amount", "consideration"
}

SUMMARY_HEADER_KEYWORDS = {
    "opening value", "closing value", "nav", "nav per unit", "units held",
    "opening cash", "closing cash", "fees"
}
