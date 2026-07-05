"""Daily-loss / drawdown BUY kill-switch (portfolio-level, SHADOW-first).

A capital-preservation circuit breaker: when the account equity curve is in a
meaningful drawdown from its peak, or just took a large day-over-day drop, stop
opening NEW positions. It ONLY vetoes fresh entries — it never sells, never
force-liquidates, and never touches sizing/stops of existing holdings (those are
managed by the sell tiers / stop-loss). Reducing exposure on a bleeding account
is a sell decision; this is purely "don't add more risk right now".

SHADOW by default: it logs the would-block and returns a verdict, but the caller
enforces only under DAILY_LOSS_KILL_LIVE. Fail-open: any error, disabled state,
or insufficient history returns None (= allow the buy), so a bug here can never
block a legitimate entry — at worst it falls back to the old (no-gate) behavior.

Self-contained (stdlib + sqlite only, no project imports) so it is import-safe
under both the root and the prism-us cores-shadowed runtimes, mirroring
reentry_cooldown.py.

Data source: account_equity_snapshot — one POST-SETTLEMENT equity point per
account per day (written by the dashboard generator via tracking.equity_tracker;
fees/tax already netted). Consequences of the daily cadence, by design:
  - "drawdown" = highest recorded equity → latest recorded equity (peak-to-now).
  - "daily drop" = the last COMPLETED day-over-day move (latest vs previous
    snapshot). At a 09:30 buy run the latest snapshot is the prior session's EOD,
    so this reads as "did the last closed session hurt us", not intraday-today.
  - Forward-only from the first snapshot; external deposits/withdrawals are NOT
    adjusted for (a deposit looks like a gain) — same caveats as equity_tracker.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Master switch (set false to no-op entirely) and the SHADOW→LIVE enforce flag.
ENABLED = _env_flag("DAILY_LOSS_KILL_ENABLED", True)
LIVE = _env_flag("DAILY_LOSS_KILL_LIVE", False)  # False => SHADOW (log only)

# Thresholds as POSITIVE percents; a breach AT/ABOVE the value blocks new buys.
# 0 (or negative) disables that individual gate.
DRAWDOWN_PCT = _env_float("DAILY_LOSS_KILL_DRAWDOWN_PCT", 10.0)  # peak → latest
DAILY_DROP_PCT = _env_float("DAILY_LOSS_KILL_DAILY_PCT", 5.0)    # prev day → latest


def _db_path(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    env = os.getenv("PRISM_TRACKING_DB")
    if env:
        return env
    return str(Path(__file__).resolve().parent / "stock_tracking_db.sqlite")


def _equity_series(path: str, account_key: str) -> list:
    """[(snapshot_date, total_eval_amount)] ascending. Fail-open: [] on any error."""
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            rows = conn.execute(
                "SELECT snapshot_date, total_eval_amount FROM account_equity_snapshot "
                "WHERE account_key = ? AND total_eval_amount IS NOT NULL "
                "ORDER BY snapshot_date ASC, id ASC",
                (account_key,),
            ).fetchall()
            return [(r[0], float(r[1])) for r in rows if r[1] is not None]
        except sqlite3.OperationalError:
            return []  # table absent (pre-first-snapshot) -> allow
        finally:
            conn.close()
    except Exception:
        return []  # fail-open


def buy_block(account_key: str, db_path: Optional[str] = None) -> Optional[dict]:
    """If the account is in a drawdown/daily-loss state that should halt NEW buys,
    return a verdict dict; else None (allow).

    Verdict: {action, reason, drawdown_pct, daily_drop_pct, peak, latest,
              dd_threshold, daily_threshold}. Fail-open: None on disabled / no
              account / <2 snapshots / any error.
    """
    if not ENABLED or not account_key:
        return None
    series = _equity_series(_db_path(db_path), account_key)
    if len(series) < 2:
        return None  # not enough history to judge -> allow

    values = [v for _, v in series]
    peak = max(values)
    latest = values[-1]
    prev = values[-2]
    if peak <= 0:
        return None

    drawdown = (peak - latest) / peak * 100.0
    daily_drop = (prev - latest) / prev * 100.0 if prev > 0 else 0.0

    reasons = []
    if DRAWDOWN_PCT > 0 and drawdown >= DRAWDOWN_PCT:
        reasons.append(f"drawdown {drawdown:.1f}%>={DRAWDOWN_PCT:.1f}%")
    if DAILY_DROP_PCT > 0 and daily_drop >= DAILY_DROP_PCT:
        reasons.append(f"daily_drop {daily_drop:.1f}%>={DAILY_DROP_PCT:.1f}%")
    if not reasons:
        return None

    return {
        "action": "WOULD_BLOCK",
        "reason": " & ".join(reasons),
        "drawdown_pct": round(drawdown, 2),
        "daily_drop_pct": round(daily_drop, 2),
        "peak": round(peak, 2),
        "latest": round(latest, 2),
        "dd_threshold": DRAWDOWN_PCT,
        "daily_threshold": DAILY_DROP_PCT,
    }
