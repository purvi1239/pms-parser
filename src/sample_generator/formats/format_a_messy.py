"""Format A — Messy variant: multi-page holdings table with repeated headers.

Same structural anchors and same section names as Format A.
Holdings span two pages; table header row repeats on each page (repeatRows=1).
One security name is long enough to wrap across lines.

The future engine should parse this correctly using Format A's spec.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.platypus import SimpleDocTemplate, Paragraph, Table
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle

from src.sample_generator._data import SampleData
from src.sample_generator._pdf import (
    MARGIN, STYLE_TITLE, STYLE_SUBTITLE, STYLE_META, STYLE_SECTION,
    STYLE_BODY,
    holdings_table_style, transactions_table_style, summary_table_style,
    fmt_inr, fmt_qty, fmt_pct, hr, spacer,
)


def _date_str(d) -> str:
    return d.strftime("%d/%m/%Y")


def build_format_a_messy(data: SampleData, out_path: Path) -> None:
    """Render a multi-page Format-A-messy PDF.

    Holdings span multiple pages, and repeatRows=1 repeats the header.
    Long security names wrap within the security-name column.
    """
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    story = []

    # ── Header (same as Format A) ────────────────────────────────────────────
    story.append(Paragraph("Portfolio Management Statement", STYLE_TITLE))
    story.append(Paragraph(f"{data.provider_name} — {data.strategy_name}", STYLE_SUBTITLE))
    story.append(hr())
    story.append(spacer(4))

    from reportlab.platypus import TableStyle as TS
    from reportlab.lib import colors

    meta_rows = [
        ["Client ID:", data.client_identifier,
         "Statement Period:", data.statement_period],
        ["Currency:", data.currency,
         "Statement Type:", data.statement_type],
    ]
    meta_table = Table(meta_rows, colWidths=["20%", "30%", "22%", "28%"])
    meta_table.setStyle(TS([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(meta_table)
    story.append(spacer(8))

    # ── Holdings (spans multiple pages via repeatRows=1) ─────────────────────
    story.append(Paragraph("Holdings", STYLE_SECTION))
    story.append(hr())

    avail_w = A4[0] - 2 * MARGIN
    col_ws = [avail_w * r for r in (0.30, 0.15, 0.09, 0.15, 0.15, 0.10, 0.06)]

    h_header = [[
        "Security Name", "ISIN", "Qty", "Cost (INR)",
        "Mkt Value (INR)", "Weight %", "",
    ]]

    # Use a 7 pt paragraph so long security names wrap within column 1.
    name_style = ParagraphStyle(
        "HoldingSecurityName",
        parent=STYLE_BODY,
        fontName="Helvetica",
        fontSize=7,
        leading=8,
        spaceAfter=0,
        spaceBefore=0,
    )

    h_rows = [
        [
            Paragraph(h.security_name, name_style),
            h.isin,
            fmt_qty(h.quantity),
            fmt_inr(h.cost),
            fmt_inr(h.market_value),
            fmt_pct(h.weight),
            "",
        ]
        for h in data.holdings
    ]

    all_h = h_header + h_rows
    h_table = Table(all_h, colWidths=col_ws, repeatRows=1)
    h_table.setStyle(holdings_table_style(len(h_rows)))
    story.append(h_table)
    story.append(spacer(8))

    # ── Transactions ──────────────────────────────────────────────────────────
    story.append(Paragraph("Transactions", STYLE_SECTION))
    story.append(hr())

    t_col_ws = [avail_w * r for r in (0.14, 0.12, 0.40, 0.12, 0.22)]
    t_header = [["Date", "Type", "Security", "Qty", "Amount (INR)"]]
    t_rows = [
        [
            _date_str(t.date),
            t.txn_type,
            t.security or "—",
            fmt_qty(t.quantity) if t.quantity else "—",
            fmt_inr(t.amount),
        ]
        for t in data.transactions
    ]
    t_table = Table(t_header + t_rows, colWidths=t_col_ws, repeatRows=1)
    t_table.setStyle(transactions_table_style(len(t_rows)))
    story.append(t_table)
    story.append(spacer(8))

    # ── Portfolio Summary ─────────────────────────────────────────────────────
    story.append(Paragraph("Portfolio Summary", STYLE_SECTION))
    story.append(hr())

    s = data.summary
    s_rows = [
        ["Opening Portfolio Value", fmt_inr(s.opening_value)],
        ["Closing Portfolio Value", fmt_inr(s.closing_value)],
        ["Opening Cash Balance", fmt_inr(s.opening_cash)],
        ["Closing Cash Balance", fmt_inr(s.closing_cash)],
        ["Fees Charged", fmt_inr(s.fees_charged)],
        ["Income (Dividends/Int.)", fmt_inr(s.income)],
        ["Realised Gains", fmt_inr(s.realized_gains)],
        ["Unrealised Gains", fmt_inr(s.unrealized_gains)],
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
