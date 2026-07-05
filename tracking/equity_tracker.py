"""
Real-account equity tracking — honest portfolio return & MDD.

The dashboard's headline "cumulative return" is the arithmetic sum of per-trade
percentages, which is not an account return. This module records the real KIS
account equity (``get_account_summary()['total_eval_amount']`` — the net amount
after KIS settles fees and transaction tax) as a time series, and derives a
portfolio-level return and maximum drawdown from that curve.

Because the equity value is already post-settlement, fees/tax/slippage are
reflected automatically — no per-trade cost itemization is needed here.

Caveats (by design):
  - Start capital = the first recorded snapshot. Historical equity was never
    stored, so metrics are accurate FORWARD from when recording begins; the past
    cannot be reconstructed.
  - External deposits/withdrawals are NOT adjusted for. A deposit inflates the
    return as if it were a gain. Handling that needs a time-weighted return over
    the cashflow log (out of scope here).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from tracking.db_schema import TABLE_ACCOUNT_EQUITY_SNAPSHOT


# ---------------------------------------------------------------------------
# Pure metric helpers (no I/O — unit-testable in isolation)
# ---------------------------------------------------------------------------

def total_return_pct(start_equity: float, latest_equity: float) -> float:
    """Simple return of the equity curve: (latest - start) / start * 100.

    Returns 0.0 when start_equity is missing or non-positive.
    """
    if start_equity is None or start_equity <= 0:
        return 0.0
    return round((latest_equity - start_equity) / start_equity * 100.0, 2)


def max_drawdown_pct(values: list[float]) -> float:
    """Maximum peak-to-trough drop of an equity series, as a POSITIVE percent.

    e.g. 12.34 means the worst decline from a running peak was -12.34%.
    Returns 0.0 for series shorter than 2 points.
    """
    if len(values) < 2:
        return 0.0
    peak = values[0]
    mdd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > mdd:
                mdd = dd
    return round(mdd, 2)


def current_drawdown_pct(values: list[float]) -> float:
    """Drawdown of the latest point from the highest peak reached, POSITIVE percent."""
    if not values:
        return 0.0
    peak = max(values)
    if peak <= 0:
        return 0.0
    return round((peak - values[-1]) / peak * 100.0, 2)


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the snapshot table + index if absent (idempotent).

    The dashboard generator connects to the DB without running the agent's
    create_all_tables()/create_indexes(), so record/compute defend both here.
    """
    conn.execute(TABLE_ACCOUNT_EQUITY_SNAPSHOT)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_equity_snapshot_account "
        "ON account_equity_snapshot(account_key, ts)"
    )


def record_equity_snapshot(
    conn: sqlite3.Connection,
    account_key: str,
    account_name: Optional[str],
    summary: dict,
    ts: Optional[str] = None,
    source: str = "dashboard",
) -> bool:
    """Append one equity point from a ``get_account_summary()`` dict.

    Skips (returns False) when the summary is empty or reports a non-positive
    total evaluation — an empty/failed KIS inquiry must not pollute the series.
    """
    if not summary:
        return False
    total_eval = summary.get("total_eval_amount")
    if total_eval is None or total_eval <= 0:
        return False

    total_cash = summary.get("total_cash")
    securities_value = (
        total_eval - total_cash
        if total_cash is not None
        else None
    )
    ts = ts or datetime.now().isoformat()
    snapshot_date = ts[:10]  # local calendar day — one point per account per day

    _ensure_table(conn)
    # Upsert: a second run on the same day overwrites (last write ~ EOD wins),
    # so MDD is computed on one point per trading day, not intraday noise.
    conn.execute(
        "INSERT INTO account_equity_snapshot "
        "(account_key, account_name, ts, snapshot_date, total_eval_amount, "
        " securities_value, total_cash, deposit, unrealized_pnl, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_key, snapshot_date) DO UPDATE SET "
        "ts=excluded.ts, account_name=excluded.account_name, "
        "total_eval_amount=excluded.total_eval_amount, "
        "securities_value=excluded.securities_value, total_cash=excluded.total_cash, "
        "deposit=excluded.deposit, unrealized_pnl=excluded.unrealized_pnl, "
        "source=excluded.source",
        (
            account_key,
            account_name,
            ts,
            snapshot_date,
            float(total_eval),
            securities_value,
            total_cash,
            summary.get("deposit"),
            summary.get("total_profit_amount"),
            source,
        ),
    )
    conn.commit()
    return True


def compute_equity_metrics(conn: sqlite3.Connection, account_key: str) -> dict:
    """Return honest portfolio metrics from the recorded equity series.

    Return-shape keys: n_snapshots, start_ts, start_equity, latest_ts,
    latest_equity, peak_equity, return_pct, mdd_pct, current_drawdown_pct, note.
    """
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT ts, total_eval_amount FROM account_equity_snapshot "
        "WHERE account_key = ? ORDER BY ts ASC, id ASC",
        (account_key,),
    ).fetchall()

    series = [(r[0], float(r[1])) for r in rows if r[1] is not None]
    if not series:
        return {
            "n_snapshots": 0,
            "start_ts": None, "start_equity": None,
            "latest_ts": None, "latest_equity": None,
            "peak_equity": None,
            "return_pct": 0.0, "mdd_pct": 0.0, "current_drawdown_pct": 0.0,
            "note": "no snapshots yet — recording starts on the next dashboard run",
        }

    values = [v for _, v in series]
    start_ts, start_equity = series[0]
    latest_ts, latest_equity = series[-1]
    return {
        "n_snapshots": len(series),
        "start_ts": start_ts, "start_equity": round(start_equity, 2),
        "latest_ts": latest_ts, "latest_equity": round(latest_equity, 2),
        "peak_equity": round(max(values), 2),
        "return_pct": total_return_pct(start_equity, latest_equity),
        "mdd_pct": max_drawdown_pct(values),
        "current_drawdown_pct": current_drawdown_pct(values),
        "note": "forward-only from first snapshot; excludes external deposits/withdrawals",
    }
