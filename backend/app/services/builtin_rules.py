"""Rules shipped by Securo (RFC toledo-features §5.3).

"Brazilian credit cards (Open Finance)": bank quirks observed in production
data (Appendix C) expressed as editable rules instead of code paths — the
CAIXA previous-balance line, payments recognized only by their text, and the
installment markers banks write into descriptions. Rules are flagged
``is_builtin`` with a stable ``builtin_key``; users may edit or disable them
and "Restore defaults" recreates the originals.
"""
from __future__ import annotations

BR_CARDS = "BR_CARDS"

_PARC_EQ = r"\bPARC\s*=\s*\d{1,3}"
_PARC = r"\bPARC(ELA)?\.?\s*\d{1,3}"
_SLASH = r"\b\d{1,3}\s*/\s*\d{1,3}\b"
_DE = r"\b\d{1,2}\s+DE\s+\d{1,2}\b"

BUILTIN_PACKS: dict[str, dict] = {
    BR_CARDS: {
        "name": {"en": "Brazilian credit cards (Open Finance)", "pt-BR": "Cartões brasileiros (Open Finance)"},
        "flag": "\U0001F1E7\U0001F1F7",
        "rules": [
            {
                "key": "br.previous_balance",
                "name": {"en": "Previous bill balance as a line", "pt-BR": "Saldo anterior lançado como transação"},
                "priority": 10,
                "conditions_op": "and",
                "conditions": [
                    {"field": "description", "op": "regex", "value": "(TOTAL DA )?FATURA ANTERIOR|SALDO ANTERIOR"},
                ],
                "actions": [{"op": "set_card_line_kind", "value": "previous_balance"}],
            },
            {
                "key": "br.payment_by_text",
                "name": {"en": "Bill payment by description", "pt-BR": "Pagamento de fatura pela descrição"},
                "priority": 20,
                "conditions_op": "and",
                "conditions": [
                    {"field": "type", "op": "equals", "value": "credit"},
                    {"field": "description", "op": "regex", "value": r"\b(PGTO|PAGTO|PAGAMENTO)\b"},
                    {"field": "description", "op": "not_regex", "value": "AJUSTE|ESTORNO"},
                ],
                "actions": [{"op": "set_card_line_kind", "value": "payment"}],
            },
            {
                "key": "br.marker_parc_eq",
                "name": {"en": "Installment marker \"PARC=1NN\"", "pt-BR": "Marcador de parcela \"PARC=1NN\""},
                "priority": 100,
                "conditions_op": "and",
                "conditions": [{"field": "description", "op": "regex", "value": _PARC_EQ}],
                "actions": [{"op": "strip_installment_marker", "value": r"\bPARC\s*=\s*\d{1,3}\s*"}],
            },
            {
                "key": "br.marker_parc",
                "name": {"en": "Installment marker \"PARC 3/10\"", "pt-BR": "Marcador de parcela \"PARC 3/10\""},
                "priority": 110,
                "conditions_op": "and",
                "conditions": [{"field": "description", "op": "regex", "value": _PARC}],
                "actions": [{
                    "op": "strip_installment_marker",
                    "value": r"\bPARC(?:ELA)?\.?\s*\d{1,3}(?:\s*(?:/|DE)\s*\d{1,3})?\b",
                }],
            },
            {
                "key": "br.marker_slash",
                "name": {"en": "Installment marker \"3/10\"", "pt-BR": "Marcador de parcela \"3/10\""},
                "priority": 120,
                "conditions_op": "and",
                "conditions": [{"field": "description", "op": "regex", "value": _SLASH}],
                "actions": [{"op": "strip_installment_marker", "value": _SLASH}],
            },
            {
                "key": "br.marker_de",
                "name": {"en": "Installment marker \"03 DE 06\"", "pt-BR": "Marcador de parcela \"03 DE 06\""},
                "priority": 130,
                "conditions_op": "and",
                "conditions": [{"field": "description", "op": "regex", "value": _DE}],
                "actions": [{"op": "strip_installment_marker", "value": _DE}],
            },
        ],
    }
}


def localized(names: dict, lang: str) -> str:
    return names.get(lang) or names.get(lang.split("-")[0]) or names["en"]
