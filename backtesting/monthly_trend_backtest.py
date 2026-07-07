"""Monthly N-month-MA trend backtest (Faber-style) on top-거래대금 KR names.

Buy when the monthly close is above the N-month SMA; sell when it closes back below.
A slow, low-turnover trend follower. Reuses sim_broker for costs and
pullback_backtest.portfolio_equity for the 10-slot portfolio sim + real MDD.

Data via FinanceDataReader. ⚠️ SURVIVORSHIP: the universe is the CURRENT top-N by
거래대금 pulled back many years — today's winners. This inflates a trend-following
backtest (survivors trend up) and suppresses MDD. Directional read only.

Run: .venv-bt/bin/python backtesting/monthly_trend_backtest.py --start 2013-01-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                       # sim_broker
sys.path.insert(0, str(Path(__file__).resolve().parent))  # pullback_backtest
import sim_broker as sb
from pullback_backtest import get_universe, get_prices, portfolio_equity, _metrics


def monthly_trend_trades(prices: dict[str, pd.DataFrame], codes: list[str], ma_months: int = 10,
                         trail_pct: float | None = None, exit_ma_months: int | None = None) -> list[dict]:
    """Enter on a fresh monthly cross above the N-month SMA. Exit at the FIRST of:
      (a) a DAILY trailing stop trail_pct% below the running peak high, or
      (b) a month-end close below the exit SMA (exit_ma_months, default = ma_months).
    trail_pct=None reproduces the pure monthly-MA-break exit. The trailing stop is checked on
    daily bars so it catches fast intra-month crashes the slow monthly signal gives back.
    Signals use only data through the current bar (no lookahead)."""
    rank_of = {c: i + 1 for i, c in enumerate(codes)}
    trades = []
    for c, d in prices.items():
        cl = d["Close"].dropna()
        if cl.empty:
            continue
        m = cl.groupby(cl.index.to_period("M")).tail(1)
        if len(m) < ma_months + 2:
            continue
        ma_v = m.rolling(ma_months).mean().values
        exma_v = (m.rolling(exit_ma_months).mean().values if exit_ma_months else ma_v)
        mv = m.values
        above = mv > ma_v
        m_dates = [str(x.date()) for x in m.index]
        break_px = {m_dates[i]: float(mv[i]) for i in range(len(m))
                    if not pd.isna(exma_v[i]) and mv[i] < exma_v[i]}     # monthly exit-signal months
        hi, lo, cld = d["High"].values, d["Low"].values, d["Close"].values
        d_dates = [str(x.date()) for x in d.index]
        pos_of = {ds: i for i, ds in enumerate(d_dates)}
        prev_above, last_exit = False, ""
        for i in range(len(m)):
            if pd.isna(ma_v[i]) or (exit_ma_months and pd.isna(exma_v[i])):
                continue
            a = bool(above[i])
            if a and not prev_above and m_dates[i] > last_exit:          # fresh cross above
                prev_above, start = a, pos_of.get(m_dates[i])
                if start is None:
                    continue
                e_dt, e_px = m_dates[i], float(mv[i])
                peak, x_dt, x_px, reason = e_px, None, None, None
                for j in range(start + 1, len(d_dates)):
                    if pd.notna(hi[j]) and hi[j] > peak:
                        peak = hi[j]
                    if trail_pct:
                        lvl = peak * (1 - trail_pct / 100.0)
                        if pd.notna(lo[j]) and lo[j] <= lvl:
                            x_dt, x_px, reason = d_dates[j], lvl, "trail"; break
                    if d_dates[j] in break_px:
                        x_dt, x_px, reason = d_dates[j], break_px[d_dates[j]], "ma_break"; break
                if x_dt is None:
                    x_dt, x_px, reason = d_dates[-1], float(cld[-1]), "open_end"
                trades.append(dict(ticker=c, entry_date=e_dt, exit_date=x_dt, entry_price=e_px,
                                   net_pct=sb.net_trade_return_pct(e_px, x_px),
                                   reason=reason, rank=rank_of.get(c, 9999)))
                last_exit = x_dt
                continue
            prev_above = a
    return trades


def trail_sweep(prices: dict[str, pd.DataFrame], codes: list[str], split: str,
                slots: int = 30, ma_months: int = 10) -> None:
    """Keep the monthly-MA-cross ENTRY; sweep a daily trailing-stop EXIT to see if it tames the
    catastrophic MDD (slow monthly exit gives back Korea's fast crashes) while keeping the trend upside."""
    print(f"\n=== TRAILING-STOP SWEEP (entry = monthly {ma_months}M-MA cross · {slots}-slot · "
          f"OOS split @ {split}) ===")
    hdr = f"  {'exit rule':<16}{'trades':>7} | {'ret%':>8}{'ann%':>7}{'MDD%':>8} | {'IS%':>8}{'OOS%':>8}"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for name, kw in [("MA-break only", {}), ("trail 30%", dict(trail_pct=30)),
                     ("trail 25%", dict(trail_pct=25)), ("trail 20%", dict(trail_pct=20)),
                     ("trail 15%", dict(trail_pct=15)), ("trail 10%", dict(trail_pct=10))]:
        trades = monthly_trend_trades(prices, codes, ma_months, **kw)
        mm = portfolio_equity(trades, prices, split, slots=slots, verbose=False)
        print(f"  {name:<16}{len(trades):>7} | {mm['ret']:>+8.0f}{mm['ann']:>+7.1f}{mm['mdd']:>8.1f} | "
              f"{mm['ret_in']:>+8.0f}{mm['ret_oos']:>+8.0f}")
    print("  (want: MDD much less negative while ret/OOS largely kept)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2013-01-01")
    ap.add_argument("--end", default="2026-07-03")
    ap.add_argument("--universe-top", type=int, default=80)
    ap.add_argument("--ma-months", type=int, default=10)
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--trail-sweep", action="store_true", help="sweep daily trailing-stop exits")
    ap.add_argument("--split", default="2023-12-31", help="in-sample/OOS entry-date boundary")
    a = ap.parse_args()

    print(f"[1/3] universe: current top-{a.universe_top} by 거래대금 ...")
    codes = get_universe(a.universe_top)
    print(f"      {len(codes)} tickers")
    print(f"[2/3] prices {a.start}~{a.end} (long history; first fetch is slow) ...")
    prices = get_prices(codes, a.start, a.end)
    print(f"      {len(prices)} tickers with data")
    if a.trail_sweep:
        print(f"[3/3] trailing-stop exit sweep ...")
        trail_sweep(prices, codes, a.split, slots=a.slots, ma_months=a.ma_months)
        return
    print(f"[3/3] monthly {a.ma_months}-month-MA trend: buy close>MA, sell close<MA ...")
    trades = monthly_trend_trades(prices, codes, a.ma_months)
    print("\n=== PER-TRADE (monthly cross; compound_x/mdd here are the naive proxy — "
          "use the portfolio block below) ===")
    for k, v in _metrics(trades).items():
        print(f"  {k}: {v}")
    portfolio_equity(trades, prices, a.split, slots=a.slots)


if __name__ == "__main__":
    main()
