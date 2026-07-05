#!/usr/bin/env python3
"""
KR holdings reconciliation (Theme A-3): compare the tracking DB (stock_holdings)
against the live KIS account, per account, and alert on divergence.

Read-only + alert. It NEVER modifies positions or the DB — a mismatch is an
operator signal, not something to auto-"fix" (auto-trading off a reconciliation
mismatch is unsafe). It exists because the DB is written around the broker order
(order-before-record still has a residual window: an INSERT that fails after a
fill, a multi-account partial fill, or a manual trade at the broker), so the two
ledgers can drift and nothing else notices.

Divergence types:
  - db_only  : in stock_holdings but NOT at the broker → a phantom buy (order
    rejected/unfilled yet recorded — legacy rows) or a sell that filled at the
    broker while the DB row was not removed.
  - kis_only : at the broker but NOT in stock_holdings → an unrecorded fill (DB
    write failed after a filled order), a partial fill on a sub-account, or a
    manual trade.

Exit code: 0 = reconciled, 1 = divergence found (Telegram alert attempted).

Run:  python -m tools.reconcile_kr_holdings
Schedule it after the trading windows (e.g. a few minutes past close).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("reconcile_kr")

DB_PATH = os.getenv("PRISM_TRACKING_DB") or str(PROJECT_ROOT / "stock_tracking_db.sqlite")
# Alerts reuse the main bot unless a dedicated ops bot/chat is configured.
BOT_TOKEN = os.getenv("RECONCILE_ALERT_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("RECONCILE_ALERT_CHAT_ID") or os.getenv("TELEGRAM_CHANNEL_ID")


def _load_mode() -> str:
    """Trading mode (demo/real), mirroring the dashboard's kis_devlp.yaml lookup."""
    try:
        import yaml
        with open(PROJECT_ROOT / "trading" / "config" / "kis_devlp.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(cfg.get("default_mode", "demo")).strip().lower()
    except Exception:
        return os.getenv("PRISM_DEFAULT_MODE", "demo").strip().lower()


def _db_tickers_by_account(conn: sqlite3.Connection) -> dict:
    """{account_key: {ticker: company_name}} from stock_holdings."""
    out: dict = {}
    for account_key, ticker, name in conn.execute(
        "SELECT account_key, ticker, company_name FROM stock_holdings"
    ):
        out.setdefault(account_key, {})[ticker] = name
    return out


def _send_alert(text: str) -> bool:
    if not BOT_TOKEN or not ALERT_CHAT_ID:
        logger.warning("[reconcile] cannot alert: bot token or chat id missing")
        return False
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": ALERT_CHAT_ID, "text": text},
            timeout=10,
        )
        return bool(resp.ok)
    except Exception as e:
        logger.error(f"[reconcile] alert send failed: {e}")
        return False


def reconcile() -> int:
    from trading.domestic_stock_trading import DomesticStockTrading, MultiAccountDomesticStockTrading

    mode = _load_mode()
    logger.info(f"[reconcile] mode={mode} db={DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        db_by_acct = _db_tickers_by_account(conn)
    finally:
        conn.close()

    mta = MultiAccountDomesticStockTrading(mode=mode)
    if not mta.account_configs:
        logger.warning("[reconcile] no KR accounts configured — nothing to reconcile")
        return 0

    problems: list[str] = []
    for account in mta.account_configs:
        acct_key = account["account_key"]
        name = account["name"]
        db_tickers = db_by_acct.get(acct_key, {})
        try:
            trader = DomesticStockTrading(
                mode=mode, account_name=name, product_code=account["product"]
            )
            kis_tickers = {h.get("stock_code") for h in trader.get_portfolio() if h.get("stock_code")}
        except Exception as e:
            # A fetch error must NOT be reported as "everything is a phantom".
            logger.error(f"[reconcile] {name}({acct_key}) portfolio fetch failed — skipping account: {e}")
            continue

        # get_portfolio() returns [] for both a genuinely flat account AND a
        # swallowed inquiry error, so an empty broker side with DB rows is
        # ambiguous — flag it for a human rather than declaring N phantoms.
        if db_tickers and not kis_tickers:
            problems.append(
                f"⚠️ [{name}] KIS 보유 0 / DB 보유 {len(db_tickers)} — 전량매도 또는 조회오류 확인 필요: {sorted(db_tickers)}"
            )
            continue

        for t in sorted(set(db_tickers) - kis_tickers):
            problems.append(f"👻 [{name}] DB에만: {t}({db_tickers[t]}) — 미체결/거부 매수 또는 매도 미반영 의심")
        for t in sorted(kis_tickers - set(db_tickers)):
            problems.append(f"🩸 [{name}] 브로커에만: {t} — 미기록 체결/부분체결/수동거래 의심")

    if problems:
        body = f"🔧 [RECONCILE][KR] 보유 불일치 {len(problems)}건 (mode={mode})\n" + "\n".join(problems[:30])
        logger.warning(body)
        _send_alert(body)
        return 1

    logger.info("[reconcile] OK — DB and broker holdings match for all accounts")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(reconcile())
