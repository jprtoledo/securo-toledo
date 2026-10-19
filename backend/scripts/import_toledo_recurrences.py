#!/usr/bin/env python3
"""Import the recurrences of a toledo-finance database into Securo.

Reads toledo-finance's Postgres directly and creates (or updates) Securo
recurring transactions with the Recurrences v2 fields described in
docs/rfc-toledo-features.md §8. Needs that schema: the script checks the
columns first and stops with the list of what is missing.

Must run with the backend modules importable (e.g. inside the backend container):
    docker compose exec backend python scripts/import_toledo_recurrences.py \
        --toledo-url postgresql://postgres:postgres@db:5432/toledo_finance

Options:
    --toledo-url URL      toledo-finance database (default: env TOLEDO_DATABASE_URL)
    --workspace-id UUID   only accounts of this Securo workspace
    --with-decisions      also copy per-transaction decisions (manual link,
                          "not recurring", accepted, rejected)
    --apply               write the changes (default: dry run, nothing is written)

Accounts are matched by Pluggy account id (toledo accounts.id ==
Securo accounts.external_id of a Pluggy connection) and transactions by
Pluggy transaction id. Re-running is safe: recurrences are keyed by
import_ref = "toledo:<toledo recurrence id>".
"""
import argparse
import asyncio
import calendar
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

# Allow running from repo root or backend root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from app.core.database import async_session_maker

IMPORT_PREFIX = "toledo:"

REQUIRED_RECURRING_COLUMNS = {
    "match_condition", "amount_mode", "amount_tolerance_pct", "day_window",
    "origin", "import_ref",
}
REQUIRED_DECISION_COLUMNS = {
    "recurring_manual_id", "recurring_manual_none", "recurring_manual_excluded",
    "recurring_rejected_ids",
}

# Appendix B of the RFC: toledo condition language -> Securo rules language.
FIELD_MAP = {
    "description": "description",
    "descriptionKey": "description_key",
    "descriptionRaw": "provider_description_raw",
    "merchantName": "merchant_name",
    "amount": "amount",
    "signedAmount": "signed_amount",
    "type": "type",
    "status": "status",
    "operationType": "operation_type",
    "category": "provider_category",
    "currencyCode": "currency",
    "date": "date",
    "cardNumber": "card_number",
    "installmentNumber": "installment_number",
    "totalInstallments": "total_installments",
    "paymentType": "payment_type",
}
OP_MAP = {
    "eq": "equals",
    "neq": "not_equals",
    "contains": "contains",
    "notContains": "not_contains",
    "startsWith": "starts_with",
    "matches": "regex",
    "notMatches": "not_regex",
    "in": "in",
    "notIn": "not_in",
    "isNull": "is_empty",
    "isNotNull": "is_not_empty",
    "lt": "lt",
    "lte": "lte",
    "gt": "gt",
    "gte": "gte",
}
# Securo stores these enums in lowercase (toledo used the Pluggy spelling).
LOWERCASE_FIELDS = {"type", "status"}


class UnsupportedCondition(ValueError):
    pass


def convert_node(node: dict) -> dict:
    """One toledo condition node -> one Securo condition node."""
    if "and" in node or "or" in node:
        op = "and" if "and" in node else "or"
        return {"op": op, "conditions": [convert_node(c) for c in node[op]]}
    if "not" in node:
        return {"not": convert_node(node["not"])}
    toledo_field, toledo_op = node.get("field"), node.get("op")
    if toledo_field not in FIELD_MAP:
        raise UnsupportedCondition(f"field {toledo_field!r}")
    if toledo_op not in OP_MAP:
        raise UnsupportedCondition(f"operator {toledo_op!r}")
    securo_field = FIELD_MAP[toledo_field]
    leaf: dict = {"field": securo_field, "op": OP_MAP[toledo_op]}
    if "value" in node:
        value = node["value"]
        if securo_field in LOWERCASE_FIELDS:
            value = [str(v).lower() for v in value] if isinstance(value, list) else str(value).lower()
        leaf["value"] = value
    return leaf


def convert_condition(condition) -> dict:
    """Top level is always a group, so the stored shape matches a rule's."""
    if isinstance(condition, str):
        condition = json.loads(condition)
    converted = convert_node(condition)
    if "conditions" in converted:
        return converted
    return {"op": "and", "conditions": [converted]}


def clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def add_months(d: date, months: int, day: int) -> date:
    index = d.year * 12 + (d.month - 1) + months
    return clamp_day(index // 12, index % 12 + 1, day)


def next_occurrence(start: date, end: Optional[date], step: int, day: int, today: date) -> date:
    """First period on or after today (or the last one when the series ended)."""
    current = start
    while current < today:
        following = add_months(current, step, day)
        if end and following > end:
            break
        current = following
    return current


@dataclass
class Report:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    decisions: int = 0
    decisions_unmatched: int = 0


async def missing_columns(session, table: str, required: set[str]) -> set[str]:
    rows = await session.execute(
        text("select column_name from information_schema.columns where table_name = :t"),
        {"t": table},
    )
    return required - {r[0] for r in rows}


async def load_securo_accounts(session, workspace_id: Optional[uuid.UUID]) -> dict[str, list[dict]]:
    """Pluggy account id -> Securo accounts carrying it (one per workspace)."""
    rows = await session.execute(
        text(
            """
            select a.id, a.external_id, a.user_id, a.workspace_id, a.name
            from accounts a
            join bank_connections c on c.id = a.connection_id
            where c.provider = 'pluggy' and a.external_id is not null
              and (cast(:ws as uuid) is null or a.workspace_id = cast(:ws as uuid))
            """
        ),
        {"ws": workspace_id},
    )
    by_external: dict[str, list[dict]] = {}
    for r in rows.mappings():
        by_external.setdefault(r["external_id"], []).append(dict(r))
    return by_external


async def import_recurrences(toledo, session, accounts, today: date, report: Report) -> dict[str, uuid.UUID]:
    """Create/update recurrences; returns toledo recurrence id -> Securo id."""
    rows = await toledo.fetch(
        """
        select r.*, a.name as account_name
        from recurrences r join accounts a on a.id = r.account_id
        order by r.created_at
        """
    )
    mapping: dict[str, uuid.UUID] = {}
    for r in rows:
        label = f"{r['name']} ({r['account_name']})"
        targets = accounts.get(r["account_id"], [])
        if not targets:
            report.skipped.append(f"{label}: account not connected in Securo")
            continue
        if len(targets) > 1:
            report.skipped.append(f"{label}: account exists in {len(targets)} workspaces, use --workspace-id")
            continue
        account = targets[0]
        try:
            condition = convert_condition(r["condition"])
        except UnsupportedCondition as exc:
            report.skipped.append(f"{label}: unsupported condition ({exc})")
            continue

        yearly = r["frequency"] == "YEARLY"
        day = r["day_of_month"]
        start_month: date = r["start_month"]
        start = clamp_day(start_month.year, start_month.month, day)
        end_month: Optional[date] = r["end_month"]
        end = clamp_day(end_month.year, end_month.month, day) if end_month else None
        values = {
            "user_id": account["user_id"],
            "workspace_id": account["workspace_id"],
            "account_id": account["id"],
            "description": r["name"],
            "amount": Decimal(r["expected_amount"]),
            "currency": r["currency_code"],
            "type": "debit" if r["direction"] == "EXPENSE" else "credit",
            "frequency": "yearly" if yearly else "monthly",
            "day_of_month": day,
            "start_date": start,
            "end_date": end,
            "is_active": bool(r["enabled"]),
            "next_occurrence": next_occurrence(start, end, 12 if yearly else 1, day, today),
            "match_condition": condition,
            "amount_mode": r["amount_mode"].lower(),
            "amount_tolerance_pct": Decimal(r["tolerance_pct"]),
            "day_window": r["day_window"],
            "import_ref": f"{IMPORT_PREFIX}{r['id']}",
        }

        existing = (
            await session.execute(
                text("select id from recurring_transactions where workspace_id = :ws and import_ref = :ref"),
                {"ws": values["workspace_id"], "ref": values["import_ref"]},
            )
        ).scalar_one_or_none()

        # The legacy unique key (user, description, frequency, start date) still applies.
        clash = (
            await session.execute(
                text(
                    """
                    select id from recurring_transactions
                    where user_id = :user_id and description = :description
                      and frequency = :frequency and start_date = :start_date
                      and (cast(:existing as uuid) is null or id <> cast(:existing as uuid))
                    """
                ),
                {**{k: values[k] for k in ("user_id", "description", "frequency", "start_date")}, "existing": existing},
            )
        ).scalar_one_or_none()
        if clash:
            report.skipped.append(f"{label}: a Securo recurrence with the same name, frequency and start already exists")
            continue

        params = {**values, "match_condition": condition}
        typed = [bindparam("match_condition", type_=JSONB)]
        if existing:
            await session.execute(
                text(
                    """
                    update recurring_transactions set
                      description = :description, amount = :amount, currency = :currency, type = :type,
                      frequency = :frequency, day_of_month = :day_of_month, start_date = :start_date,
                      end_date = :end_date, is_active = :is_active, next_occurrence = :next_occurrence,
                      match_condition = :match_condition, amount_mode = :amount_mode,
                      amount_tolerance_pct = :amount_tolerance_pct, day_window = :day_window
                    where id = :id
                    """
                ).bindparams(*typed),
                {**params, "id": existing},
            )
            mapping[str(r["id"])] = existing
            report.updated.append(label)
        else:
            new_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    insert into recurring_transactions (
                      id, user_id, workspace_id, account_id, description, amount, currency, type,
                      frequency, weekend_adjustment, day_of_month, start_date, end_date, is_active,
                      auto_generate, next_occurrence, match_condition, amount_mode,
                      amount_tolerance_pct, day_window, origin, import_ref
                    ) values (
                      :id, :user_id, :workspace_id, :account_id, :description, :amount, :currency, :type,
                      :frequency, 'none', :day_of_month, :start_date, :end_date, :is_active,
                      false, :next_occurrence, :match_condition, :amount_mode,
                      :amount_tolerance_pct, :day_window, 'import', :import_ref
                    )
                    """
                ).bindparams(*typed),
                {**params, "id": new_id},
            )
            mapping[str(r["id"])] = new_id
            report.created.append(label)
    return mapping


async def import_decisions(toledo, session, accounts, mapping, report: Report) -> None:
    rows = await toledo.fetch(
        """
        select id, account_id, recurrence_manual_id, recurrence_manual_none,
               recurrence_manual_excluded, recurrence_rejected_ids
        from transactions
        where recurrence_manual_id is not null or recurrence_manual_none
           or recurrence_manual_excluded or cardinality(recurrence_rejected_ids) > 0
        """
    )
    update = text(
        """
        update transactions set
          recurring_manual_id = :manual_id, recurring_manual_none = :manual_none,
          recurring_manual_excluded = :manual_excluded, recurring_rejected_ids = :rejected
        where account_id = :account_id and external_id = :external_id
        """
    ).bindparams(bindparam("rejected", type_=ARRAY(UUID(as_uuid=True))))
    for r in rows:
        targets = accounts.get(r["account_id"], [])
        manual_id = mapping.get(str(r["recurrence_manual_id"])) if r["recurrence_manual_id"] else None
        if len(targets) != 1 or (r["recurrence_manual_id"] and not manual_id):
            report.decisions_unmatched += 1
            continue
        rejected = [mapping[str(x)] for x in (r["recurrence_rejected_ids"] or []) if str(x) in mapping]
        result = await session.execute(
            update,
            {
                "manual_id": manual_id,
                "manual_none": bool(r["recurrence_manual_none"]),
                "manual_excluded": bool(r["recurrence_manual_excluded"]) and manual_id is not None,
                "rejected": rejected,
                "account_id": targets[0]["id"],
                "external_id": r["id"],
            },
        )
        if result.rowcount:
            report.decisions += 1
        else:
            report.decisions_unmatched += 1


async def recompute(session, account_ids: set[uuid.UUID]) -> bool:
    try:
        from app.services.recompute_service import recompute_account
    except ImportError:
        return False
    for account_id in account_ids:
        await recompute_account(session, account_id)
    return True


def print_report(report: Report, apply: bool) -> None:
    verb = "" if apply else "would be "
    print(f"\nRecurrences {verb}created: {len(report.created)}")
    for label in report.created:
        print(f"  + {label}")
    print(f"Recurrences {verb}updated: {len(report.updated)}")
    for label in report.updated:
        print(f"  ~ {label}")
    print(f"Skipped: {len(report.skipped)}")
    for reason in report.skipped:
        print(f"  - {reason}")
    if report.decisions or report.decisions_unmatched:
        print(f"Transaction decisions {verb}copied: {report.decisions}")
        print(f"Transaction decisions without a matching Securo transaction: {report.decisions_unmatched}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--toledo-url", default=os.environ.get("TOLEDO_DATABASE_URL"))
    parser.add_argument("--workspace-id", type=uuid.UUID)
    parser.add_argument("--with-decisions", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.toledo_url:
        parser.error("--toledo-url (or TOLEDO_DATABASE_URL) is required")
    toledo_url = args.toledo_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgres://")

    toledo = await asyncpg.connect(toledo_url)
    try:
        async with async_session_maker() as session:
            missing = await missing_columns(session, "recurring_transactions", REQUIRED_RECURRING_COLUMNS)
            if args.with_decisions:
                missing |= await missing_columns(session, "transactions", REQUIRED_DECISION_COLUMNS)
            if missing:
                print(
                    "Securo schema is missing the Recurrences v2 columns "
                    f"({', '.join(sorted(missing))}). Implement RFC Phase 6 and run the migrations first.",
                    file=sys.stderr,
                )
                return 2

            report = Report()
            accounts = await load_securo_accounts(session, args.workspace_id)
            mapping = await import_recurrences(toledo, session, accounts, date.today(), report)
            if args.with_decisions:
                await import_decisions(toledo, session, accounts, mapping, report)

            if not args.apply:
                await session.rollback()
                print_report(report, apply=False)
                print("\nDry run: nothing was written. Re-run with --apply.")
                return 0

            touched = {
                row[0]
                for row in await session.execute(
                    text("select distinct account_id from recurring_transactions where id = any(:ids)").bindparams(
                        bindparam("ids", type_=ARRAY(UUID(as_uuid=True)))
                    ),
                    {"ids": list(mapping.values())},
                )
            }
            recomputed = await recompute(session, touched)
            await session.commit()
            print_report(report, apply=True)
            if not recomputed:
                print("\nNote: recompute_service is not available yet; links appear on the next sync.")
            return 0
    finally:
        await toledo.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
