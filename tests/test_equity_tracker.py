"""Unit tests for tracking.equity_tracker (honest account return & MDD).

Pure metric helpers are tested in isolation; the record/compute path is
exercised against an in-memory SQLite DB (no KIS, no real DB file).
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tracking.equity_tracker import (
    total_return_pct,
    max_drawdown_pct,
    current_drawdown_pct,
    record_equity_snapshot,
    compute_equity_metrics,
)


# --- pure helpers ---------------------------------------------------------

def test_total_return_pct_basic():
    assert total_return_pct(100.0, 110.0) == 10.0
    assert total_return_pct(100.0, 80.0) == -20.0


def test_total_return_pct_guards_nonpositive_start():
    assert total_return_pct(0.0, 50.0) == 0.0
    assert total_return_pct(-10.0, 50.0) == 0.0
    assert total_return_pct(None, 50.0) == 0.0


def test_max_drawdown_pct():
    # peak 120 then trough 90 -> (120-90)/120 = 25%
    assert max_drawdown_pct([100, 120, 90, 110]) == 25.0
    # monotonically rising -> no drawdown
    assert max_drawdown_pct([100, 110, 120]) == 0.0
    # fewer than 2 points -> 0
    assert max_drawdown_pct([100]) == 0.0
    assert max_drawdown_pct([]) == 0.0


def test_current_drawdown_pct():
    # latest 90 vs peak 120 -> 25%
    assert current_drawdown_pct([100, 120, 90]) == 25.0
    # at peak -> 0
    assert current_drawdown_pct([100, 120, 120]) == 0.0
    assert current_drawdown_pct([]) == 0.0


# --- DB round-trip --------------------------------------------------------

def _summary(total_eval, cash=None, deposit=None, pnl=None):
    return {
        "total_eval_amount": total_eval,
        "total_cash": cash,
        "deposit": deposit,
        "total_profit_amount": pnl,
    }


def test_record_and_compute_roundtrip():
    conn = sqlite3.connect(":memory:")
    acct = "12345678-01"
    evals = [1000.0, 1100.0, 900.0, 1050.0]
    for i, ev in enumerate(evals):
        ok = record_equity_snapshot(
            conn, acct, "real", _summary(ev, cash=ev * 0.5),
            ts=f"2026-07-0{i + 1}T09:00:00", source="dashboard",
        )
        assert ok is True

    m = compute_equity_metrics(conn, acct)
    assert m["n_snapshots"] == 4
    assert m["start_equity"] == 1000.0
    assert m["latest_equity"] == 1050.0
    assert m["peak_equity"] == 1100.0
    assert m["return_pct"] == 5.0                    # (1050-1000)/1000
    assert m["mdd_pct"] == round((1100 - 900) / 1100 * 100, 2)   # 18.18
    assert m["current_drawdown_pct"] == round((1100 - 1050) / 1100 * 100, 2)  # 4.55
    conn.close()


def test_record_skips_empty_or_nonpositive_summary():
    conn = sqlite3.connect(":memory:")
    acct = "acct"
    assert record_equity_snapshot(conn, acct, "real", {}) is False
    assert record_equity_snapshot(conn, acct, "real", _summary(0.0)) is False
    assert record_equity_snapshot(conn, acct, "real", _summary(-5.0)) is False
    # nothing recorded -> empty metrics
    m = compute_equity_metrics(conn, acct)
    assert m["n_snapshots"] == 0
    assert m["return_pct"] == 0.0
    assert m["mdd_pct"] == 0.0
    conn.close()


def test_metrics_isolated_per_account():
    conn = sqlite3.connect(":memory:")
    record_equity_snapshot(conn, "A", "real", _summary(1000.0), ts="2026-07-01T09:00:00")
    record_equity_snapshot(conn, "A", "real", _summary(1200.0), ts="2026-07-02T09:00:00")
    record_equity_snapshot(conn, "B", "real", _summary(1000.0), ts="2026-07-01T09:00:00")
    record_equity_snapshot(conn, "B", "real", _summary(800.0), ts="2026-07-02T09:00:00")

    a = compute_equity_metrics(conn, "A")
    b = compute_equity_metrics(conn, "B")
    assert a["return_pct"] == 20.0
    assert b["return_pct"] == -20.0
    assert b["mdd_pct"] == 20.0
    conn.close()


def test_same_day_run_upserts_keeping_latest():
    # Cron runs twice/weekday (11:05 & 17:10 KST). Both land on the same
    # calendar day and must collapse to ONE point (last=EOD wins), else MDD
    # sees intraday noise as extra trading days.
    conn = sqlite3.connect(":memory:")
    acct = "prod:12345678:01"
    assert record_equity_snapshot(conn, acct, "primary", _summary(1000.0),
                                  ts="2026-07-05T11:05:00") is True
    assert record_equity_snapshot(conn, acct, "primary", _summary(1020.0),
                                  ts="2026-07-05T17:10:00") is True
    m = compute_equity_metrics(conn, acct)
    assert m["n_snapshots"] == 1
    assert m["latest_equity"] == 1020.0
    conn.close()


def test_out_of_order_insertion_is_ordered_by_ts():
    # Distinct days inserted latest-first; compute must sort chronologically.
    conn = sqlite3.connect(":memory:")
    acct = "prod:12345678:01"
    record_equity_snapshot(conn, acct, "primary", _summary(1100.0), ts="2026-07-03T09:00:00")
    record_equity_snapshot(conn, acct, "primary", _summary(1000.0), ts="2026-07-01T09:00:00")
    m = compute_equity_metrics(conn, acct)
    assert m["n_snapshots"] == 2
    assert m["start_equity"] == 1000.0    # 07-01 is chronological start
    assert m["latest_equity"] == 1100.0   # 07-03 is chronological latest
    assert m["return_pct"] == 10.0
    conn.close()
