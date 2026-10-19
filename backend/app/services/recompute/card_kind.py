"""Card line kind: what a credit-card row is for statement math (RFC §5.3, §6.5).

A ``set_card_line_kind`` rule wins (highest priority); otherwise the default:
a credit with operation type PAGAMENTO_FATURA is a payment, any other
credit is a credit, a debit is a purchase. Bank quirks (CAIXA's previous
balance line, payments recognized by text) are built-in rules, not code.
"""
from __future__ import annotations

from typing import Optional


def default_card_kind(tx_type: str, operation_type: Optional[str]) -> str:
    if tx_type == "credit":
        return "payment" if (operation_type or "").upper() == "PAGAMENTO_FATURA" else "credit"
    return "purchase"
