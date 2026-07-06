"""눌림목(pullback) strategy backtest — v1. See docs/DESIGN_pullback_strategy.md.

Public data via FinanceDataReader (no KRX login; KRX locked down public pykrx).
Offline after the OHLCV cache is warm. Reuses sim_broker.py for realistic fills
(tick slippage + commission + sell tax). No-lookahead: screening/support use the
PRIOR day's data; entries fill on the current day.

v1 rules (starting values — sweep to tune):
  universe   = current top-N by 거래대금 (Amount), fetched once (survivorship caveat)
  daily set  = per-day top DAILY_TOP by 거래대금 (Close*Volume proxy)
  screen     = Close_{d-1} > MA20_{d-1}  AND  5d return_{d-1} > UPTREND_5D  AND not pumped
  support    = MA(SUPPORT_MA)_{d-1}   entry if Low_d <= support  (fill at support)
  stop       = entry * (1 - STOP_PCT/100)   [checked before target same day]
  target     = entry * (1 + TARGET_PCT/100)
  time exit  = close after HOLD_DAYS bars

Run:  .venv-bt/bin/python backtesting/pullback_backtest.py --start 2025-01-01 --end 2026-07-03
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import FinanceDataReader as fdr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sim_broker as sb

CACHE = Path(__file__).resolve().parent / "cache"
CACHE.mkdir(exist_ok=True)

DEFAULTS = dict(
    universe_top=150,   # fetch universe = current top-N by 거래대금
    daily_top=100,      # per-day 거래대금 top eligible for candidacy
    trend_ma=20,        # uptrend filter MA
    support_ma=5,       # pullback support MA
    uptrend_5d=0.0,     # 5-day return threshold (fraction, e.g. 0.03)
    pump_pct=15.0,      # exclude if prior-day 1d change > this %
    stop_pct=2.0,       # stop below entry (%)
    target_pct=5.0,     # take-profit (%)
    hold_days=3,        # time exit (bars)
)


def get_universe(top_n: int) -> list[str]:
    """Current top-N KR tickers by 거래대금 (Amount) from KOSPI+KOSDAQ listings."""
    frames = []
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            frames.append(fdr.StockListing(mkt))
        except Exception as e:
            print(f"[warn] StockListing({mkt}) failed: {e}")
    lst = pd.concat(frames, ignore_index=True)
    lst = lst[lst["Amount"].notna()].sort_values("Amount", ascending=False)
    return lst["Code"].astype(str).str.zfill(6).head(top_n).tolist()


def get_prices(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per ticker via fdr, cached to parquet. Offline once warm."""
    out = {}
    for i, code in enumerate(codes, 1):
        f = CACHE / f"{code}_{start}_{end}.pkl"
        if f.exists():
            out[code] = pd.read_pickle(f)
            continue
        try:
            df = fdr.DataReader(code, start, end)
            if len(df):
                df.to_pickle(f)
                out[code] = df
        except Exception as e:
            print(f"[warn] {code} fetch failed: {e}")
        if i % 25 == 0:
            print(f"  fetched {i}/{len(codes)}")
    return out


def backtest(prices: dict[str, pd.DataFrame], p: dict) -> dict:
    # Align close/high/low/volume into date x ticker matrices.
    close = pd.DataFrame({c: d["Close"] for c, d in prices.items()}).sort_index()
    high  = pd.DataFrame({c: d["High"]  for c, d in prices.items()}).reindex(close.index)
    low   = pd.DataFrame({c: d["Low"]   for c, d in prices.items()}).reindex(close.index)
    vol   = pd.DataFrame({c: d["Volume"] for c, d in prices.items()}).reindex(close.index)

    ma_t = close.rolling(p["trend_ma"]).mean()
    ma_s = close.rolling(p["support_ma"]).mean()
    ret5 = close.pct_change(5)
    chg1 = close.pct_change(1) * 100.0
    value = close * vol                       # 거래대금 proxy
    # per-day 거래대금 rank (1 = highest); eligible if rank <= daily_top
    rank = value.rank(axis=1, ascending=False)

    dates = close.index
    trades = []
    start_i = max(p["trend_ma"], p["support_ma"], 6) + 1
    open_by = {}  # ticker -> dict(entry, stop, target, entry_i)

    for i in range(start_i, len(dates)):
        d, dm1 = dates[i], dates[i - 1]
        for c in close.columns:
            # ---- manage an open trade first ----
            tr = open_by.get(c)
            if tr is not None:
                lo, hi, cl = low.at[d, c], high.at[d, c], close.at[d, c]
                exit_px = None; reason = None
                if pd.notna(lo) and lo <= tr["stop"]:            # stop first (conservative)
                    exit_px, reason = tr["stop"], "stop"
                elif pd.notna(hi) and hi >= tr["target"]:
                    exit_px, reason = tr["target"], "target"
                elif i - tr["entry_i"] >= p["hold_days"]:
                    exit_px, reason = cl, "time"
                if exit_px is not None and pd.notna(exit_px):
                    net = sb.net_trade_return_pct(tr["entry"], exit_px)
                    trades.append(dict(ticker=c, entry_date=str(dates[tr["entry_i"]])[:10],
                                       exit_date=str(d)[:10], reason=reason, net_pct=net))
                    open_by.pop(c, None)
                continue  # one position per ticker at a time

            # ---- look for a new entry on day d (screen as-of dm1) ----
            if rank.at[dm1, c] > p["daily_top"] or pd.isna(rank.at[dm1, c]):
                continue
            c_dm1, mt, ms, r5, ch = (close.at[dm1, c], ma_t.at[dm1, c], ma_s.at[dm1, c],
                                     ret5.at[dm1, c], chg1.at[dm1, c])
            if pd.isna(ms) or pd.isna(mt) or pd.isna(r5):
                continue
            if not (c_dm1 > mt and r5 > p["uptrend_5d"] and ch < p["pump_pct"]):
                continue
            support = ms
            lo = low.at[d, c]
            if pd.notna(lo) and lo <= support:                   # pullback touched support
                open_by[c] = dict(entry=support, entry_i=i,
                                  stop=support * (1 - p["stop_pct"] / 100.0),
                                  target=support * (1 + p["target_pct"] / 100.0))

    return _metrics(trades)


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, note="no trades")
    df = pd.DataFrame(trades)
    wins = df[df.net_pct > 0]
    # equal-weight sequential compounding as a rough equity proxy
    eq = (1 + df.net_pct / 100.0).cumprod()
    peak = eq.cummax(); mdd = ((peak - eq) / peak).max() * 100
    by = df.reason.value_counts().to_dict()
    return dict(
        n=len(df), win_rate=round(len(wins) / len(df) * 100, 1),
        avg_net=round(df.net_pct.mean(), 2), median_net=round(df.net_pct.median(), 2),
        best=round(df.net_pct.max(), 2), worst=round(df.net_pct.min(), 2),
        sum_net=round(df.net_pct.sum(), 1), compound_x=round(eq.iloc[-1], 3),
        mdd_pct=round(mdd, 1), exits=by,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-07-03")
    ap.add_argument("--universe-top", type=int, default=DEFAULTS["universe_top"])
    a = ap.parse_args()
    p = dict(DEFAULTS, universe_top=a.universe_top)

    print(f"[1/3] universe: current top-{p['universe_top']} by 거래대금 ...")
    codes = get_universe(p["universe_top"])
    print(f"      {len(codes)} tickers")
    print(f"[2/3] prices {a.start}~{a.end} (cache: {CACHE}) ...")
    prices = get_prices(codes, a.start, a.end)
    print(f"      {len(prices)} tickers with data")
    print(f"[3/3] backtest (v1: support MA{p['support_ma']}, stop -{p['stop_pct']}%, "
          f"target +{p['target_pct']}%, hold {p['hold_days']}d) ...")
    res = backtest(prices, p)
    print("\n=== RESULT (v1) ===")
    for k, v in res.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
