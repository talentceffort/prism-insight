"""Local paper-fill simulator for KR equities — NO broker, NO real orders.

Purpose: let the tracking agent "paper trade" its (auto-screened) picks against
REAL prices with REALISTIC frictions, so forward returns can be watched honestly
without a brokerage/demo account. This module is the pure cost/fill math; the
stateful notional-portfolio accounting (cash + positions + equity curve) is layered
on top separately.

Friction model (all env-configurable):
  - Slippage = TICK-based (호가단위). A market buy crosses the spread → fills ~1
    tick above the reference; a sell fills ~1 tick below. Tick-based (not a flat %)
    because the real minimum friction IS the tick, which scales with price.
  - Commission (위탁수수료) = SIM_COMMISSION_RATE per side (default 0.015%).
  - Securities transaction tax (증권거래세) = SIM_TAX_RATE, SELL side only
    (default 0.15% — 2025+ schedule; VERIFY the current rate).

Self-contained (stdlib only), import-safe under root/prism-us runtimes.
"""
from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# Notional starting capital for the paper portfolio.
SIM_CAPITAL = _env_float("SIM_CAPITAL", 10_000_000.0)
# Slippage in ticks per side (1 = cross the spread by one tick).
SIM_SLIPPAGE_TICKS = _env_int("SIM_SLIPPAGE_TICKS", 1)
# Brokerage commission per side (fraction, not %). 0.00015 = 0.015%.
SIM_COMMISSION_RATE = _env_float("SIM_COMMISSION_RATE", 0.00015)
# Securities transaction tax, SELL side only (fraction). 0.0015 = 0.15% (2025+).
SIM_TAX_RATE = _env_float("SIM_TAX_RATE", 0.0015)


def kr_tick_size(price: float) -> int:
    """KRX 호가단위 (post-2023 unified schedule, KOSPI == KOSDAQ).

    Returns the minimum price increment (KRW) for a given price. VERIFY against
    the current KRX rule if precision matters — this is the 2023 revision.
    """
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _round_to_tick(price: float) -> float:
    """Round DOWN to a valid KRX price tick (a tradable price sits on a tick)."""
    tick = kr_tick_size(price)
    return float(int(price // tick) * tick)


def sim_buy_fill(ref_price: float, ticks: int | None = None) -> float:
    """Simulated BUY fill: reference rounded to a tick, then + `ticks` ticks
    (crossing the spread). Slippage is unfavorable (higher) for a buy."""
    if ref_price <= 0:
        return 0.0
    ticks = SIM_SLIPPAGE_TICKS if ticks is None else ticks
    base = _round_to_tick(ref_price)
    return base + ticks * kr_tick_size(ref_price)


def sim_sell_fill(ref_price: float, ticks: int | None = None) -> float:
    """Simulated SELL fill: reference rounded to a tick, then - `ticks` ticks.
    Slippage is unfavorable (lower) for a sell. Floored at one tick (never <=0)."""
    if ref_price <= 0:
        return 0.0
    ticks = SIM_SLIPPAGE_TICKS if ticks is None else ticks
    tick = kr_tick_size(ref_price)
    base = _round_to_tick(ref_price)
    return max(float(tick), base - ticks * tick)


def buy_cost(fill_price: float, qty: int) -> float:
    """Total cash out for a buy: notional + commission."""
    notional = fill_price * qty
    return notional + notional * SIM_COMMISSION_RATE


def sell_proceeds(fill_price: float, qty: int) -> float:
    """Total cash in for a sell: notional - commission - transaction tax."""
    notional = fill_price * qty
    return notional - notional * SIM_COMMISSION_RATE - notional * SIM_TAX_RATE


def net_trade_return_pct(buy_ref: float, sell_ref: float) -> float:
    """Round-trip NET return % for one share, applying slippage (both sides) +
    commission (both sides) + sell tax. This is what a paper round-trip actually
    keeps after realistic KR frictions.

    Returns 0.0 on invalid input.
    """
    if buy_ref <= 0 or sell_ref <= 0:
        return 0.0
    bf = sim_buy_fill(buy_ref)
    sf = sim_sell_fill(sell_ref)
    cost = buy_cost(bf, 1)          # cash out for 1 share
    proceeds = sell_proceeds(sf, 1)  # cash in for 1 share
    if cost <= 0:
        return 0.0
    return round((proceeds - cost) / cost * 100.0, 4)
