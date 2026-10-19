"""An in-memory Pluggy for sync tests (RFC toledo-features).

``FakePluggy`` subclasses the real provider and only replaces the HTTP
calls, so every payload goes through the real mapping
(``_build_transaction_data``, ``_build_bill_data``, ``_build_account_data``).
Payloads are plain dicts shaped like Pluggy's, built with ``pluggy_tx``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from app.providers.pluggy import PluggyProvider, _build_account_data, _build_bill_data


def iso(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f"{d.isoformat()}T00:00:00.000Z"


def pluggy_tx(
    tx_id: str,
    *,
    account_id: str = "acc-card",
    description: str = "PURCHASE",
    amount: float = -10.0,
    when: date = date(2026, 10, 1),
    status: str = "POSTED",
    tx_type: Optional[str] = None,
    created_at: Optional[datetime] = None,
    provider_id: Optional[str] = None,
    bill_id: Optional[str] = None,
    installment: Optional[tuple[int, int]] = None,
    purchase_date: Optional[str] = None,
    payment_type: Optional[str] = None,
    operation_type: Optional[str] = None,
    currency: str = "BRL",
    category: Optional[str] = None,
    category_id: Optional[str] = None,
    forecast: Optional[str] = None,
    post_date: Optional[str] = None,
    transaction_at: Optional[str] = None,
    merchant: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> dict:
    """A Pluggy transaction payload. ``amount`` is signed (− outflow)."""
    payload: dict = {
        "id": tx_id,
        "accountId": account_id,
        "description": description,
        "descriptionRaw": description,
        "currencyCode": currency,
        "amount": amount,
        "date": iso(when),
        "type": tx_type or ("DEBIT" if amount < 0 else "CREDIT"),
        "status": status,
        "category": category,
        "categoryId": category_id,
        "createdAt": iso(created_at or datetime(when.year, when.month, when.day, 12, tzinfo=timezone.utc)),
        "updatedAt": iso(created_at or datetime(when.year, when.month, when.day, 12, tzinfo=timezone.utc)),
    }
    if provider_id:
        payload["providerId"] = provider_id
    if operation_type:
        payload["operationType"] = operation_type
    if merchant:
        payload["merchant"] = merchant
    cc: dict = {}
    if bill_id:
        cc["billId"] = bill_id
    if installment:
        cc["installmentNumber"], cc["totalInstallments"] = installment
    if purchase_date:
        cc["purchaseDate"] = purchase_date
    if payment_type:
        cc["paymentType"] = payment_type
    if forecast:
        cc["billForecastDate"] = forecast
    if post_date:
        cc["billPostDate"] = post_date
    if transaction_at:
        cc["transactionDateTime"] = transaction_at
    if cc:
        payload["creditCardMetadata"] = cc
    if extra:
        payload.update(extra)
    return payload


def pluggy_bill(
    bill_id: str,
    due: date,
    total: float,
    *,
    closing: Optional[date] = None,
    finance_charges: Optional[list] = None,
    payments: Optional[list] = None,
) -> dict:
    raw = {
        "id": bill_id,
        "dueDate": iso(due),
        "totalAmount": total,
        "totalAmountCurrencyCode": "BRL",
        "minimumPaymentAmount": round(total * 0.15, 2),
        "allowsInstallments": True,
        "financeCharges": finance_charges or [],
        "payments": payments or [],
    }
    if closing:
        raw["closingDate"] = iso(closing)
    return raw


def pluggy_account(
    account_id: str = "acc-card",
    *,
    kind: str = "CREDIT",
    name: str = "Card",
    balance: float = 0.0,
    credit_data: Optional[dict] = None,
) -> dict:
    payload = {
        "id": account_id,
        "name": name,
        "type": kind,
        "subtype": "CREDIT_CARD" if kind == "CREDIT" else "CHECKING_ACCOUNT",
        "balance": balance,
        "currencyCode": "BRL",
    }
    if kind == "CREDIT":
        payload["creditData"] = credit_data or {"creditLimit": 10000, "availableCreditLimit": 9000}
    return payload


class FakePluggy(PluggyProvider):
    """Pluggy without HTTP. Mutate ``accounts`` / ``transactions`` / ``bills``
    between syncs to simulate what the bank and Pluggy do."""

    def __init__(self) -> None:
        super().__init__(credential=None)
        self.accounts: list[dict] = []
        self.transactions: dict[str, list[dict]] = {}
        self.bills: dict[str, list[dict]] = {}
        # Bills reachable by GET /bills/{id} but missing from the listing.
        self.hidden_bills: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_created_sweep = False

    # --- the calls the sync makes -------------------------------------------
    async def refresh_credentials(self, credentials: dict) -> dict:
        return credentials

    async def get_institution_logo(self, credentials: dict):
        return None

    async def get_item_status(self, credentials: dict) -> dict:
        return {"status": "UPDATED", "execution_status": "SUCCESS"}

    async def get_holdings(self, credentials: dict):
        return []

    async def get_accounts(self, credentials: dict):
        return [_build_account_data(a, self._map_account_type) for a in self.accounts]

    async def get_bills(self, credentials: dict, account_external_id: str):
        self.calls.append(("bills", account_external_id))
        return [b for b in (_build_bill_data(r) for r in self.bills.get(account_external_id, [])) if b]

    async def get_bill(self, credentials: dict, bill_external_id: str):
        self.calls.append(("bill", bill_external_id))
        raw = self.hidden_bills.get(bill_external_id)
        if raw is None:
            raise RuntimeError("404 bill not found")
        return _build_bill_data(raw)

    async def get_transactions_filtered(
        self,
        credentials: dict,
        account_external_id: str,
        *,
        payee_source: str = "auto",
        date_from: Optional[date] = None,
        created_at_from: Optional[str] = None,
        ids: Optional[list[str]] = None,
    ):
        self.calls.append(
            ("transactions", account_external_id, date_from, created_at_from, tuple(ids or ()))
        )
        if created_at_from and self.fail_created_sweep:
            raise RuntimeError("Pluggy 500")
        rows = list(self.transactions.get(account_external_id, []))
        if date_from:
            rows = [r for r in rows if r["date"][:10] >= date_from.isoformat()]
        if created_at_from:
            threshold = datetime.fromisoformat(created_at_from.replace("Z", "+00:00"))
            if threshold.tzinfo is None:
                threshold = threshold.replace(tzinfo=timezone.utc)
            rows = [
                r for r in rows
                if datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= threshold
            ]
        if ids:
            wanted = set(ids)
            rows = [r for r in rows if r["id"] in wanted]
        return [self._build_transaction_data(r, payee_source) for r in rows]

    # --- helpers for tests --------------------------------------------------
    def put(self, *payloads: dict) -> None:
        """Insert or replace payloads by id."""
        for p in payloads:
            rows = self.transactions.setdefault(p["accountId"], [])
            rows[:] = [r for r in rows if r["id"] != p["id"]] + [p]

    def drop(self, account_id: str, *tx_ids: str) -> None:
        rows = self.transactions.get(account_id, [])
        rows[:] = [r for r in rows if r["id"] not in set(tx_ids)]

    def transaction_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "transactions"]


def new_id() -> str:
    return str(uuid.uuid4())
