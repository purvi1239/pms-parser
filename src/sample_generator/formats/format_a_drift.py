"""Format A — Drift variant: mutated layout to trigger DRIFT_SUSPECTED.

Deliberate structural differences from Format A:
  1. Title renamed:  "Portfolio Statement Report"  (was "Portfolio Management Statement")
  2. Holdings table column ORDER changed:  ISIN moved to last column
  3. Holdings column header renamed: "Net Value (INR)" instead of "Mkt Value (INR)"
  4. Transactions date format:  DD-Mon-YYYY  (was DD/MM/YYYY)
  5. Summary section heading: "Account Summary" (was "Portfolio Summary")
  6. New extra summary row: "Report Generated" (extra anchor phrase)

These changes will produce different fingerprint features so the fingerprint
module classifies this as DRIFT_SUSPECTED relative to Format A specs.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.platypus import SimpleDocTemplate, Paragraph, Table
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

from src.sample_generator._data import SampleData
from src.sample_generator._pdf import (
    MARGIN, STYLE_TITLE, STYLE_SUBTITLE, STYLE_META, STYLE_SECTION,
    holdings_table_style, transactions_table_style, summary_table_style,
    fmt_inr, fmt_qty, fmt_pct, hr, spacer,
)


def _date_str_drift(d) -> str:
    """DD-Mon-YYYY format — deliberately different from Format A."""
    return d.strftime("%d-%b-%Y")


def build_format_a_drift(data: SampleData, out_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
    )
    story = []

    # ── Header: RENAMED title ────────────────────────────────────────────────
    story.append(Paragraph("Portfolio Statement Report", STYLE_TITLE))   # ← renamed
    story.append(Paragraph(f"{data.provider_name} — {data.strategy_name}", STYLE_SUBTITLE))
    story.append(hr())
    story.append(spacer(4))

    from reportlab.platypus import TableStyle as TS

    meta_rows = [
        ["Client ID:",       data.client_identifier,
         "Statement Period:", data.statement_period],
        ["Currency:",        data.currency,
         "Statement Type:",  data.statement_type],
    ]
    meta_table = Table(meta_rows, colWidths=["20%", "30%", "22%", "28%"])
    meta_table.setStyle(TS([
        ("FONTNAME",  (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",  (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTNAME",  (1, 0), (1, -1), "Helvetica"),
        ("FONTNAME",  (3, 0), (3, -1), "Helvetica"),
        ("FONTSIZE",  (0, 0), (-1, -1), 8),
        ("TOPPADDING",(0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0,0),(-1,-1), 2),
    ]))
    story.append(meta_table)
    story.append(spacer(8))

    # ── Holdings: REORDERED columns + RENAMED header ─────────────────────────
    story.append(Paragraph("Holdings", STYLE_SECTION))
    story.append(hr())

    avail_w = A4[0] - 2 * MARGIN
    # Column order: Security Name | Qty | Cost (INR) | Net Value (INR) | Weight % | ISIN
    col_ws = [avail_w * r for r in (0.28, 0.09, 0.15, 0.16, 0.10, 0.16, 0.06)]
    h_header = [["Security Name", "Qty", "Cost (INR)", "Net Value (INR)", "Weight %", "ISIN", ""]]  # ← reordered + renamed
    h_rows = [
        [h.security_name, fmt_qty(h.quantity),
         fmt_inr(h.cost), fmt_inr(h.market_value), fmt_pct(h.weight), h.isin, ""]
        for h in data.holdings
    ]
    h_table = Table(h_header + h_rows, colWidths=col_ws, repeatRows=1)
    h_table.setStyle(holdings_table_style(len(h_rows)))
    story.append(h_table)
    story.append(spacer(8))

    # ── Transactions: DIFFERENT date format ───────────────────────────────────
    story.append(Paragraph("Transactions", STYLE_SECTION))
    story.append(hr())

    t_col_ws = [avail_w * r for r in (0.16, 0.12, 0.38, 0.12, 0.22)]
    t_header = [["Date", "Type", "Security", "Qty", "Amount (INR)"]]
    t_rows = [
        [_date_str_drift(t.date), t.txn_type,           # ← DD-Mon-YYYY
         t.security or "—", fmt_qty(t.quantity) if t.quantity else "—",
         fmt_inr(t.amount)]
        for t in data.transactions
    ]
    t_table = Table(t_header + t_rows, colWidths=t_col_ws, repeatRows=1)
    t_table.setStyle(transactions_table_style(len(t_rows)))
    story.append(t_table)
    story.append(spacer(8))

    # ── RENAMED summary section heading ───────────────────────────────────────
    story.append(Paragraph("Account Summary", STYLE_SECTION))    # ← renamed
    story.append(hr())

    s = data.summary
    s_rows = [
        ["Opening Portfolio Value",  fmt_inr(s.opening_value)],
        ["Closing Portfolio Value",  fmt_inr(s.closing_value)],
        ["Opening Cash Balance",     fmt_inr(s.opening_cash)],
        ["Closing Cash Balance",     fmt_inr(s.closing_cash)],
        ["Fees Charged",             fmt_inr(s.fees_charged)],
        ["Income (Dividends/Int.)",  fmt_inr(s.income)],
        ["Realised Gains",           fmt_inr(s.realized_gains)],
        ["Unrealised Gains",         fmt_inr(s.unrealized_gains)],
        ["Report Generated",         data.period_end.strftime("%d-%b-%Y")],  # ← extra row
    ]
    s_table = Table(s_rows, colWidths=[avail_w * 0.55, avail_w * 0.45])
    s_table.setStyle(summary_table_style())
    story.append(s_table)

    story.append(spacer(12))
    story.append(hr())
    story.append(Paragraph(
        f"{data.provider_name} — Confidential | Generated for testing purposes only",
        STYLE_META,
    ))

    doc.build(story)
