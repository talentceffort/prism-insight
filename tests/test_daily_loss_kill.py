"""daily_loss_kill (drawdown/daily-loss BUY kill-switch) unit tests.

Self-contained (stdlib + sqlite), runs in the root pytest session with no heavy
deps. Mirrors the SHADOW-first / fail-open contract of reentry_cooldown.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import daily_loss_kill as dlk


def _make_db(tmp_path, account_key, equities, name="t.sqlite"):
    """Temp tracking DB with account_equity_snapshot rows.
    equities: list of (snapshot_date, total_eval_amount)."""
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE account_equity_snapshot ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, account_key TEXT, "
        "snapshot_date TEXT, total_eval_amount REAL)"
    )
    conn.executemany(
        "INSERT INTO account_equity_snapshot (account_key, snapshot_date, total_eval_amount) "
        "VALUES (?,?,?)",
        [(account_key, d, v) for d, v in equities],
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture(autouse=True)
def _known_config(monkeypatch):
    # Deterministic enabled state + thresholds regardless of the runner's env.
    monkeypatch.setattr(dlk, "ENABLED", True)
    monkeypatch.setattr(dlk, "DRAWDOWN_PCT", 10.0)
    monkeypatch.setattr(dlk, "DAILY_DROP_PCT", 5.0)


def test_none_when_insufficient_history(tmp_path):
    assert dlk.buy_block("A", _make_db(tmp_path, "A", [], "a.sqlite")) is None
    assert dlk.buy_block("A", _make_db(tmp_path, "A", [("2026-07-01", 1000.0)], "b.sqlite")) is None


def test_none_when_flat_or_up(tmp_path):
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 1010.0)])
    assert dlk.buy_block("A", db) is None


def test_drawdown_breach_blocks(tmp_path):
    # peak 1000 -> latest 880 = 12% drawdown >= 10%
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 950.0), ("2026-07-03", 880.0)])
    v = dlk.buy_block("A", db)
    assert v and v["action"] == "WOULD_BLOCK"
    assert v["drawdown_pct"] == 12.0
    assert "drawdown" in v["reason"]


def test_below_both_thresholds_allows(tmp_path):
    # dd 3% (<10), daily 3% (<5)
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 970.0)])
    assert dlk.buy_block("A", db) is None


def test_daily_drop_breach_blocks_without_drawdown(tmp_path):
    # peak 1000, latest 940 -> dd 6% (<10), but last-day drop 6% (>=5)
    db = _make_db(tmp_path, "A", [("2026-07-01", 900.0), ("2026-07-02", 1000.0), ("2026-07-03", 940.0)])
    v = dlk.buy_block("A", db)
    assert v and "daily_drop" in v["reason"]
    assert v["daily_drop_pct"] == 6.0
    assert "drawdown" not in v["reason"]  # 6% < 10% dd threshold


def test_disabled_master_switch_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(dlk, "ENABLED", False)
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 800.0)])
    assert dlk.buy_block("A", db) is None


def test_zero_threshold_disables_that_gate(tmp_path, monkeypatch):
    # Disable drawdown gate (0), keep daily. A pure-drawdown breach must not fire.
    monkeypatch.setattr(dlk, "DRAWDOWN_PCT", 0.0)
    # 1000 -> 999 -> 850: dd 15% but last-day drop ~14.9% would fire daily; craft daily<5.
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 860.0), ("2026-07-03", 850.0)])
    # dd = (1000-850)/1000 = 15% (gate off), daily = (860-850)/860 = 1.16% (<5) -> allow
    assert dlk.buy_block("A", db) is None


def test_missing_table_fail_open(tmp_path):
    db = tmp_path / "empty.sqlite"
    sqlite3.connect(str(db)).close()  # no table at all
    assert dlk.buy_block("A", str(db)) is None


def test_empty_account_key_allows(tmp_path):
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 800.0)])
    assert dlk.buy_block("", db) is None


def test_account_scoped(tmp_path):
    # Account B is down hard; account A is flat. Blocking B must not affect A.
    db = _make_db(
        tmp_path, "A",
        [("2026-07-01", 1000.0), ("2026-07-02", 1000.0)],
    )
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO account_equity_snapshot (account_key, snapshot_date, total_eval_amount) VALUES (?,?,?)",
        [("B", "2026-07-01", 1000.0), ("B", "2026-07-02", 700.0)],
    )
    conn.commit()
    conn.close()
    assert dlk.buy_block("A", db) is None
    assert dlk.buy_block("B", db) is not None


def test_verdict_fields_present(tmp_path):
    db = _make_db(tmp_path, "A", [("2026-07-01", 1000.0), ("2026-07-02", 800.0)])
    v = dlk.buy_block("A", db)
    assert {"action", "reason", "drawdown_pct", "daily_drop_pct",
            "peak", "latest", "dd_threshold", "daily_threshold"}.issubset(v)
    assert v["peak"] == 1000.0 and v["latest"] == 800.0
