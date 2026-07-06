"""눌림목(pullback) strategy backtest — v1. See docs/DESIGN_pullback_strategy.md.

Public data via FinanceDataReader (no KRX login; KRX locked down public pykrx).
Offline after the OHLCV cache is warm. Reuses sim_broker.py for realistic fills
(tick slippage + commission + sell tax). No-lookahead: screening/support use the
PRIOR day's data; entries fill on the current day.

v1 rules (starting values — sweep to tune):
  universe   = current top-N by 거래대금 (Amount), fetched once (survivorship caveat)
  daily set  = per-day top DAILY_TOP by 거래대금 (Close*Volume proxy)
  screen     = Close_{d-1} > MA20_{d-1}  AND  5d return_{d-1} > UPTREND_5D  AND not pumped
  support    = MA(SUPPORT_MA)_{d-1}   entry if Low_d <= support
  entry fill = min(Open_d, support)   (gap-through the limit fills at the open)
  stop       = support * (1 - STOP_PCT/100)   [stop before target same day; gap-down exits at open]
  target     = support * (1 + TARGET_PCT/100) (None -> run to trailing/time)
  trailing   = peak_high * (1 - TRAIL_PCT/100), max'd with the fixed stop (None disables)
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
    stop_pct=2.0,       # stop below support (%)  [anchored to support, not fill]
    target_pct=5.0,     # take-profit above support (%); None disables (run to trail/time)
    hold_days=3,        # time exit (bars)
    trail_pct=None,     # trailing stop from peak high (%); None disables
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
    """Daily OHLCV per ticker via fdr, cached to pickle. Offline once warm."""
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


def _simulate(op, hi, lo, cl, i0, entry, init_stop, target, trail_pct, hold_days):
    """Forward-simulate one trade from day i0. Conservative + realistic fills:
    stop checked before target intraday; a gap through either level fills at the
    open (worse for stops, realistic for targets); optional trailing stop from the
    running peak high (uses prior days' peak only, never today's, to avoid loosening
    today's stop with today's high). Returns (exit_px, reason, exit_index)."""
    n = len(cl)
    peak = -1.0
    for j in range(i0, n):
        first = (j == i0)
        o, h, l, c = op[j], hi[j], lo[j], cl[j]
        eff_stop = init_stop
        if trail_pct and not first and peak > 0:
            ts = peak * (1 - trail_pct / 100.0)
            if ts > eff_stop:
                eff_stop = ts
        if not first:                                   # resolve open-gaps first
            if pd.notna(o) and o <= eff_stop:
                return o, "stop", j
            if target is not None and pd.notna(o) and o >= target:
                return o, "target", j
        if pd.notna(l) and l <= eff_stop:               # intraday stop (before target)
            return eff_stop, "stop", j
        if target is not None and pd.notna(h) and h >= target:
            return target, "target", j
        if j - i0 >= hold_days:
            return c, "time", j
        if pd.notna(h) and h > peak:
            peak = h
    j = n - 1
    return cl[j], "eod", j


def backtest(prices: dict[str, pd.DataFrame], p: dict) -> list[dict]:
    # Align close/high/low/volume into date x ticker matrices.
    close = pd.DataFrame({c: d["Close"] for c, d in prices.items()}).sort_index()
    open_ = pd.DataFrame({c: d["Open"]  for c, d in prices.items()}).reindex(close.index)
    high  = pd.DataFrame({c: d["High"]  for c, d in prices.items()}).reindex(close.index)
    low   = pd.DataFrame({c: d["Low"]   for c, d in prices.items()}).reindex(close.index)
    vol   = pd.DataFrame({c: d["Volume"] for c, d in prices.items()}).reindex(close.index)

    ma_t = close.rolling(p["trend_ma"]).mean()
    ma_s = close.rolling(p["support_ma"]).mean()
    ret5 = close.pct_change(5)
    chg1 = close.pct_change(1) * 100.0
    high20 = high.rolling(20).max()           # recent peak (pullback-from-high)
    vol_ma = vol.rolling(20).mean()           # baseline volume
    value = close * vol                       # 거래대금 proxy
    # per-day 거래대금 rank (1 = highest); eligible if rank <= daily_top
    rank = value.rank(axis=1, ascending=False)

    dates = close.index
    date_str = [str(x)[:10] for x in dates]
    n = len(dates)
    start_i = max(p["trend_ma"], p["support_ma"], 20, 6) + 1   # warmup for MAs/high20/vol_ma
    trades = []

    for c in close.columns:
        op, hi, lo, cl, vv = (open_[c].values, high[c].values, low[c].values,
                              close[c].values, vol[c].values)
        mt_a, ms_a, r5_a, ch_a = ma_t[c].values, ma_s[c].values, ret5[c].values, chg1[c].values
        h20_a, vm_a, rk_a = high20[c].values, vol_ma[c].values, rank[c].values
        i = start_i
        while i < n:
            dm1 = i - 1
            rk = rk_a[dm1]
            if pd.isna(rk) or rk > p["daily_top"]:
                i += 1; continue
            c1, mt, ms, r5, ch = cl[dm1], mt_a[dm1], ms_a[dm1], r5_a[dm1], ch_a[dm1]
            if pd.isna(ms) or pd.isna(mt) or pd.isna(r5) or pd.isna(c1) or pd.isna(ch):
                i += 1; continue
            if not (c1 > mt and r5 > p["uptrend_5d"] and ch < p["pump_pct"]):
                i += 1; continue
            support = ms
            if pd.isna(lo[i]) or lo[i] > support:                # no pullback touch today
                i += 1; continue

            # entry: buy-limit at support; gap-through the limit fills at the (lower) open
            entry = op[i] if (pd.notna(op[i]) and op[i] < support) else support
            init_stop = support * (1 - p["stop_pct"] / 100.0)
            target = support * (1 + p["target_pct"] / 100.0) if p["target_pct"] else None
            if entry <= init_stop:                               # gapped in at/below stop (knife)
                exit_px, reason, j = entry, "stop", i
            else:
                exit_px, reason, j = _simulate(op, hi, lo, cl, i, entry, init_stop,
                                               target, p.get("trail_pct"), p["hold_days"])

            h20v, vmv = h20_a[dm1], vm_a[dm1]
            trades.append(dict(
                ticker=c, entry_date=date_str[i], exit_date=date_str[j],
                reason=reason, net_pct=sb.net_trade_return_pct(entry, exit_px),
                ret5=round(r5 * 100, 2), ext=round((c1 / mt - 1) * 100, 2),
                depth=round((c1 / support - 1) * 100, 2), chg1=round(ch, 2), rank=int(rk),
                from_high=round((c1 / h20v - 1) * 100, 2) if pd.notna(h20v) else None,
                vol_ratio=round(vv[dm1] / vmv, 2) if (pd.notna(vmv) and vmv) else None,
                gap=round((op[i] / support - 1) * 100, 2) if pd.notna(op[i]) else None,
            ))
            i = j + 1                                            # no overlap on same ticker

    return trades


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, note="no trades")
    df = pd.DataFrame(trades).sort_values("entry_date").reset_index(drop=True)
    wins = df[df.net_pct > 0]
    # equal-weight sequential compounding as a rough equity proxy (no 10-slot cap yet)
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


FEATS = [  # entry features (all as-of prior day, no lookahead) + their meaning
    ("ret5", "5d momentum %"), ("ext", "% above MA20"), ("depth", "pullback depth vs support %"),
    ("chg1", "prior-day move %"), ("rank", "거래대금 rank"), ("from_high", "% below 20d high"),
    ("vol_ratio", "vol / 20d avg"), ("gap", "open vs support % (<0 = gap-down entry)"),
]


def _feat_table(a: pd.DataFrame, b: pd.DataFrame, la: str, lb: str) -> None:
    print(f"  {'feature':<11}{la:>10}{lb:>10}{'Δ(a-b)':>10}   meaning")
    for f, meaning in FEATS:
        if f not in a.columns:
            continue
        av = pd.to_numeric(a[f], errors="coerce").mean()
        bv = pd.to_numeric(b[f], errors="coerce").mean()
        if pd.isna(av) or pd.isna(bv):
            continue
        print(f"  {f:<11}{av:>10.2f}{bv:>10.2f}{av - bv:>+10.2f}   {meaning}")


def _compare(trades: list[dict]) -> None:
    """Contrast entry features of winners vs losers — what discriminates them?"""
    if not trades:
        print("  (no trades to compare)"); return
    df = pd.DataFrame(trades)
    win, lose = df[df.net_pct > 0], df[df.net_pct <= 0]
    print(f"\n=== WINNERS ({len(win)}) vs LOSERS ({len(lose)}) — mean entry features ===")
    _feat_table(win, lose, "winners", "losers")
    print("  exits |", "win:", win.reason.value_counts().to_dict(),
          " lose:", lose.reason.value_counts().to_dict())

    n = max(5, len(df) // 5)                       # tail contrast (biggest wins vs losses)
    top, bot = df.nlargest(n, "net_pct"), df.nsmallest(n, "net_pct")
    print(f"\n=== TOP {n} (avg {top.net_pct.mean():+.2f}%) vs BOTTOM {n} "
          f"(avg {bot.net_pct.mean():+.2f}%) by net return ===")
    _feat_table(top, bot, "top", "bottom")


def _split(trades: list[dict], split_date: str) -> tuple[dict, dict]:
    """Metrics for trades entered in-sample (<= split) vs out-of-sample (> split)."""
    tr = [t for t in trades if t["entry_date"] <= split_date]
    te = [t for t in trades if t["entry_date"] > split_date]
    return _metrics(tr), _metrics(te)


def _num(m: dict, k: str):
    return m.get(k) if m.get("n") else None


def sweep(prices: dict[str, pd.DataFrame], base_p: dict, split_date: str) -> None:
    """Exit-geometry sweep judged on walk-forward OOS. Screening is held fixed; only
    stop/target/hold/trail vary. Ranked by IN-SAMPLE avg — the OOS columns show whether
    the in-sample pick actually survives (guards against curve-fitting to one period)."""
    grid = [dict(stop_pct=s, target_pct=t, hold_days=h, trail_pct=tr)
            for s in (2.0, 3.0) for t in (5.0, 8.0, None)
            for h in (3, 10) for tr in (None, 3.0)]
    rows = []
    for g in grid:
        m_in, m_out = _split(backtest(prices, dict(base_p, **g)), split_date)
        rows.append((g, m_in, m_out))
    rows.sort(key=lambda r: (_num(r[1], "avg_net") if _num(r[1], "avg_net") is not None else -99),
              reverse=True)

    def fnum(x, w=7, d=2):
        return f"{x:>{w}.{d}f}" if isinstance(x, (int, float)) else f"{'—':>{w}}"
    hdr = (f"  {'stop':>4}{'tgt':>5}{'hold':>5}{'trail':>6} | "
           f"{'IN n':>6}{'avg':>7}{'win%':>6}{'mdd':>6} | {'OOS n':>6}{'avg':>7}{'win%':>6}{'worst':>7}")
    print(f"\n=== EXIT-GEOMETRY SWEEP — OOS split @ {split_date} "
          f"(ranked by in-sample avg) ===")
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for g, mi, mo in rows:
        print(f"  {g['stop_pct']:>4}{str(g['target_pct']):>5}{g['hold_days']:>5}"
              f"{str(g['trail_pct']):>6} | "
              f"{(mi.get('n') or 0):>6}{fnum(_num(mi,'avg_net'))}{fnum(_num(mi,'win_rate'),6,1)}"
              f"{fnum(_num(mi,'mdd_pct'),6,0)} | "
              f"{(mo.get('n') or 0):>6}{fnum(_num(mo,'avg_net'))}{fnum(_num(mo,'win_rate'),6,1)}"
              f"{fnum(_num(mo,'worst'),7,2)}")
    print("  (avg = mean net % per trade after costs; edge must clear 0 on OOS to be real)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-07-03")
    ap.add_argument("--universe-top", type=int, default=DEFAULTS["universe_top"])
    ap.add_argument("--sweep", action="store_true", help="exit-geometry sweep with OOS split")
    ap.add_argument("--split", default="2026-01-31", help="in-sample/OOS entry-date boundary")
    a = ap.parse_args()
    p = dict(DEFAULTS, universe_top=a.universe_top)

    print(f"[1/3] universe: current top-{p['universe_top']} by 거래대금 ...")
    codes = get_universe(p["universe_top"])
    print(f"      {len(codes)} tickers")
    print(f"[2/3] prices {a.start}~{a.end} (cache: {CACHE}) ...")
    prices = get_prices(codes, a.start, a.end)
    print(f"      {len(prices)} tickers with data")

    if a.sweep:
        print(f"[3/3] exit-geometry sweep (screening fixed) ...")
        sweep(prices, p, a.split)
        return

    print(f"[3/3] backtest (v1: support MA{p['support_ma']}, stop -{p['stop_pct']}%, "
          f"target +{p['target_pct']}%, hold {p['hold_days']}d) ...")
    trades = backtest(prices, p)
    print("\n=== RESULT (v1) ===")
    for k, v in _metrics(trades).items():
        print(f"  {k}: {v}")
    _compare(trades)
    dump = CACHE / "trades_v1.pkl"
    pd.DataFrame(trades).to_pickle(dump)
    print(f"\n  [{len(trades)} trades dumped -> {dump}]")


if __name__ == "__main__":
    main()
