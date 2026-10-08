"""Format C — AIF Statement PDF generator.

Layout landmarks:
  - Title: "Alternative Investment Fund — Investor Statement"
  - Metadata: Investor ID, Commitment Period, Base Currency, Fund Category
  - Section: "Fund Overview" (NAV per unit, total units, closing value)
  - Section: "Cash Ledger" (transactions — no security/quantity for AIF)
  - Section: "Fund Performance Summary"
  - No Holdings section (AIF investors hold units, not individual securities)
  - Dates: DD MMM YYYY (e.g. "01 Jan 2026")
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
    fmt_inr, fmt_qty, fmt_pct, hr, spacer,
)


def _date_str(d) -> str:
    """DD MMM YYYY — Format C canonical date format."""
    return d.strftime("%d %b %Y")


TEAL_HDR = colors.HexColor("#004d5c")
TEAL_ALT = colors.HexColor("#e0f7fa")
TEAL_GRD = colors.HexColor("#80cbc4")


def _aif_table_style(num_data_rows: int) -> TS:
    cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), TEAL_HDR),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("GRID",          (0, 0), (-1, -1), 0.3, TEAL_GRD),
        ("ALIGN",         (2, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
    ]
    for i in range(1, num_data_rows + 1):
        if i % 2 == 0:
            cmds.append(("BACKGROUND", (0, i), (-1, i), TEAL_ALT))
    return TS(cmds)


def _summary_style() -> TS:
    return TS([
        ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",      (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("GRID",          (0, 0), (-1, -1), 0.3, TEAL_GRD),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ])


def build_format_c(data: SampleData, out_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
    )
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph("Alternative Investment Fund — Investor Statement", STYLE_TITLE))
    story.append(Paragraph(data.provider_name, STYLE_SUBTITLE))
    story.append(Paragraph(data.strategy_name, STYLE_SUBTITLE))
    story.append(hr())
    story.append(spacer(4))

    meta_rows = [
        ["Investor ID:",      data.client_identifier,
         "Commitment Period:", data.statement_period],
        ["Base Currency:",    data.currency,
         "Fund Category:",    "Category II AIF"],
    ]
    meta_table = Table(meta_rows, colWidths=["20%", "30%", "22%", "28%"])
    meta_table.setStyle(TS([
        ("FONTNAME",  (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",  (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 8),
        ("TOPPADDING",(0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0,0),(-1,-1), 2),
    ]))
    story.append(meta_table)
    story.append(spacer(8))

    # ── Fund Overview ──────────────────────────────────────────────────────────
    story.append(Paragraph("Fund Overview", STYLE_SECTION))
    story.append(hr())

    avail_w = A4[0] - 2 * MARGIN
    s = data.summary
    nav_rows = [
        ["Units Held",          fmt_qty(s.units)],
        ["NAV Per Unit (INR)",  fmt_inr(s.nav_per_unit)],
        ["Total Investment Value (INR)", fmt_inr(s.closing_value)],
    ]
    nav_table = Table(nav_rows, colWidths=[avail_w * 0.55, avail_w * 0.45])
    nav_table.setStyle(_summary_style())
    story.append(nav_table)
    story.append(spacer(8))

    # ── Cash Ledger ────────────────────────────────────────────────────────────
    story.append(Paragraph("Cash Ledger", STYLE_SECTION))
    story.append(hr())

    t_col_ws = [avail_w * r for r in (0.20, 0.30, 0.50)]
    t_header = [["Date", "Activity", "Amount (INR)"]]
    t_rows = [
        [_date_str(t.date), t.txn_type, fmt_inr(t.amount)]
        for t in data.transactions
    ]
    t_table = Table(t_header + t_rows, colWidths=t_col_ws, repeatRows=1)
    t_table.setStyle(_aif_table_style(len(t_rows)))
    story.append(t_table)
    story.append(spacer(8))

    # ── Fund Performance Summary ──────────────────────────────────────────────
    story.append(Paragraph("Fund Performance Summary", STYLE_SECTION))
    story.append(hr())

    perf_rows = [
        ["Opening Investment Value",  fmt_inr(s.opening_value)],
        ["Closing Investment Value",  fmt_inr(s.closing_value)],
        ["Total Contributions",       fmt_inr(s.opening_cash + sum(
            t.amount for t in data.transactions if t.txn_type == "inflow"))],
        ["Management Fees Charged",   fmt_inr(s.fees_charged)],
        ["Opening Cash Balance",      fmt_inr(s.opening_cash)],
        ["Closing Cash Balance",      fmt_inr(s.closing_cash)],
    ]
    perf_table = Table(perf_rows, colWidths=[avail_w * 0.60, avail_w * 0.40])
    perf_table.setStyle(_summary_style())
    story.append(perf_table)

    story.append(spacer(12))
    story.append(hr())
    story.append(Paragraph(
        f"{data.provider_name} | SEBI Reg. AIF/CAT-II/2021 | Generated for testing purposes only",
        STYLE_META,
    ))

    doc.build(story)
