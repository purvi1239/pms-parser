"""Format B — Structurally different PMS PDF generator.

Layout landmarks deliberately different from Format A:
  - Title: "Client Portfolio Report"  (different from Format A's "Portfolio Management Statement")
  - Metadata: different label names ("Account Number", "Report Period", "Base Currency")
  - Holdings section: "Portfolio Holdings" (not "Holdings")
    Columns: No. | Scrip Name | ISIN Code | Units | Purchase Cost | Current Value | Allocation %
  - Transactions section: "Trade Ledger" (not "Transactions")
    Columns: Trade Date | Activity | Instrument | Units | Consideration (INR)
  - Summary section: "Fund Summary" (not "Portfolio Summary")
    Summary is a horizontal two-row table (labels row, values row) not a two-column list
  - Dates: YYYY-MM-DD format
  - Footer different provider text

All these differences mean Format B will have a distinct fingerprint from Format A.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.platypus import SimpleDocTemplate, Paragraph, Table
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import TableStyle as TS

from src.sample_generator._data import SampleData
from src.sample_generator._pdf import (
    MARGIN, STYLE_TITLE, STYLE_SUBTITLE, STYLE_META, STYLE_SECTION,
    HEADER_BG, ROW_ALT, GRID_CLR,
    fmt_inr, fmt_qty, fmt_pct, hr, spacer,
)


def _date_str(d) -> str:
    """YYYY-MM-DD — Format B canonical date format."""
    return d.strftime("%Y-%m-%d")


def build_format_b(data: SampleData, out_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
    )
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph("Client Portfolio Report", STYLE_TITLE))   # ← different title
    story.append(Paragraph(data.provider_name, STYLE_SUBTITLE))
    story.append(Paragraph(data.strategy_name, STYLE_SUBTITLE))
    story.append(hr())
    story.append(spacer(4))

    meta_rows = [
        ["Account Number:",  data.client_identifier,
         "Report Period:",   data.statement_period],
        ["Base Currency:",   data.currency,
         "Account Type:",    data.statement_type],
    ]
    meta_table = Table(meta_rows, colWidths=["22%", "28%", "22%", "28%"])
    meta_table.setStyle(TS([
        ("FONTNAME",  (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",  (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 8),
        ("TOPPADDING",(0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0,0),(-1,-1), 2),
    ]))
    story.append(meta_table)
    story.append(spacer(8))

    # ── Portfolio Holdings ─────────────────────────────────────────────────────
    story.append(Paragraph("Portfolio Holdings", STYLE_SECTION))   # ← different heading
    story.append(hr())

    avail_w = A4[0] - 2 * MARGIN
    # Column order: No. | Scrip Name | ISIN Code | Units | Purchase Cost | Current Value | Allocation %
    col_ws = [avail_w * r for r in (0.04, 0.28, 0.15, 0.09, 0.15, 0.15, 0.14)]
    h_header = [["No.", "Scrip Name", "ISIN Code", "Units", "Purchase Cost", "Current Value", "Allocation %"]]
    h_rows = [
        [str(i + 1), h.security_name, h.isin, fmt_qty(h.quantity),
         fmt_inr(h.cost), fmt_inr(h.market_value), fmt_pct(h.weight)]
        for i, h in enumerate(data.holdings)
    ]
    h_table = Table(h_header + h_rows, colWidths=col_ws, repeatRows=1)
    h_table.setStyle(TS([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#1b5e20")),  # ← green header
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#a5d6a7")),
        ("ALIGN",         (3, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        *[("BACKGROUND", (0, i), (-1, i), colors.HexColor("#e8f5e9"))
          for i in range(2, len(h_rows) + 1, 2)],
    ]))
    story.append(h_table)
    story.append(spacer(8))

    # ── Trade Ledger ─────────────────────────────────────────────────────────
    story.append(Paragraph("Trade Ledger", STYLE_SECTION))   # ← different heading
    story.append(hr())

    t_col_ws = [avail_w * r for r in (0.16, 0.14, 0.38, 0.12, 0.20)]
    t_header = [["Trade Date", "Activity", "Instrument", "Units", "Consideration (INR)"]]
    t_rows = [
        [_date_str(t.date), t.txn_type,
         t.security or "—", fmt_qty(t.quantity) if t.quantity else "—",
         fmt_inr(t.amount)]
        for t in data.transactions
    ]
    t_table = Table(t_header + t_rows, colWidths=t_col_ws, repeatRows=1)
    t_table.setStyle(TS([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#1b5e20")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#a5d6a7")),
        ("ALIGN",         (3, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        *[("BACKGROUND", (0, i), (-1, i), colors.HexColor("#e8f5e9"))
          for i in range(2, len(t_rows) + 1, 2)],
    ]))
    story.append(t_table)
    story.append(spacer(8))

    # ── Fund Summary — horizontal layout ─────────────────────────────────────
    story.append(Paragraph("Fund Summary", STYLE_SECTION))    # ← different heading
    story.append(hr())

    s = data.summary
    # Horizontal format: labels in row 0, values in row 1
    label_row = ["Opening Value", "Closing Value", "Opening Cash", "Closing Cash",
                 "Fees Charged", "Income", "Realised Gains", "Unrealised Gains"]
    value_row = [fmt_inr(s.opening_value), fmt_inr(s.closing_value),
                 fmt_inr(s.opening_cash),  fmt_inr(s.closing_cash),
                 fmt_inr(s.fees_charged),  fmt_inr(s.income),
                 fmt_inr(s.realized_gains), fmt_inr(s.unrealized_gains)]
    n = len(label_row)
    col_w = avail_w / n
    s_table = Table([label_row, value_row], colWidths=[col_w] * n)
    s_table.setStyle(TS([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#1b5e20")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTNAME",      (0, 1), (-1, 1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, 1), 7),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#a5d6a7")),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(s_table)

    story.append(spacer(12))
    story.append(hr())
    story.append(Paragraph(
        f"{data.provider_name} | SEBI Reg. No. INP0001234 | Generated for testing only",
        STYLE_META,
    ))

    doc.build(story)
