import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import String, and_, or_, select, desc, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.core.config import get_settings
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.transaction import Transaction
from app.models.category import Category
from app.models.user import User
from app.services._query_filters import (
    counts_as_pnl,
    counts_as_user_pnl,
    owner_split_offset_by_category,
    is_card_refund,
    reporting_date,
    reporting_date_col,
)
from app.services.admin_service import get_credit_card_accounting_mode
from app.services.account_service import get_account_name
from app.services.fx_rate_service import convert
from app.schemas.report import (
    CategoryTrendItem,
    ReportBreakdown,
    ReportCompositionItem,
    ReportDataPoint,
    ReportMeta,
    ReportResponse,
    ReportSummary,
)
from app.services import invoice_forecast_service
from app.services.dashboard_service import (
    _account_balance_at,
    _counts_as_user_pnl_row,
    _get_forecast_transactions,
    _get_open_accounts,
)

CATEGORY_TREND_TOP_N = 11

_ACCOUNT_TYPE_COLORS: dict[str, str] = {
    "checking": "#6366F1",
    "savings": "#3B82F6",
    "credit_card": "#F43F5E",
    "investment": "#8B5CF6",
    "wallet": "#F59E0B",
}
_ASSET_TYPE_COLORS: dict[str, str] = {
    "real_estate": "#0EA5E9",
    "vehicle": "#14B8A6",
    "valuable": "#F59E0B",
    "investment": "#8B5CF6",
    "other": "#6B7280",
}


def _report_start_date(
    today: date,
    months: int,
    period: str | None = None,
    days: int | None = None,
    financial_year_start_month: int = 1,
    start_date: date | None = None,
) -> date:
    """Resolve historical report start date.

    An explicit ``start_date`` wins over every other selector so a caller can
    pin the window to a calendar range. `days` requests an exact rolling window
    ending today (inclusive), instead of the month-aligned window the `months`
    ranges use.
    """
    if start_date is not None:
        return start_date

    if days:
        return today - timedelta(days=days - 1)

    if period == "ytd":
        if not 1 <= financial_year_start_month <= 12:
            raise ValueError("financial_year_start_month must be between 1 and 12")
        year = today.year - (today.month < financial_year_start_month)
        return date(year, financial_year_start_month, 1)

    start = date(today.year, today.month, 1) - timedelta(days=months * 30)
    return start.replace(day=1)


async def _asset_value_at(
    session: AsyncSession, workspace_id: uuid.UUID, cutoff: date,
    primary_currency: str = "USD",
    group_ids: Optional[list[uuid.UUID]] = None,
) -> float:
    """Sum of all active asset values at a given date, converted to primary currency."""
    asset_stmt = select(Asset).where(
        Asset.workspace_id == workspace_id,
        Asset.is_archived == False,
        Asset.sell_date.is_(None),
    )
    if group_ids is not None:
        asset_stmt = asset_stmt.where(Asset.group_id.in_(group_ids))
    asset_result = await session.execute(asset_stmt)
    total = 0.0
    for asset in asset_result.scalars().all():
        val_result = await session.execute(
            select(AssetValue.amount)
            .where(AssetValue.asset_id == asset.id, AssetValue.date <= cutoff)
            .order_by(desc(AssetValue.date), desc(AssetValue.id))
            .limit(1)
        )
        val = val_result.scalar_one_or_none()
        if val is not None:
            amount = float(val)
        elif asset.purchase_price is not None and (
            asset.purchase_date is None or asset.purchase_date <= cutoff
        ):
            amount = float(asset.purchase_price)
        else:
            amount = 0.0
        if amount > 0:
            converted, _ = await convert(
                session, Decimal(str(amount)), asset.currency, primary_currency, cutoff
            )
            total += float(converted)
    return total


async def _net_worth_at(
    session: AsyncSession, workspace_id: uuid.UUID, cutoff: date,
    primary_currency: str = "USD",
    account_ids: Optional[list[uuid.UUID]] = None,
    asset_group_ids: Optional[list[uuid.UUID]] = None,
    accounts: Optional[list[Account]] = None,
    card_debts: Optional[dict] = None,
) -> ReportDataPoint:
    """Compute a single net worth snapshot at a given date, converted to primary currency.

    Under a Collection filter (``account_ids`` set), only those accounts are
    summed and only assets in the collection's wallets (``asset_group_ids``)
    are included.

    ``accounts`` lets a caller that already fetched the (date-independent) open
    account list for this workspace/filter pass it in, instead of re-querying
    it for every snapshot in a multi-point trend. Omit it to fetch as before.
    """
    if accounts is None:
        accounts = await _get_open_accounts(session, workspace_id, account_ids)

    accounts_total = 0.0
    liabilities_total = 0.0
    composition: list[ReportCompositionItem] = []

    for account in accounts:
        debt = (card_debts or {}).get(account.id)
        bal = -debt.at(cutoff) if debt else await _account_balance_at(session, account, cutoff)
        converted, _ = await convert(
            session, Decimal(str(abs(bal))), account.currency, primary_currency, cutoff
        )
        converted_val = float(converted)
        if account.type == "credit_card" or bal < 0:
            liabilities_total += converted_val
            if converted_val > 0:
                composition.append(ReportCompositionItem(
                    key=str(account.id),
                    label=get_account_name(account),
                    value=round(converted_val, 2),
                    color=_ACCOUNT_TYPE_COLORS.get(account.type, "#6B7280"),
                    group="liabilities",
                ))
        else:
            accounts_total += converted_val
            if converted_val > 0:
                composition.append(ReportCompositionItem(
                    key=str(account.id),
                    label=get_account_name(account),
                    value=round(converted_val, 2),
                    color=_ACCOUNT_TYPE_COLORS.get(account.type, "#6B7280"),
                    group="accounts",
                ))

    # Per-asset composition at the cutoff date
    filtered = account_ids is not None
    asset_stmt = select(Asset).where(
        Asset.workspace_id == workspace_id,
        Asset.is_archived == False,
        Asset.sell_date.is_(None),
    )
    if filtered:
        asset_stmt = asset_stmt.where(Asset.group_id.in_(asset_group_ids or []))
    asset_result = await session.execute(asset_stmt)
    assets_total = 0.0
    for asset in asset_result.scalars().all():
        val_result = await session.execute(
            select(AssetValue.amount)
            .where(AssetValue.asset_id == asset.id, AssetValue.date <= cutoff)
            .order_by(desc(AssetValue.date), desc(AssetValue.id))
            .limit(1)
        )
        val = val_result.scalar_one_or_none()
        if val is not None:
            amount = float(val)
        elif asset.purchase_price is not None and (
            asset.purchase_date is None or asset.purchase_date <= cutoff
        ):
            amount = float(asset.purchase_price)
        else:
            amount = 0.0
        if amount > 0:
            converted, _ = await convert(
                session, Decimal(str(amount)), asset.currency, primary_currency, cutoff
            )
            converted_val = round(float(converted), 2)
            assets_total += converted_val
            composition.append(ReportCompositionItem(
                key=str(asset.id),
                label=asset.name,
                value=converted_val,
                color=_ASSET_TYPE_COLORS.get(asset.type, "#6B7280"),
                group="assets",
            ))

    net_worth = accounts_total + assets_total - liabilities_total

    return ReportDataPoint(
        date=cutoff.isoformat(),
        value=round(net_worth, 2),
        breakdowns={
            "accounts": round(accounts_total, 2),
            "assets": round(assets_total, 2),
            "liabilities": round(liabilities_total, 2),
        },
        composition=composition,
    )


def _format_date_label(d: date, interval: str) -> str:
    """Format a date point based on interval granularity."""
    if interval == "daily":
        return d.isoformat()
    elif interval == "weekly":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    elif interval == "monthly":
        return f"{d.year}-{d.month:02d}"
    elif interval == "yearly":
        return str(d.year)
    return d.isoformat()


def _date_points(
    start: date, end: date, interval: str
) -> list[date]:
    """Generate date points between start and end for the given interval."""
    points: list[date] = []
    current = start

    if interval == "daily":
        while current <= end:
            points.append(current)
            current += timedelta(days=1)
    elif interval == "weekly":
        while current <= end:
            points.append(current)
            current += timedelta(weeks=1)
    elif interval == "monthly":
        # One snapshot per month: last day of the month, capped at end
        current = date(start.year, start.month, 1)
        while current <= end:
            last_day = calendar.monthrange(current.year, current.month)[1]
            points.append(min(date(current.year, current.month, last_day), end))
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)
    elif interval == "yearly":
        while current <= end:
            points.append(current)
            current = date(current.year + 1, current.month, current.day)
    else:
        # Default to monthly
        return _date_points(start, end, "monthly")

    # Ensure the last point uses `end` so the final snapshot reflects today's data.
    # If the last generated point is in the same period as `end`, replace it;
    # otherwise append `end` as a new point.
    if points and points[-1] < end:
        if _format_date_label(points[-1], interval) == _format_date_label(end, interval):
            points[-1] = end  # same label, use today's cutoff
        else:
            points.append(end)

    return points


async def _pluggy_card_debts(
    session: AsyncSession, workspace_id: uuid.UUID, accounts: list[Account], today: date,
) -> dict:
    """Pluggy cards in net worth: what their bills owe on each date — closed
    bills not paid yet plus the open bill — rather than the bank's used limit,
    which also reserves every parcel still to come."""
    from app.services.card_liability_service import load_card_bill_debt
    from app.services.dashboard_service import reference_month_cards

    open_ids = {a.id for a in accounts}
    return {
        card.id: await load_card_bill_debt(session, card, today=today)
        for card in await reference_month_cards(session, workspace_id)
        if card.id in open_ids
    }


async def _net_worth_ahead(
    session: AsyncSession,
    point: date,
    today: date,
    primary_currency: str,
    accounts: list[Account],
    card_debts: dict,
    cash: "_CashWalk",
    today_snapshot: ReportDataPoint,
) -> ReportDataPoint:
    """A net worth point after today: the forecast cash balance (every
    recurrence and parcel still to come), the cards' bills still owed on
    that date, and today's assets."""
    async def to_primary(amount: float, ccy: str) -> float:
        if ccy == primary_currency or amount == 0:
            return amount
        converted, _ = await convert(session, Decimal(str(amount)), ccy, primary_currency, today)
        return float(converted)

    async def card_total() -> tuple[float, list[ReportCompositionItem]]:
        total = 0.0
        items: list[ReportCompositionItem] = []
        for account in accounts:
            if account.type != "credit_card":
                continue
            debt = card_debts.get(account.id)
            if debt is not None:
                owed = debt.at(point)
            else:
                # No statements: today's debt until the next due day.
                owed = -float(await _account_balance_at(session, account, today))
                if point >= _next_due(today, account.payment_due_day):
                    owed = 0.0
            owed = await to_primary(max(owed, 0.0), account.currency)
            total += owed
            if owed > 0:
                items.append(ReportCompositionItem(
                    key=str(account.id), label=get_account_name(account), value=round(owed, 2),
                    color=_ACCOUNT_TYPE_COLORS.get(account.type, "#6B7280"), group="liabilities",
                ))
        return total, items

    assets = today_snapshot.breakdowns.get("assets", 0.0)
    asset_items = [c for c in today_snapshot.composition if c.group == "assets"]
    balance = cash.daily_balance.get(point, cash.daily_balance.get(max(cash.daily_balance), 0.0))
    cards, card_items = await card_total()
    liabilities = cards + max(-balance, 0.0)
    value = max(balance, 0.0) + assets - liabilities
    breakdowns = {
        "accounts": round(max(balance, 0.0), 2),
        "assets": round(assets, 2),
        "liabilities": round(liabilities, 2),
    }
    composition = [
        *([ReportCompositionItem(
            key="cash", label="Accounts", value=round(balance, 2), color="#6366F1", group="accounts",
        )] if balance > 0 else []),
        *asset_items,
        *card_items,
    ]
    return ReportDataPoint(date="", value=round(value, 2), breakdowns=breakdowns, composition=composition)


async def get_net_worth_report(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    months: int = 12,
    interval: str = "monthly",
    currency: str = "USD",
    account_ids: Optional[list[uuid.UUID]] = None,
    asset_group_ids: Optional[list[uuid.UUID]] = None,
    period: str | None = None,
    financial_year_start_month: int = 1,
    start_date: date | None = None,
    end_date: date | None = None,
    reference_date: date | None = None,
    past_months: int | None = None,
    future_months: int | None = None,
) -> ReportResponse:
    """Build a full ReportResponse for net worth over time.

    With ``past_months``/``future_months`` the window runs from that many
    months before ``reference_date`` (default today) to that many after it;
    points after today are forecasts (see ``_net_worth_ahead``)."""
    # A wallet-only collection (wallets, no accounts) still filters.
    if asset_group_ids is not None and account_ids is None:
        account_ids = []
    real_today = app_today()
    windowed = past_months is not None or future_months is not None
    if windowed:
        start, axis_end = _window(reference_date or real_today, past_months or 0, future_months or 0, interval)
    else:
        axis_end = end_date or real_today
        start = _report_start_date(
            axis_end, months, period, financial_year_start_month=financial_year_start_month,
            start_date=start_date,
        )

    # Get user's primary currency
    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency

    points = _date_points(start, axis_end, interval)

    # Fetch the open-account list once: it does not depend on the snapshot
    # date, so every point below reuses it instead of re-querying it.
    accounts = await _get_open_accounts(session, workspace_id, account_ids)
    card_debts = await _pluggy_card_debts(
        session, workspace_id, accounts, real_today,
    )
    ahead = [p for p in points if p > real_today]
    cash = today_snapshot = None
    if ahead:
        cash = await _cash_walk(
            session, workspace_id, account_ids, primary_currency, real_today, real_today, axis_end,
        )
        today_snapshot = await _net_worth_at(
            session, workspace_id, real_today, primary_currency, account_ids, asset_group_ids,
            accounts=accounts, card_debts=card_debts,
        )

    # Compute snapshot at each date point
    trend: list[ReportDataPoint] = []
    for point in points:
        if point > real_today:
            dp = await _net_worth_ahead(
                session, point, real_today, primary_currency, accounts, card_debts,
                cash, today_snapshot,
            )
        else:
            dp = await _net_worth_at(
                session, workspace_id, point, primary_currency, account_ids, asset_group_ids,
                accounts=accounts, card_debts=card_debts,
            )
        dp.date = _format_date_label(point, interval)
        dp.change = round(dp.value - trend[-1].value, 2) if trend else None
        trend.append(dp)

    # Current snapshot (last point) for summary; baseline at period start for delta
    current = trend[-1] if trend else ReportDataPoint(
        date="", value=0, breakdowns={"accounts": 0, "assets": 0, "liabilities": 0}
    )
    if windowed:
        # The summary reads the reference date, not the end of the window.
        reference = reference_date or real_today
        if reference > real_today:
            if cash is None:
                cash = await _cash_walk(
                    session, workspace_id, account_ids, primary_currency, real_today, real_today, reference,
                )
                today_snapshot = await _net_worth_at(
                    session, workspace_id, real_today, primary_currency, account_ids, asset_group_ids,
                    accounts=accounts, card_debts=card_debts,
                )
            current = await _net_worth_ahead(
                session, reference, real_today, primary_currency, accounts, card_debts,
                cash, today_snapshot,
            )
        else:
            current = await _net_worth_at(
                session, workspace_id, reference, primary_currency, account_ids, asset_group_ids,
                accounts=accounts, card_debts=card_debts,
            )
    # The first trend point's *actual* cutoff (`points[0]`, before its `.date`
    # was overwritten with a formatted label) equals `start` for every
    # interval except monthly, which snaps to month-end. Only reuse it then —
    # an approximate match would silently swap in the wrong day's snapshot.
    if trend and points[0] == start:
        baseline = trend[0]
    else:
        baseline = await _net_worth_at(
            session, workspace_id, min(start, real_today), primary_currency, account_ids, asset_group_ids,
            accounts=accounts, card_debts=card_debts,
        )
    previous = baseline if trend else current

    change_amount = current.value - previous.value
    change_percent = (
        (change_amount / abs(previous.value) * 100)
        if previous.value != 0
        else None
    )

    summary = ReportSummary(
        primary_value=current.value,
        change_amount=round(change_amount, 2),
        change_percent=round(change_percent, 2) if change_percent is not None else None,
        breakdowns=[
            ReportBreakdown(
                key="accounts",
                label="Accounts",
                value=current.breakdowns.get("accounts", 0),
                color="#6366F1",
            ),
            ReportBreakdown(
                key="assets",
                label="Assets",
                value=current.breakdowns.get("assets", 0),
                color="#F59E0B",
            ),
            ReportBreakdown(
                key="liabilities",
                label="Liabilities",
                value=current.breakdowns.get("liabilities", 0),
                color="#F43F5E",
            ),
        ],
    )

    meta = ReportMeta(
        type="net_worth",
        series_keys=["accounts", "assets", "liabilities"],
        currency=primary_currency,
        interval=interval,
        forecast_start_date=_format_date_label(real_today, interval) if ahead else None,
        reference_date=(reference_date or real_today).isoformat() if windowed else None,
        window_start=start.isoformat() if windowed else None,
        window_end=axis_end.isoformat() if windowed else None,
    )

    # Reuse the selected end date's per-item composition for the response-level
    # `composition` field (final snapshot for the donut).
    composition = current.composition if windowed else (trend[-1].composition if trend else [])

    return ReportResponse(summary=summary, trend=trend, meta=meta, composition=composition)


def _interval_label_expr(interval: str, date_col=None):
    """SQL expression that groups transaction dates into interval buckets."""
    col = date_col if date_col is not None else Transaction.date
    if interval == "daily":
        return func.to_char(col, 'YYYY-MM-DD')
    elif interval == "weekly":
        return func.concat(
            func.extract('isoyear', col).cast(String),
            '-W',
            func.lpad(func.extract('week', col).cast(String), 2, '0'),
        )
    elif interval == "yearly":
        return func.to_char(col, 'YYYY')
    else:  # monthly (default)
        return func.to_char(col, 'YYYY-MM')


async def get_income_expenses_report(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    months: int = 12,
    interval: str = "monthly",
    currency: str = "USD",
    account_ids: Optional[list[uuid.UUID]] = None,
    period: str | None = None,
    days: int | None = None,
    financial_year_start_month: int = 1,
    start_date: date | None = None,
    end_date: date | None = None,
    reference_date: date | None = None,
    past_months: int | None = None,
    future_months: int | None = None,
) -> ReportResponse:
    """Build income/expenses; explicit custom windows include actuals only.

    With ``past_months``/``future_months`` the window runs from that many
    months before ``reference_date`` (default today) to that many after it.
    From the current month on, every recurrence and parcel still to come
    is forecast (``projectedIncome``/``projectedExpenses``) unless the user
    skipped that occurrence."""
    filtered = account_ids is not None
    acct_filter = [Transaction.account_id.in_(account_ids)] if filtered else []
    real_today = app_today()
    windowed = past_months is not None or future_months is not None
    if windowed:
        start, axis_end = _window(reference_date or real_today, past_months or 0, future_months or 0, interval)
    else:
        axis_end = end_date or real_today
        start = _report_start_date(
            axis_end,
            months,
            period=period,
            days=days,
            financial_year_start_month=financial_year_start_month,
            start_date=start_date,
        )
    # Actuals never go past today, whatever the window.
    actual_end = min(axis_end, real_today)

    # Get user's primary currency + global reporting mode
    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency
    accounting_mode = await get_credit_card_accounting_mode(session)
    report_date = reporting_date_col(accounting_mode)

    label_expr = _interval_label_expr(interval, report_date).label('period')

    # A card row already posted belongs to the actuals of its statement's
    # month even when that bill is due later this month (RFC toledo-features
    # §6.2): it happened, only its bucket date is ahead. Custom windows keep
    # their exact bounds.
    month_last_day = _add_months(actual_end.replace(day=1), 1) - timedelta(days=1)
    actual_window = (
        report_date <= actual_end
        if end_date is not None
        else or_(
            report_date <= actual_end,
            and_(
                Transaction.effective_date_source.is_not(None),
                Transaction.date <= actual_end,
                report_date <= month_last_day,
            ),
        )
    )

    # A card statement line is a charge whether the bank still shows it as
    # pending or not — the statement counts it, and the pending copies of
    # posted rows are flagged duplicates (ignored) by recompute.
    actual_status = or_(Transaction.status == "posted", Transaction.card_line_kind.is_not(None))

    def _counted_as_actual(tx: Transaction) -> bool:
        """Forecast rows the actual queries above already counted."""
        if end_date is not None or tx.effective_date_source is None or tx.date > actual_end:
            return False
        if tx.status != "posted" and tx.card_line_kind is None:
            return False
        return reporting_date(tx, accounting_mode) <= month_last_day

    # Use amount_primary when available, fall back to amount
    amount_expr = func.coalesce(Transaction.amount_primary, Transaction.amount)

    # Card refunds lower their statement's spending instead of counting as
    # income (RFC toledo-features §6.4: a statement's spending nets them).
    refund = is_card_refund()
    result = await session.execute(
        select(
            label_expr,
            func.sum(case((and_(Transaction.type == "credit", ~refund), amount_expr), else_=0)),
            func.sum(case(
                (Transaction.type == "debit", func.abs(amount_expr)),
                (refund, -func.abs(amount_expr)),
                else_=0,
            )),
        )
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Account.is_closed == False,
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            actual_status,
            counts_as_user_pnl(),
            *acct_filter,
        )
        .group_by(label_expr)
        .order_by(label_expr)
    )

    # Build the actual (posted-only) data map. Forecast rows are kept in a
    # separate map below so the report can expose the same distinction as
    # balances: actual income/expenses never silently absorb pending entries.
    data_map: dict[str, tuple[float, float]] = {}
    for row in result.all():
        income = float(row[1] or 0)
        expenses = max(0.0, float(row[2] or 0))
        data_map[row[0]] = (income, expenses)

    # Subtract non-owner shares of the user's own splits per period — the
    # user shouldn't be charged for the parts they're owed back.
    from sqlalchemy import or_ as _or_, and_ as _and_
    from app.models.group import Group as _Group_, GroupMember as _GroupMember_
    from app.models.transaction_split import TransactionSplit as _TS_
    from app.services.fx_rate_service import convert as fx_convert

    own_member_ids_sq = (
        select(_GroupMember_.id)
        .outerjoin(_Group_, _Group_.id == _GroupMember_.group_id)
        .where(
            _or_(
                _GroupMember_.linked_user_id == user_id,
                _and_(_GroupMember_.is_self == True, _Group_.user_id == user_id),
            )
        )
    )
    owner_offset_result = await session.execute(
        select(
            label_expr,
            Transaction.currency,
            func.sum(
                case(
                    (Transaction.type == "credit", _TS_.share_amount),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (Transaction.type == "debit", _TS_.share_amount),
                    else_=0,
                )
            ),
        )
        .select_from(_TS_)
        .join(Transaction, _TS_.transaction_id == Transaction.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.workspace_id == workspace_id,
            _TS_.group_member_id.notin_(own_member_ids_sq),
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            actual_status,
            counts_as_user_pnl(),
        )
        .group_by(label_expr, Transaction.currency)
    )
    for row in (owner_offset_result.all() if not filtered else []):
        period, currency, raw_credit, raw_debit = row
        sub_inc = 0.0
        sub_exp = 0.0
        if raw_credit:
            inc_pri, _ = await fx_convert(
                session, Decimal(str(raw_credit)), currency, primary_currency
            )
            sub_inc = float(inc_pri)
        if raw_debit:
            exp_pri, _ = await fx_convert(
                session, Decimal(str(raw_debit)), currency, primary_currency
            )
            sub_exp = abs(float(exp_pri))
        existing_income, existing_expenses = data_map.get(period, (0.0, 0.0))
        data_map[period] = (
            max(0.0, existing_income - sub_inc),
            max(0.0, existing_expenses - sub_exp),
        )

    # Layer in the viewer's share from group splits — concert tickets
    # paid by a friend show up as the viewer's expense in their P/L
    # picture, just like in /transactions and the dashboard. Group by
    # currency so we can FX-convert each bucket to primary correctly.
    from app.models.group import GroupMember
    from app.models.transaction_split import TransactionSplit

    # Exclude is_self memberships — the owner's own self-member must
    # not surface their in-Pessoal transactions in Trabalho's report.
    viewer_member_ids = select(GroupMember.id).where(
        GroupMember.linked_user_id == user_id,
        GroupMember.is_self.is_(False),
    )
    shared_result = await session.execute(
        select(
            label_expr,
            Transaction.currency,
            func.sum(
                case(
                    (Transaction.type == "credit", TransactionSplit.share_amount),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (Transaction.type == "debit", TransactionSplit.share_amount),
                    else_=0,
                )
            ),
        )
        .select_from(TransactionSplit)
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .where(
            TransactionSplit.group_member_id.in_(viewer_member_ids),
            Transaction.user_id != user_id,
            Transaction.workspace_id != workspace_id,
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            actual_status,
            counts_as_user_pnl(),
        )
        .group_by(label_expr, Transaction.currency)
    )
    from app.services.fx_rate_service import convert as fx_convert
    for row in (shared_result.all() if not filtered else []):
        period, currency, raw_credit, raw_debit = row
        share_income_pri = 0.0
        share_expenses_pri = 0.0
        if raw_credit:
            inc_pri, _ = await fx_convert(
                session, Decimal(str(raw_credit)), currency, primary_currency
            )
            share_income_pri = float(inc_pri)
        if raw_debit:
            exp_pri, _ = await fx_convert(
                session, Decimal(str(raw_debit)), currency, primary_currency
            )
            share_expenses_pri = abs(float(exp_pri))
        existing_income, existing_expenses = data_map.get(period, (0.0, 0.0))
        data_map[period] = (
            existing_income + share_income_pri,
            existing_expenses + share_expenses_pri,
        )

    # Custom windows report only actuals, including when they end today.
    # Presets retain their existing full-month estimates.
    include_forecasts = start_date is None and end_date is None
    forecast_until = axis_end if windowed else real_today
    forecast_map: dict[str, tuple[float, float]] = {}

    # Add recurring projections for each month in the range (consistent with dashboard)
    from app.services.dashboard_service import _month_range, _get_recurring_projections

    projection_cache: dict = {}

    cursor = start
    while include_forecasts and cursor <= forecast_until:
        m_start, m_end = _month_range(cursor)
        # A day-window range can start mid-month: only project occurrences that
        # fall inside the requested window.
        projections = await _get_recurring_projections(
            session, workspace_id, max(m_start, start), m_end, account_ids, cache=projection_cache,
        )
        for proj in projections:
            # Convert to primary currency
            converted, _ = await fx_convert(
                session, Decimal(str(proj["amount"])), proj["currency"], primary_currency,
            )
            proj_amount = float(converted)
            label = _format_date_label(cursor, interval)
            existing_income, existing_expenses = forecast_map.get(label, (0.0, 0.0))
            if proj["type"] == "credit":
                forecast_map[label] = (existing_income + proj_amount, existing_expenses)
            else:
                forecast_map[label] = (existing_income, existing_expenses + proj_amount)
        forecast_transactions = await _get_forecast_transactions(
            session, workspace_id, max(m_start, start), m_end, account_ids,
            range_date_col=report_date,
        )
        for tx in forecast_transactions:
            if not _counts_as_user_pnl_row(tx) or _counted_as_actual(tx):
                continue
            if tx.amount_primary is not None:
                amount = abs(float(tx.amount_primary))
            else:
                converted, _ = await fx_convert(
                    session, Decimal(str(abs(tx.amount))), tx.currency, primary_currency,
                )
                amount = abs(float(converted))
            tx_report_date = reporting_date(tx, accounting_mode)
            label = _format_date_label(tx_report_date, interval)
            existing_income, existing_expenses = forecast_map.get(label, (0.0, 0.0))
            if tx.type == "credit" and tx.card_line_kind == "credit":
                forecast_map[label] = (existing_income, existing_expenses - amount)
            elif tx.type == "credit":
                forecast_map[label] = (existing_income + amount, existing_expenses)
            else:
                forecast_map[label] = (existing_income, existing_expenses + amount)

        # Invoices still owed, at the date they were promised for. Only
        # the unallocated balance, so a claim and the payment that
        # settles it never both land in the same month. Skipped when the
        # report is narrowed to particular accounts: a claim has no
        # account until somebody pays it.
        # `is None`, not falsy: a collection holding only wallets narrows
        # the report to an empty set of bank accounts, and an empty list
        # means filtered to nothing rather than not filtered at all.
        if account_ids is None:
            for claim in await invoice_forecast_service.claims_in_range(
                session, workspace_id, max(m_start, start), m_end
            ):
                converted, _ = await fx_convert(
                    session, claim.amount, claim.currency, primary_currency,
                )
                claim_amount = abs(float(converted))
                label = _format_date_label(claim.due_date, interval)
                existing_income, existing_expenses = forecast_map.get(label, (0.0, 0.0))
                if claim.direction == "receivable":
                    forecast_map[label] = (existing_income + claim_amount, existing_expenses)
                else:
                    forecast_map[label] = (existing_income, existing_expenses + claim_amount)

        # Advance to next month
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)

    current_label = _format_date_label(real_today, "monthly")

    # Generate all expected date points and map to results
    points = _date_points(start, axis_end, interval)
    trend: list[ReportDataPoint] = []
    total_income = 0.0
    total_expenses = 0.0
    for point in points:
        label = _format_date_label(point, interval)
        income, expenses = data_map.get(label, (0.0, 0.0))
        forecast_income, forecast_expenses = forecast_map.get(label, (0.0, 0.0))
        # The report always assumes every recurrence still to come happens,
        # so a month's net counts what is forecast as well as what posted.
        net = round(income + forecast_income - expenses - forecast_expenses, 2)
        total_income += income
        total_expenses += expenses
        breakdowns = {
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "projectedIncome": round(forecast_income, 2),
            "projectedExpenses": round(forecast_expenses, 2),
        }
        trend.append(ReportDataPoint(
            date=label,
            value=net,
            breakdowns=breakdowns,
        ))

    # A daily preset's visible axis ends today, but the monthly forecast query
    # can still contain future installments/generate-ahead rows in the current
    # month. Keep those rows in the projected summary even when they have no
    # historical point to render yet.
    projected_income = sum(income for income, _ in forecast_map.values())
    projected_expenses = sum(expenses for _, expenses in forecast_map.values())

    total_net = round(total_income + projected_income - total_expenses - projected_expenses, 2)

    # Compare last point vs first point net income
    current_net = trend[-1].value if trend else 0.0
    previous_net = trend[0].value if len(trend) > 1 else 0.0
    change_amount = current_net - previous_net
    change_percent = (
        (change_amount / abs(previous_net) * 100)
        if previous_net != 0
        else None
    )

    summary = ReportSummary(
        primary_value=total_net,
        change_amount=round(change_amount, 2),
        change_percent=round(change_percent, 2) if change_percent is not None else None,
        breakdowns=[
            ReportBreakdown(
                key="income",
                label="Income",
                value=round(total_income, 2),
                color="#10B981",
            ),
            ReportBreakdown(
                key="expenses",
                label="Expenses",
                value=round(total_expenses, 2),
                color="#F43F5E",
            ),
            ReportBreakdown(
                key="projectedIncome",
                label="Projected Income",
                value=round(projected_income, 2),
                color="#34D399",
            ),
            ReportBreakdown(
                key="projectedExpenses",
                label="Projected Expenses",
                value=round(projected_expenses, 2),
                color="#FB7185",
            ),
            ReportBreakdown(
                key="netIncome",
                label="Net Income",
                value=total_net,
                color="#6366F1",
            ),
        ],
    )

    meta = ReportMeta(
        type="income_expenses",
        series_keys=["income", "expenses", "projectedIncome", "projectedExpenses"],
        currency=primary_currency,
        interval=interval,
        forecast_start_date=current_label if windowed and axis_end > real_today else None,
        reference_date=(reference_date or real_today).isoformat() if windowed else None,
        window_start=start.isoformat() if windowed else None,
        window_end=axis_end.isoformat() if windowed else None,
    )

    # Build per-category composition for the full date range
    cat_result = await session.execute(
        select(
            Category.id,
            Category.name,
            Category.color,
            Transaction.type,
            refund.label("is_refund"),
            func.sum(amount_expr),
        )
        .select_from(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Account.is_closed == False,
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            actual_status,
            counts_as_user_pnl(),
            *acct_filter,
        )
        .group_by(Category.id, Category.name, Category.color, Transaction.type, "is_refund")
    )

    # Collect composition into a mutable map so projections can be added
    # Key: (cat_key, group) -> {label, color, value}. Card refunds go to
    # their category's expenses as a negative amount.
    comp_map: dict[tuple[str, str], dict] = {}
    for row in cat_result.all():
        cat_id, cat_name, cat_color, txn_type, is_refund, total_amount = row
        amount = abs(float(total_amount or 0))
        if amount <= 0:
            continue
        cat_key = str(cat_id) if cat_id else "uncategorized"
        group = "income" if txn_type == "credit" and not is_refund else "expenses"
        entry = comp_map.setdefault((cat_key, group), {
            "label": cat_name if cat_name else "Uncategorized",
            "color": cat_color if cat_color else "#6B7280",
            "value": 0.0,
        })
        entry["value"] += -amount if is_refund else amount

    # Subtract non-owner shares of own splits from composition (debit only —
    # owner_split_offset_by_category is debit-only). Keeps the report's
    # composition consistent with summary totals under share-only model.
    full_range_offset = {} if filtered else await owner_split_offset_by_category(
        session, user_id, start, actual_end + timedelta(days=1),
        use_effective_date=accounting_mode == "accrual",
        primary_currency=primary_currency,
    )
    for cat_uuid, offset_total in full_range_offset.items():
        cat_key = str(cat_uuid) if cat_uuid else "uncategorized"
        comp_key = (cat_key, "expenses")
        if comp_key in comp_map:
            comp_map[comp_key]["value"] -= offset_total
            if comp_map[comp_key]["value"] <= 0:
                comp_map.pop(comp_key)

    # Investment-style outflows: transactions in `treat_as_transfer` categories
    # are excluded from P&L by counts_as_user_pnl (an investment application's
    # counterpart is an Asset/Holding, not spending). But for the cashflow Sankey
    # we surface them as their own "investments" group — money set aside, distinct
    # from spending and from leftover surplus (mirrors how Sure shows an
    # "Investment Contributions" node). Paired account transfers stay excluded via
    # the transfer_pair_id filter, so only one-sided movements (contributions)
    # appear here.
    inv_result = await session.execute(
        select(
            Category.id,
            Category.name,
            Category.color,
            func.sum(amount_expr),
        )
        .select_from(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Account.is_closed == False,
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            Transaction.type == "debit",
            actual_status,
            Transaction.transfer_pair_id.is_(None),
            Transaction.is_ignored.is_(False),
            Transaction.exclude_from_pnl.is_(False),
            Category.treat_as_transfer.is_(True),
            Category.is_ignored.is_(False),
            *acct_filter,
        )
        .group_by(Category.id, Category.name, Category.color)
    )
    for cat_id, cat_name, cat_color, total_amount in inv_result.all():
        amount = abs(float(total_amount or 0))
        if amount <= 0:
            continue
        comp_map[(str(cat_id), "investments")] = {
            "label": cat_name or "Investments",
            "color": cat_color or "#0EA5E9",
            "value": amount,
        }

    # Build per-category trend (sparklines) for the full date range
    cat_trend_result = await session.execute(
        select(
            label_expr,
            Category.id,
            Category.name,
            Category.color,
            Transaction.type,
            refund.label("is_refund"),
            func.sum(amount_expr),
        )
        .select_from(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Account.is_closed == False,
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            actual_status,
            counts_as_user_pnl(),
            *acct_filter,
        )
        .group_by(label_expr, Category.id, Category.name, Category.color, Transaction.type, "is_refund")
    )

    # Collect into dict[(cat_key, group)] -> {label, color, total, periods}
    cat_trend_map: dict[tuple[str, str], dict] = {}
    for row in cat_trend_result.all():
        period_label, cat_id, cat_name, cat_color, txn_type, is_refund, total_amount = row
        amount = abs(float(total_amount or 0))
        if amount <= 0:
            continue
        if is_refund:
            amount = -amount
        cat_key = str(cat_id) if cat_id else "uncategorized"
        group = "income" if txn_type == "credit" and not is_refund else "expenses"
        map_key = (cat_key, group)
        if map_key not in cat_trend_map:
            cat_trend_map[map_key] = {
                "label": cat_name if cat_name else "Uncategorized",
                "color": cat_color if cat_color else "#6B7280",
                "total": 0.0,
                "periods": {},
            }
        cat_trend_map[map_key]["total"] += amount
        cat_trend_map[map_key]["periods"][period_label] = (
            cat_trend_map[map_key]["periods"].get(period_label, 0.0) + amount
        )

    # Subtract non-owner shares of own splits per (period, category) — keeps
    # the per-category trend consistent with the share-only summary.
    cat_offset_result = await session.execute(
        select(
            label_expr,
            Transaction.category_id,
            Transaction.currency,
            func.sum(_TS_.share_amount),
        )
        .select_from(_TS_)
        .join(Transaction, _TS_.transaction_id == Transaction.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.workspace_id == workspace_id,
            Transaction.type == "debit",
            _TS_.group_member_id.notin_(own_member_ids_sq),
            report_date >= start,
            actual_window,
            Transaction.source != "opening_balance",
            actual_status,
            counts_as_user_pnl(),
        )
        .group_by(label_expr, Transaction.category_id, Transaction.currency)
    )
    for period_label, cat_id, currency, raw_total in (cat_offset_result.all() if not filtered else []):
        if not raw_total:
            continue
        cat_key = str(cat_id) if cat_id else "uncategorized"
        offset_pri, _ = await fx_convert(
            session, Decimal(str(raw_total)), currency, primary_currency
        )
        offset = float(offset_pri)
        map_key = (cat_key, "expenses")
        if map_key not in cat_trend_map:
            continue
        cat_trend_map[map_key]["total"] = max(
            0.0, cat_trend_map[map_key]["total"] - offset
        )
        cur_period = cat_trend_map[map_key]["periods"].get(period_label, 0.0)
        cat_trend_map[map_key]["periods"][period_label] = max(0.0, cur_period - offset)
    # Drop categories that fully zeroed out
    for key in list(cat_trend_map.keys()):
        if cat_trend_map[key]["total"] <= 0:
            cat_trend_map.pop(key)

    # Add recurring projections to composition and category trend
    cat_cache: dict[str, dict] = {}
    cursor2 = start
    while include_forecasts and cursor2 <= forecast_until:
        m_start, m_end = _month_range(cursor2)
        projections = await _get_recurring_projections(
            session, workspace_id, m_start, m_end, account_ids, cache=projection_cache,
        )
        period_label = _format_date_label(cursor2, interval)
        for proj in projections:
            cat_id_str = str(proj["category_id"]) if proj["category_id"] else "uncategorized"
            group = "income" if proj["type"] == "credit" else "expenses"

            # Fetch category info if needed
            if cat_id_str != "uncategorized" and cat_id_str not in cat_cache:
                cat_row = await session.execute(
                    select(Category.name, Category.color)
                    .where(Category.id == proj["category_id"])
                )
                row = cat_row.one_or_none()
                cat_cache[cat_id_str] = {
                    "label": row[0] if row else "Uncategorized",
                    "color": row[1] if row else "#6B7280",
                }

            info = cat_cache.get(cat_id_str, {"label": "Uncategorized", "color": "#6B7280"})

            # Convert projection amount to primary currency
            converted, _ = await fx_convert(
                session, Decimal(str(proj["amount"])), proj["currency"], primary_currency,
            )
            proj_amount = float(converted)

            # Add to composition map
            comp_key = (cat_id_str, group)
            if comp_key in comp_map:
                comp_map[comp_key]["value"] += proj_amount
            else:
                comp_map[comp_key] = {
                    "label": info["label"],
                    "color": info["color"],
                    "value": proj_amount,
                }

            # Add to category trend map
            if comp_key not in cat_trend_map:
                cat_trend_map[comp_key] = {
                    "label": info["label"],
                    "color": info["color"],
                    "total": 0.0,
                    "periods": {},
                }
            cat_trend_map[comp_key]["total"] += proj_amount
            cat_trend_map[comp_key]["periods"][period_label] = (
                cat_trend_map[comp_key]["periods"].get(period_label, 0.0) + proj_amount
            )

        forecast_transactions = await _get_forecast_transactions(
            session, workspace_id, m_start, m_end, account_ids,
            range_date_col=report_date,
        )
        for tx in forecast_transactions:
            if not _counts_as_user_pnl_row(tx) or _counted_as_actual(tx):
                continue
            cat_id_str = str(tx.category_id) if tx.category_id else "uncategorized"
            tx_refund = tx.type == "credit" and tx.card_line_kind == "credit"
            group = "income" if tx.type == "credit" and not tx_refund else "expenses"
            tx_report_date = reporting_date(tx, accounting_mode)
            forecast_period_label = _format_date_label(tx_report_date, interval)
            if cat_id_str != "uncategorized" and cat_id_str not in cat_cache:
                cat_row = await session.execute(
                    select(Category.name, Category.color).where(Category.id == tx.category_id)
                )
                row = cat_row.one_or_none()
                cat_cache[cat_id_str] = {
                    "label": row[0] if row else "Uncategorized",
                    "color": row[1] if row else "#6B7280",
                }
            info = cat_cache.get(
                cat_id_str, {"label": "Uncategorized", "color": "#6B7280"}
            )
            if tx.amount_primary is not None:
                forecast_amount = abs(float(tx.amount_primary))
            else:
                converted, _ = await fx_convert(
                    session, Decimal(str(abs(tx.amount))), tx.currency, primary_currency,
                )
                forecast_amount = abs(float(converted))
            if tx_refund:
                forecast_amount = -forecast_amount
            comp_key = (cat_id_str, group)
            if comp_key in comp_map:
                comp_map[comp_key]["value"] += forecast_amount
            else:
                comp_map[comp_key] = {
                    "label": info["label"],
                    "color": info["color"],
                    "value": forecast_amount,
                }
            if comp_key not in cat_trend_map:
                cat_trend_map[comp_key] = {
                    "label": info["label"],
                    "color": info["color"],
                    "total": 0.0,
                    "periods": {},
                }
            cat_trend_map[comp_key]["total"] += forecast_amount
            cat_trend_map[comp_key]["periods"][forecast_period_label] = (
                cat_trend_map[comp_key]["periods"].get(forecast_period_label, 0.0)
                + forecast_amount
            )

        if cursor2.month == 12:
            cursor2 = date(cursor2.year + 1, 1, 1)
        else:
            cursor2 = date(cursor2.year, cursor2.month + 1, 1)

    # Build final composition list from map
    composition: list[ReportCompositionItem] = []
    for (cat_key, group), info in comp_map.items():
        if info["value"] <= 0:
            continue
        composition.append(ReportCompositionItem(
            key=cat_key,
            label=info["label"],
            value=round(info["value"], 2),
            color=info["color"],
            group=group,
        ))

    # Build period labels from the same points used by the trend
    period_labels = [_format_date_label(p, interval) for p in points]

    # Top 6 + Other per group
    category_trend: list[CategoryTrendItem] = []
    for group in ("expenses", "income"):
        group_items = [
            (k, v) for (k, g), v in cat_trend_map.items() if g == group
        ]
        group_items.sort(key=lambda x: x[1]["total"], reverse=True)
        top = group_items[:CATEGORY_TREND_TOP_N]
        rest = group_items[CATEGORY_TREND_TOP_N:]

        for cat_key, info in top:
            series = [
                ReportDataPoint(
                    date=pl,
                    value=round(info["periods"].get(pl, 0.0), 2),
                    breakdowns={},
                )
                for pl in period_labels
            ]
            category_trend.append(CategoryTrendItem(
                key=cat_key,
                label=info["label"],
                color=info["color"],
                total=round(info["total"], 2),
                group=group,
                series=series,
            ))

        if rest:
            other_total = sum(v["total"] for _, v in rest)
            other_periods: dict[str, float] = {}
            for _, v in rest:
                for pl, amt in v["periods"].items():
                    other_periods[pl] = other_periods.get(pl, 0.0) + amt
            series = [
                ReportDataPoint(
                    date=pl,
                    value=round(other_periods.get(pl, 0.0), 2),
                    breakdowns={},
                )
                for pl in period_labels
            ]
            category_trend.append(CategoryTrendItem(
                key="other",
                label="Other",
                color="#6B7280",
                total=round(other_total, 2),
                group=group,
                series=series,
            ))

    # Populate each trend point's composition from the per-period category data,
    # so the frontend can show a per-period donut when a bar is clicked.
    for dp in trend:
        for (cat_key, group), info in cat_trend_map.items():
            v = info["periods"].get(dp.date, 0.0)
            if v > 0:
                dp.composition.append(ReportCompositionItem(
                    key=cat_key,
                    label=info["label"],
                    value=round(v, 2),
                    color=info["color"],
                    group=group,
                ))

    return ReportResponse(
        summary=summary, trend=trend, meta=meta,
        composition=composition, category_trend=category_trend,
    )


def _window(reference: date, past: int, future: int, interval: str = "daily") -> tuple[date, date]:
    """From ``past`` months before ``reference`` to ``future`` months after
    it, as (start, end). Monthly and yearly windows take whole months: from
    the first day of the first one, to the last day of the last one when the
    window looks ahead (a window only looking back ends on its reference)."""
    start = _add_months(reference, -past)
    end = _add_months(reference, future)
    if interval in ("monthly", "yearly"):
        start = start.replace(day=1)
        if future > 0:
            end = _add_months(end.replace(day=1), 1) - timedelta(days=1)
    return start, end


def _add_months(d: date, months: int) -> date:
    """Advance a date by N months, clamping the day to the target month's length."""
    new_month = d.month + months
    new_year = d.year + (new_month - 1) // 12
    new_month = ((new_month - 1) % 12) + 1
    last_day = calendar.monthrange(new_year, new_month)[1]
    new_day = min(d.day, last_day)
    return date(new_year, new_month, new_day)


_BASELINE_MAX_LOOKBACK_MONTHS = 12
_PAST_HISTORY_MONTHS = 1


async def _get_baseline_projection(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    today: date,
    end: date,
    primary_currency: str,
    to_primary,
    account_ids: Optional[list[uuid.UUID]] = None,
    *,
    cash_view: bool = False,
) -> tuple[list[dict], int]:
    """Estimate future flows by averaging the user's recent transaction history.

    Replaces deterministic recurring projections when baseline mode is on:
    sums all non-ignored, P&L-counting transactions inside the look-back
    window, splits into mean daily inflow and outflow, then emits one
    synthetic flow per day from today+1 to end.

    The look-back window is **adaptive**: capped at
    ``_BASELINE_MAX_LOOKBACK_MONTHS`` (12) but shrunk to the user's earliest
    qualifying transaction date when they have less history. This means a
    user with 4 months of activity gets a 4-month average; a user with
    3 years gets a 12-month average. More history → more stable estimate;
    less history → still works, just noisier.

    Symmetric for income and expense — fixes the "no recurring salary set
    up, chart looks catastrophic" case as well as the "no recurring expenses
    set up, chart looks rosy" case.

    Returns ``(projections, lookback_days)`` so the caller can surface the
    actual window used in the response (zero when there's no history).

    ``cash_view`` averages what actually entered and left the accounts
    instead of P&L: a salary filed under a transfer-like category still
    arrives, an investment application still leaves. Paired transfers stay
    out — between two accounts they net to zero, and a card bill payment is
    projected from the card's statements instead.
    """
    acct_filter = [Transaction.account_id.in_(account_ids)] if account_ids is not None else []
    if cash_view:
        counted = and_(
            Transaction.transfer_pair_id.is_(None),
            Transaction.is_ignored.is_(False),
            or_(
                Transaction.category_id.is_(None),
                Transaction.category_id.not_in(select(Category.id).where(Category.is_ignored.is_(True))),
            ),
        )
    else:
        counted = counts_as_pnl()
    cap_start = _add_months(today, -_BASELINE_MAX_LOOKBACK_MONTHS)
    earliest_result = await session.execute(
        select(func.min(Transaction.date))
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Account.is_closed == False,
            Transaction.date <= today,
            Transaction.source != "opening_balance",
            Transaction.status == "posted",
            counted,
            *acct_filter,
        )
    )
    earliest_date = earliest_result.scalar_one_or_none()
    if earliest_date is None:
        return [], 0
    window_start = max(earliest_date, cap_start)

    rows = await session.execute(
        select(
            Transaction.type,
            Transaction.amount,
            Transaction.amount_primary,
            Transaction.currency,
        )
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Account.is_closed == False,
            Transaction.date >= window_start,
            Transaction.date <= today,
            Transaction.source != "opening_balance",
            Transaction.status == "posted",
            counted,
            *acct_filter,
        )
    )
    total_inflow_primary = 0.0
    total_outflow_primary = 0.0
    for tx_type, amt, amt_primary, ccy in rows.all():
        if amt_primary is not None:
            amount = float(amt_primary)
        else:
            amount = await to_primary(Decimal(str(amt or 0)), ccy)
        if amount == 0:
            continue
        if tx_type == "credit":
            total_inflow_primary += abs(amount)
        else:
            total_outflow_primary += abs(amount)

    lookback_days = max((today - window_start).days, 1)
    daily_inflow = total_inflow_primary / lookback_days
    daily_outflow = total_outflow_primary / lookback_days

    projections: list[dict] = []
    if daily_inflow == 0 and daily_outflow == 0:
        return projections, lookback_days

    cursor = today + timedelta(days=1)
    while cursor <= end:
        if daily_inflow > 0:
            projections.append({
                "date": cursor,
                "amount": daily_inflow,
                "currency": primary_currency,
                "type": "credit",
                "category_id": None,
            })
        if daily_outflow > 0:
            projections.append({
                "date": cursor,
                "amount": daily_outflow,
                "currency": primary_currency,
                "type": "debit",
                "category_id": None,
            })
        cursor = cursor + timedelta(days=1)
    return projections, lookback_days


@dataclass
class _CashFlowScope:
    cash_ids: list[uuid.UUID]
    cards: list[Account]


async def _cash_flow_scope(
    session: AsyncSession, workspace_id: uuid.UUID, account_ids: Optional[list[uuid.UUID]],
) -> _CashFlowScope:
    """The accounts holding money (everything but credit cards) and the
    cards whose bills they pay, within the optional account filter."""
    stmt = select(Account).where(Account.workspace_id == workspace_id, Account.is_closed == False)  # noqa: E712
    if account_ids is not None:
        stmt = stmt.where(Account.id.in_(account_ids))
    accounts = list((await session.execute(stmt)).scalars().all())
    return _CashFlowScope(
        cash_ids=[a.id for a in accounts if a.type != "credit_card"],
        cards=[a for a in accounts if a.type == "credit_card"],
    )


@dataclass
class _BillOutflow:
    account_id: uuid.UUID
    due_date: date
    amount: Decimal
    currency: str
    label: str


def _next_due(after: date, due_day: Optional[int]) -> date:
    """First date after ``after`` falling on the card's due day (clamped to
    the month's length); the first of next month without a due day."""
    if not due_day:
        return _add_months(after.replace(day=1), 1)
    for offset in range(0, 3):
        month = _add_months(after.replace(day=1), offset)
        last = calendar.monthrange(month.year, month.month)[1]
        candidate = month.replace(day=min(due_day, last))
        if candidate > after:
            return candidate
    return _add_months(after, 1)


async def _card_bill_outflows(
    session: AsyncSession, workspace_id: uuid.UUID, cards: list[Account], today: date, end: date,
    *, baseline: bool = False,
) -> list[_BillOutflow]:
    """What each card will take from the bank, on which due date, until ``end``.

    Pluggy cards come from the statements engine (RFC toledo-features §6.4):
    unpaid closed bills, the open one and the future ones, each at its
    expected total — posted rows, parcels still to come and expected
    recurring charges. A bill already overdue is due tomorrow.

    With ``baseline`` (the legacy historical-mean cash flow), a Pluggy bill
    still to close is at least the average of the last closed bills — the
    known parts only cover parcels and recurring charges, not the purchases
    still to come — and the months after the last statement get that
    average too.

    Other cards have no statements: their current debt is due on the next
    due day, and each expected recurring charge on the due day after it.
    """
    from app.services.card_statement_service import get_statements
    from app.services.dashboard_service import _balance_at, _get_recurring_projections

    out: list[_BillOutflow] = []
    tomorrow = today + timedelta(days=1)
    provider_of = {}
    connection_ids = {c.connection_id for c in cards if c.connection_id}
    if connection_ids:
        rows = await session.execute(
            select(BankConnection.id, BankConnection.provider).where(BankConnection.id.in_(connection_ids))
        )
        provider_of = dict(rows.all())
    for card in cards:
        label = card.display_name or card.name
        if provider_of.get(card.connection_id) == "pluggy":
            data = await get_statements(session, card, today=today)
            statements = data["statements"]
            recent = [
                Decimal(st["total"]) for st in statements
                if st["state"] in ("closed", "closed_estimated") and Decimal(st["total"]) > 0
            ][-3:]
            average = (sum(recent, Decimal(0)) / len(recent)).quantize(Decimal("0.01")) if baseline and recent else None
            last_due = None
            for st in statements:
                closed = st["state"] in ("closed", "closed_estimated")
                if closed and st["status"] not in ("due", "overdue"):
                    continue
                amount = Decimal(st["total"]) - (Decimal(st["paid"]) if closed else Decimal(0))
                if average is not None and not closed:
                    amount = max(amount, average)
                due = st["due_date"]
                if amount <= 0 or due is None:
                    continue
                last_due = max(last_due or due, due)
                due = max(due, tomorrow)
                if due <= end:
                    out.append(_BillOutflow(card.id, due, amount, card.currency, label))
            if average is not None and data.get("due_day"):
                due = _next_due(max(last_due or today, today), data["due_day"])
                while due <= end:
                    out.append(_BillOutflow(card.id, due, average, card.currency, label))
                    due = _next_due(due, data["due_day"])
            continue
        debt = -await _balance_at(session, workspace_id, today, account_ids=[card.id])
        user_currency = (await session.get(User, card.user_id)).primary_currency
        if debt > 0:
            due = _next_due(today, card.payment_due_day)
            if due <= end:
                out.append(_BillOutflow(card.id, due, Decimal(str(round(debt, 2))), user_currency, label))
        for proj in await _get_recurring_projections(
            session, workspace_id, tomorrow, end + timedelta(days=1), [card.id], statement_months=False,
        ):
            due = _next_due(proj["date"], card.payment_due_day)
            sign = Decimal(1) if proj["type"] == "debit" else Decimal(-1)
            if due <= end:
                out.append(_BillOutflow(card.id, due, Decimal(str(proj["amount"])) * sign, proj["currency"], label))
    return out


@dataclass
class _CashWalk:
    current_balance: float
    daily_balance: dict
    daily_inflow: dict
    daily_outflow: dict
    flows: dict
    cat_totals: dict
    lookback_days: int
    lowest_balance: float
    lowest_balance_date: date
    runway_date: Optional[date]


async def _cash_walk(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_ids: Optional[list[uuid.UUID]],
    primary_currency: str,
    today: date,
    chart_start: date,
    end: date,
    *,
    baseline: bool = False,
) -> _CashWalk:
    """The money in the accounts, day by day, from ``chart_start`` to ``end``:
    actual movements back to ``chart_start`` (anchored on today's balance) and
    the forecast forward — every recurrence and parcel still to come, or the
    legacy historical-mean ``baseline``."""
    from app.services.dashboard_service import _balance_at, _get_recurring_projections
    from app.services.fx_rate_service import get_rate

    # Cash only (RFC toledo-features follow-up): the balance is the money in
    # the bank accounts; a credit card reaches it when its bill is paid, on
    # the due date — never purchase by purchase.
    scope = await _cash_flow_scope(session, workspace_id, account_ids)
    cash_ids = scope.cash_ids

    # "Saldo Atual" shown in the hero card. The walk is anchored at this
    # value (not at balance-at-chart_start) so opening-balance transactions
    # inside the past-history window can't introduce drift.
    current_balance = await _balance_at(
        session, workspace_id, today, primary_currency_hint=primary_currency,
        account_ids=cash_ids,
    )

    rate_cache: dict[str, Decimal] = {primary_currency: Decimal("1")}

    async def _to_primary(amount: Decimal, ccy: str) -> float:
        if ccy == primary_currency:
            return float(amount)
        rate = rate_cache.get(ccy)
        if rate is None:
            rate = await get_rate(session, ccy, primary_currency)
            rate_cache[ccy] = rate
        return float((amount * rate).quantize(Decimal("0.01")))

    # Display flows (inflow/outflow bars) and the net change of the cash
    # balance per day, which the backward walk follows. Every row that moves
    # a bank balance counts — bill payments and transfers out included —
    # except that money moving between two cash accounts is not shown as an
    # inflow and an outflow (it still nets to zero in the walk).
    flows: dict[date, dict[str, float]] = {}
    net_past: dict[date, float] = {}

    def _add_flow(d: date, amount: float, is_credit: bool) -> None:
        bucket = flows.setdefault(d, {"inflow": 0.0, "outflow": 0.0})
        if is_credit:
            bucket["inflow"] += amount
        else:
            bucket["outflow"] += amount

    effective = case(
        (Transaction.currency == Account.currency, Transaction.amount),
        else_=func.coalesce(Transaction.amount_primary, Transaction.amount),
    )
    cash_rows = (
        await session.execute(
            select(
                Transaction.date, Transaction.type, effective, Account.currency,
                Transaction.source, Transaction.transfer_pair_id, Transaction.exclude_from_pnl,
            )
            .join(Account, Transaction.account_id == Account.id)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(
                Transaction.workspace_id == workspace_id,
                Transaction.account_id.in_(cash_ids),
                Transaction.date > chart_start,
                Transaction.date <= end,
                Transaction.status == "posted",
                Transaction.is_ignored.is_(False),
                or_(Transaction.category_id.is_(None), Category.is_ignored.is_(False)),
            )
        )
    ).all() if cash_ids else []
    pair_legs: dict[uuid.UUID, int] = defaultdict(int)
    for row in cash_rows:
        if row.transfer_pair_id is not None:
            pair_legs[row.transfer_pair_id] += 1
    for tx_date, tx_type, amt, ccy, source, pair_id, hidden in cash_rows:
        amount_primary = abs(await _to_primary(Decimal(str(amt or 0)), ccy))
        if amount_primary == 0:
            continue
        if source == "opening_balance":
            # The money was there before the history starts: it is part of
            # the balance the walk is anchored on, never a movement.
            continue
        if tx_date <= today:
            signed = amount_primary if tx_type == "credit" else -amount_primary
            net_past[tx_date] = net_past.get(tx_date, 0.0) + signed
        if pair_id is not None and pair_legs[pair_id] > 1:
            continue  # between two cash accounts: nets to zero
        if hidden:
            continue  # exclude_from_pnl: moves the balance, not shown as a flow
        _add_flow(tx_date, amount_primary, tx_type == "credit")

    # Pending bank rows are forecast rows: dated today or earlier they are
    # part of the projected starting position (unless the provider's number
    # may already include them), later they are a dated flow.
    pending_current_delta = 0.0
    pending_forecast = await _get_forecast_transactions(
        session, workspace_id, date.min, end + timedelta(days=1), cash_ids,
    )
    for tx in pending_forecast:
        if tx.status != "pending" or tx.is_ignored:
            continue
        flow_date = tx.date
        if flow_date > end:
            continue
        if tx.amount_primary is not None:
            amount_primary = abs(float(tx.amount_primary))
        else:
            amount_primary = await _to_primary(Decimal(str(abs(tx.amount))), tx.currency)
        if amount_primary:
            if flow_date <= today:
                if not (tx.account and tx.account.connection_id):
                    pending_current_delta += amount_primary if tx.type == "credit" else -amount_primary
            else:
                _add_flow(flow_date, amount_primary, tx.type == "credit")

    # 3. Forward projection. Default: deterministic recurring rules.
    #    Baseline mode: replace them with a historical-mean estimate so the
    #    chart reflects the user's actual recent lifestyle, not just what
    #    they've explicitly marked as recurring.
    cat_totals: dict[tuple[str, str], dict] = {}
    cat_cache: dict[str, dict] = {}
    baseline_lookback_days = 0

    if baseline:
        projections, baseline_lookback_days = await _get_baseline_projection(
            session, workspace_id, today, end, primary_currency, _to_primary, cash_ids,
            cash_view=True,
        )
    else:
        # From the start of the month: a late occurrence (expected earlier
        # this month, not seen yet) still counts until the user skips it.
        projections = await _get_recurring_projections(
            session, workspace_id, today.replace(day=1), end + timedelta(days=1),
            cash_ids,
        )

    for proj in projections:
        d = proj["date"]
        if d > end or (d <= today and not proj.get("computed")):
            continue
        d = max(d, today + timedelta(days=1))  # a late one is still to come
        amount_primary = await _to_primary(
            Decimal(str(proj["amount"])), proj["currency"]
        )
        if amount_primary == 0:
            continue
        _add_flow(d, amount_primary, proj["type"] == "credit")

        cat_id = proj["category_id"]
        if cat_id:
            cat_id_str = str(cat_id)
        else:
            cat_id_str = "baseline" if baseline else "uncategorized"
        group = "income" if proj["type"] == "credit" else "expenses"
        if cat_id and cat_id_str not in cat_cache:
            cat_row = await session.execute(
                select(Category.name, Category.color).where(Category.id == cat_id)
            )
            row = cat_row.one_or_none()
            cat_cache[cat_id_str] = {
                "label": row[0] if row else "Uncategorized",
                "color": row[1] if row else "#6B7280",
            }
        elif cat_id_str == "baseline" and cat_id_str not in cat_cache:
            # Frontend translates via reports.baseline; label is the fallback.
            cat_cache[cat_id_str] = {"label": "Baseline", "color": "#94A3B8"}
        info = cat_cache.get(cat_id_str, {"label": "Uncategorized", "color": "#6B7280"})
        key = (cat_id_str, group)
        if key not in cat_totals:
            cat_totals[key] = {"label": info["label"], "color": info["color"], "value": 0.0}
        cat_totals[key]["value"] += amount_primary

    # 3b. Invoices still owed, on the day they were promised for. An
    #     invoice is a claim rather than a movement, which is why it never
    #     reached this chart before: until matching could say that an open
    #     invoice and a pending bank credit were the same money, adding
    #     both would have inflated every projection in the product.
    #
    #     Only the unallocated balance is carried, so a claim and the
    #     payment that settles it never both appear. Skipped when the
    #     chart is narrowed to particular accounts: a claim has no account
    #     until somebody pays it.
    # `is None`, not falsy: a collection holding only wallets narrows
    # the report to an empty set of bank accounts, and an empty list
    # means filtered to nothing rather than not filtered at all.
    if account_ids is None:
        for claim in await invoice_forecast_service.claims_in_range(
            session, workspace_id, today + timedelta(days=1), end + timedelta(days=1)
        ):
            claim_primary = await _to_primary(claim.amount, claim.currency)
            if claim_primary == 0:
                continue
            _add_flow(claim.due_date, abs(claim_primary), claim.direction == "receivable")

    # 3c. Credit cards: each bill leaves the bank on its due date.
    for bill in await _card_bill_outflows(session, workspace_id, scope.cards, today, end, baseline=baseline):
        bill_primary = await _to_primary(bill.amount, bill.currency)
        if bill_primary <= 0:
            continue
        _add_flow(bill.due_date, bill_primary, False)
        key = (f"card:{bill.account_id}", "expenses")
        if key not in cat_totals:
            cat_totals[key] = {"label": bill.label, "color": "#F43F5E", "value": 0.0}
        cat_totals[key]["value"] += bill_primary

    # 4. Walk day-by-day. The actual section is anchored at today's
    # authoritative balance. The forward projected section starts from that
    # balance plus pending payables already dated today or earlier.
    #    balance (``current_balance`` from ``_balance_at``) rather than at
    #    ``chart_starting_balance``: opening-balance transactions are excluded
    #    from the past-actuals query, so a forward walk seeded from
    #    ``chart_starting_balance`` would drift if any opening balance falls
    #    inside the past-history window. Anchoring at today eliminates that
    #    class of bug and keeps the past trend visually correct.
    daily_balance: dict[date, float] = {today: current_balance}
    daily_inflow: dict[date, float] = {}
    daily_outflow: dict[date, float] = {}

    # Today's own inflow/outflow bucket (for tooltip), but the balance at
    # end-of-today is already current_balance regardless of today's flows
    # (they're folded into the authoritative number from _balance_at).
    today_bucket = flows.get(today, {"inflow": 0.0, "outflow": 0.0})
    daily_inflow[today] = today_bucket["inflow"]
    daily_outflow[today] = today_bucket["outflow"]

    # Backward walk: balance(d-1) = balance(d) - net_flow(d).
    running = current_balance
    cursor_d = today
    while cursor_d > chart_start:
        running -= net_past.get(cursor_d, 0.0)
        cursor_d = cursor_d - timedelta(days=1)
        daily_balance[cursor_d] = running
        prev_bucket = flows.get(cursor_d, {"inflow": 0.0, "outflow": 0.0})
        daily_inflow[cursor_d] = prev_bucket["inflow"]
        daily_outflow[cursor_d] = prev_bucket["outflow"]

    # Forward walk: balance(d+1) = balance(d) + net_flow(d+1). Pending rows
    # already due are part of the projected starting position, not a second
    # dated flow in the future.
    #
    # The walk also tracks the runway: the first day the projected balance
    # drops below zero, and the lowest point of the horizon. Both are taken
    # from the daily walk so a dip inside a week or month still counts when
    # the chart is aggregated to a coarser interval.
    running = current_balance + pending_current_delta
    lowest_balance, lowest_balance_date = running, today
    runway_date: date | None = today if running < 0 else None
    cursor_d = today
    while cursor_d < end:
        cursor_d = cursor_d + timedelta(days=1)
        bucket = flows.get(cursor_d, {"inflow": 0.0, "outflow": 0.0})
        running += bucket["inflow"] - bucket["outflow"]
        daily_balance[cursor_d] = running
        daily_inflow[cursor_d] = bucket["inflow"]
        daily_outflow[cursor_d] = bucket["outflow"]
        if running < lowest_balance:
            lowest_balance, lowest_balance_date = running, cursor_d
        if runway_date is None and running < 0:
            runway_date = cursor_d

    return _CashWalk(
        current_balance=current_balance,
        daily_balance=daily_balance,
        daily_inflow=daily_inflow,
        daily_outflow=daily_outflow,
        flows=flows,
        cat_totals=cat_totals,
        lookback_days=baseline_lookback_days,
        lowest_balance=lowest_balance,
        lowest_balance_date=lowest_balance_date,
        runway_date=runway_date,
    )


async def get_cash_flow_report(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    months: int = 6,
    interval: str = "daily",
    currency: str = "USD",
    baseline: bool = False,
    account_ids: Optional[list[uuid.UUID]] = None,
    reference_date: Optional[date] = None,
    past_months: Optional[int] = None,
    future_months: Optional[int] = None,
) -> ReportResponse:
    """Cash flow chart with a short past window plus a forward projection.

    With ``past_months``/``future_months`` the window runs from that many
    months before ``reference_date`` (default today) to that many after it,
    and the forecast counts every recurrence and parcel still to come
    (skipped occurrences aside), never a historical estimate.

    Past window (``_PAST_HISTORY_MONTHS`` months back to today) shows actual
    booked transactions so the user has a visible "this is real" anchor next
    to the forecast. Forward window walks from today to ``today + months``
    applying either deterministic recurring projections (default) or a
    historical-mean baseline (when ``baseline=True``) — see
    ``_get_baseline_projection`` for the latter.

    The balance is the money in the accounts — every account but credit
    cards. A card reaches it only through its bill, on the due date (see
    ``_card_bill_outflows``), so a purchase is never counted twice and the
    global ``credit_card_accounting_mode`` no longer applies here.
    """
    today = app_today()
    windowed = past_months is not None or future_months is not None
    if windowed:
        start, end = _window(reference_date or today, past_months or 0, future_months or 0, interval)
    else:
        start = _add_months(today, -_PAST_HISTORY_MONTHS)
        end = _add_months(today, months)
    chart_start = min(start, today)
    walk_end = max(end, today)

    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency

    known = await _cash_walk(
        session, workspace_id, account_ids, primary_currency, today, chart_start, walk_end,
        baseline=baseline and not windowed,
    )
    current_balance = known.current_balance
    daily_balance, daily_inflow, daily_outflow = known.daily_balance, known.daily_inflow, known.daily_outflow
    flows, cat_totals = known.flows, known.cat_totals
    baseline_lookback_days = known.lookback_days
    lowest_balance, lowest_balance_date, runway_date = (
        known.lowest_balance, known.lowest_balance_date, known.runway_date,
    )
    running = daily_balance.get(walk_end, current_balance)
    chart_start = start

    # 5. Aggregate to interval.
    points = _date_points(chart_start, end, interval)
    trend: list[ReportDataPoint] = []

    if interval == "daily":
        for p in points:
            bal = daily_balance.get(p, running)
            inf = daily_inflow.get(p, 0.0)
            outf = daily_outflow.get(p, 0.0)
            breakdowns = {"inflow": round(inf, 2), "outflow": round(outf, 2)}
            trend.append(ReportDataPoint(
                date=_format_date_label(p, "daily"),
                value=round(bal, 2),
                breakdowns=breakdowns,
            ))
    else:
        groups: dict[str, dict] = {}
        cur = chart_start
        while cur <= end:
            label = _format_date_label(cur, interval)
            g = groups.setdefault(
                label,
                {"balance": daily_balance.get(cur, running), "last_d": cur,
                 "inflow": 0.0, "outflow": 0.0},
            )
            if cur >= g["last_d"]:
                g["balance"] = daily_balance.get(cur, running)
                g["last_d"] = cur
            g["inflow"] += daily_inflow.get(cur, 0.0)
            g["outflow"] += daily_outflow.get(cur, 0.0)
            cur = cur + timedelta(days=1)

        seen: set[str] = set()
        for p in points:
            label = _format_date_label(p, interval)
            if label in seen:
                continue
            seen.add(label)
            g = groups.get(label)
            if g is None:
                continue
            breakdowns = {
                "inflow": round(g["inflow"], 2),
                "outflow": round(g["outflow"], 2),
            }
            trend.append(ReportDataPoint(
                date=label,
                value=round(g["balance"], 2),
                breakdowns=breakdowns,
            ))

    # 6. Summary. "Projected income/expenses" sums only the forward portion,
    #    not the past actuals that the chart now also includes.
    ending_balance = trend[-1].value if trend else round(current_balance, 2)
    forward_inflow = 0.0
    forward_outflow = 0.0
    for d, bucket in flows.items():
        if today < d <= end:
            forward_inflow += bucket["inflow"]
            forward_outflow += bucket["outflow"]
    total_inflow = round(forward_inflow, 2)
    total_outflow = round(forward_outflow, 2)
    change_amount = round(ending_balance - current_balance, 2)
    change_percent = (
        (change_amount / abs(current_balance) * 100) if current_balance != 0 else None
    )

    summary = ReportSummary(
        primary_value=ending_balance,
        change_amount=change_amount,
        change_percent=round(change_percent, 2) if change_percent is not None else None,
        breakdowns=[
            ReportBreakdown(
                key="startingBalance", label="Starting Balance",
                value=round(current_balance, 2), color="#6366F1",
            ),
            ReportBreakdown(
                key="projectedIncome", label="Projected Income",
                value=total_inflow, color="#10B981",
            ),
            ReportBreakdown(
                key="projectedExpenses", label="Projected Expenses",
                value=total_outflow, color="#F43F5E",
            ),
            ReportBreakdown(
                key="endingBalance", label="Ending Balance",
                value=ending_balance, color="#8B5CF6",
            ),
        ],
    )

    meta = ReportMeta(
        type="cash_flow",
        reference_date=(reference_date or today).isoformat() if windowed else None,
        window_start=start.isoformat() if windowed else None,
        window_end=end.isoformat() if windowed else None,
        series_keys=["balance"],
        currency=primary_currency,
        interval=interval,
        forecast_start_date=_format_date_label(today, interval),
        baseline_active=baseline,
        baseline_lookback_days=baseline_lookback_days if baseline else None,
        runway_date=runway_date.isoformat() if runway_date else None,
        lowest_balance=round(lowest_balance, 2),
        lowest_balance_date=lowest_balance_date.isoformat(),
    )

    composition: list[ReportCompositionItem] = []
    for (cat_key, group), info in cat_totals.items():
        if info["value"] <= 0:
            continue
        composition.append(ReportCompositionItem(
            key=cat_key,
            label=info["label"],
            value=round(info["value"], 2),
            color=info["color"],
            group=group,
        ))

    return ReportResponse(
        summary=summary, trend=trend, meta=meta,
        composition=composition, category_trend=[],
    )
