"""Ground-truth JSON serialisation for all sample formats."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from src.sample_generator._data import SampleData, HoldingData, TransactionData


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def _holding_to_dict(h: HoldingData) -> dict:
    return {
        "security_name": h.security_name,
        "isin":          h.isin,
        "quantity":      str(h.quantity),
        "cost":          str(h.cost),
        "market_value":  str(h.market_value),
        "weight":        str(h.weight),
    }


def _txn_to_dict(t: TransactionData) -> dict:
    return {
        "date":     t.date.isoformat(),
        "type":     t.txn_type,
        "security": t.security,
        "quantity": str(t.quantity) if t.quantity is not None else None,
        "amount":   str(t.amount),
    }


def _summary_to_dict(s) -> dict:
    d = {
        "opening_value":    str(s.opening_value),
        "closing_value":    str(s.closing_value),
        "opening_cash":     str(s.opening_cash),
        "closing_cash":     str(s.closing_cash),
        "fees_charged":     str(s.fees_charged),
        "income":           str(s.income),
        "realized_gains":   str(s.realized_gains),
        "unrealized_gains": str(s.unrealized_gains),
    }
    if s.units is not None:
        d["units"] = str(s.units)
    if s.nav_per_unit is not None:
        d["nav_per_unit"] = str(s.nav_per_unit)
    return d


def write_ground_truth(data: SampleData, out_path: Path) -> None:
    """Serialise *data* to a ground-truth JSON file at *out_path*."""
    payload = {
        "label":            data.label,
        "provider_name":    data.provider_name,
        "strategy_name":    data.strategy_name,
        "client_identifier":data.client_identifier,
        "statement_period": data.statement_period,
        "period_start":     data.period_start.isoformat(),
        "period_end":       data.period_end.isoformat(),
        "currency":         data.currency,
        "statement_type":   data.statement_type,
        "holdings":         [_holding_to_dict(h) for h in data.holdings],
        "transactions":     [_txn_to_dict(t) for t in data.transactions],
        "summary":          _summary_to_dict(data.summary),
    }
    out_path.write_text(json.dumps(payload, indent=2, cls=_DecimalEncoder), encoding="utf-8")
