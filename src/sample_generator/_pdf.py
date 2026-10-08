"""ReportLab rendering utilities shared across all format generators."""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

# ---------------------------------------------------------------------------
# Shared styles
# ---------------------------------------------------------------------------
_base = getSampleStyleSheet()

STYLE_TITLE = ParagraphStyle(
    "PmsTitle",
    parent=_base["Heading1"],
    fontSize=14,
    spaceAfter=4,
    textColor=colors.HexColor("#1a237e"),
)
STYLE_SUBTITLE = ParagraphStyle(
    "PmsSubtitle",
    parent=_base["Heading2"],
    fontSize=10,
    spaceAfter=2,
    textColor=colors.HexColor("#283593"),
)
STYLE_META = ParagraphStyle(
    "PmsMeta",
    parent=_base["Normal"],
    fontSize=8,
    spaceAfter=2,
    textColor=colors.HexColor("#37474f"),
)
STYLE_BODY = ParagraphStyle(
    "PmsBody",
    parent=_base["Normal"],
    fontSize=8,
    spaceAfter=1,
)
STYLE_SECTION = ParagraphStyle(
    "PmsSection",
    parent=_base["Heading3"],
    fontSize=9,
    spaceBefore=6,
    spaceAfter=3,
    textColor=colors.HexColor("#1565c0"),
)

# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------
HEADER_BG = colors.HexColor("#1a237e")
ROW_ALT   = colors.HexColor("#e8eaf6")
GRID_CLR  = colors.HexColor("#9fa8da")


def holdings_table_style(num_data_rows: int) -> TableStyle:
    """Standard holdings table style with alternating row colours."""
    cmds = [
        ("BACKGROUND",   (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 7),
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 1), (-1, -1), 7),
        ("GRID",         (0, 0), (-1, -1), 0.3, GRID_CLR),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN",        (2, 1), (-1, -1), "RIGHT"),   # numeric cols right-aligned
    ]
    for i in range(1, num_data_rows + 1):
        if i % 2 == 0:
            cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    return TableStyle(cmds)


def transactions_table_style(num_data_rows: int) -> TableStyle:
    cmds = [
        ("BACKGROUND",   (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 7),
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 1), (-1, -1), 7),
        ("GRID",         (0, 0), (-1, -1), 0.3, GRID_CLR),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN",        (3, 1), (-1, -1), "RIGHT"),
    ]
    for i in range(1, num_data_rows + 1):
        if i % 2 == 0:
            cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    return TableStyle(cmds)


def summary_table_style() -> TableStyle:
    return TableStyle([
        ("FONTNAME",     (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 0), (-1, -1), 8),
        ("FONTNAME",     (0, 0), (0, -1), "Helvetica-Bold"),
        ("ALIGN",        (1, 0), (1, -1), "RIGHT"),
        ("GRID",         (0, 0), (-1, -1), 0.3, GRID_CLR),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ])


def fmt_inr(val) -> str:
    if val is None:
        return "—"
    return f"₹{float(val):,.2f}"


def fmt_qty(val) -> str:
    if val is None:
        return "—"
    return f"{float(val):,.4f}"


def fmt_pct(val) -> str:
    if val is None:
        return "—"
    return f"{float(val):.4f}%"


def hr() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=HEADER_BG, spaceAfter=3)


def spacer(h: int = 6) -> Spacer:
    return Spacer(1, h)
