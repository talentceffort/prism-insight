"""TradingMode — one source of truth for WHERE trades go (real / demo / sim).

Keeps the mode's policy (does it touch the broker? is it a paper/observation run?)
in one value object instead of scattering `default_mode == "..."` string checks across
the tracking agents. `sim` = local paper/observation: the dispatch emits buy SIGNALS +
Telegram alerts and NEVER touches KIS (no real/demo account is even resolved).

Pure and stdlib-only so it is trivially testable; only `from_env()` reads config (lazily).
"""
from __future__ import annotations

from dataclasses import dataclass

# Synthetic account identity for sim mode — one source of truth. It namespaces sim rows
# in the account-keyed tables and is returned wherever a real account would otherwise be
# resolved in sim (dispatch + the legacy multi-account backfill), so sim never reaches KIS.
SIM_ACCOUNT_KEY = "sim:0000000000:01"
SIM_ACCOUNT_NAME = "SIM(관찰)"


@dataclass(frozen=True)
class TradingMode:
    name: str  # normalized: "real" | "demo" | "sim"

    @property
    def is_sim(self) -> bool:
        return self.name == "sim"

    @property
    def is_demo(self) -> bool:
        return self.name == "demo"

    @property
    def is_real(self) -> bool:
        return self.name not in ("sim", "demo")

    @property
    def executes_orders(self) -> bool:
        """False only in sim — the buy/sell dispatch emits a signal/alert and skips KIS."""
        return not self.is_sim

    @classmethod
    def from_name(cls, raw: str | None) -> "TradingMode":
        return cls((raw or "demo").strip().lower())

    @classmethod
    def from_env(cls) -> "TradingMode":
        from trading import kis_auth as ka
        return cls.from_name(ka.getEnv().get("default_mode", "demo"))
