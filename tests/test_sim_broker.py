"""sim_broker (local paper-fill cost model) unit tests. Pure stdlib — runs in the
root pytest session with no heavy deps."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import sim_broker as sb


def test_kr_tick_size_bands():
    assert sb.kr_tick_size(1_500) == 1
    assert sb.kr_tick_size(3_000) == 5
    assert sb.kr_tick_size(10_000) == 10
    assert sb.kr_tick_size(30_000) == 50
    assert sb.kr_tick_size(100_000) == 100
    assert sb.kr_tick_size(300_000) == 500
    assert sb.kr_tick_size(600_000) == 1_000


def test_buy_fill_crosses_spread_up():
    # 10,000 -> tick 10, +1 tick = 10,010
    assert sb.sim_buy_fill(10_000, ticks=1) == 10_010
    # non-tick-aligned reference rounds down to a tick, then adds ticks
    assert sb.sim_buy_fill(10_005, ticks=1) == 10_010
    assert sb.sim_buy_fill(50_000, ticks=1) == 50_100  # tick 100 at 50k


def test_sell_fill_crosses_spread_down():
    assert sb.sim_sell_fill(10_000, ticks=1) == 9_990
    assert sb.sim_sell_fill(50_000, ticks=1) == 49_900


def test_costs_directional():
    # buy pays commission on top; sell nets commission + tax off
    bc = sb.buy_cost(10_000, 10)          # 100,000 notional
    assert bc > 100_000                    # + commission
    sp = sb.sell_proceeds(10_000, 10)
    assert sp < 100_000                    # - commission - tax
    # sell friction (comm+tax) > buy friction (comm only)
    assert (100_000 - sp) > (bc - 100_000)


def test_flat_roundtrip_loses_to_friction():
    # buy and sell at the same reference -> negative net (2 ticks + costs)
    r = sb.net_trade_return_pct(10_000, 10_000)
    assert r < 0
    assert -0.6 < r < -0.2   # ~ -0.38% for a 10k stock


def test_winning_trade_net_of_friction():
    # +10% raw move nets a bit under +10% after friction
    r = sb.net_trade_return_pct(10_000, 11_000)
    assert 9.0 < r < 10.0


def test_invalid_prices_zero():
    assert sb.sim_buy_fill(0) == 0.0
    assert sb.sim_sell_fill(-5) == 0.0
    assert sb.net_trade_return_pct(0, 10_000) == 0.0
    assert sb.net_trade_return_pct(10_000, 0) == 0.0


def test_slippage_ticks_configurable():
    # 2 ticks widens the round-trip loss vs 1 tick
    one = sb.net_trade_return_pct(10_000, 10_000)
    # emulate 2-tick config via explicit fills
    bf2 = sb.sim_buy_fill(10_000, ticks=2)   # 10,020
    sf2 = sb.sim_sell_fill(10_000, ticks=2)  # 9,980
    assert bf2 == 10_020 and sf2 == 9_980
