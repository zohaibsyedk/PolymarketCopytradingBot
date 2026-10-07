"""User-editable settings.

Settings are stored as JSON in the data directory. Every field has a default that
reflects the research in docs/STRATEGY.md, so a fresh install works without any
configuration. The web terminal edits these values through ``PUT /api/settings``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from polycopy.paths import data_dir

log = logging.getLogger(__name__)

Mode = Literal["paper", "live"]
RiskProfile = Literal["conservative", "balanced", "aggressive", "custom"]


class RiskSettings(BaseModel):
    """How much capital is put at risk and where the hard limits are."""

    # Bankroll that sizing is based on. "balance" uses the full equity of the
    # account; "fixed" caps it at allocated_bankroll_usdc so the bot only ever
    # uses part of the wallet.
    bankroll_mode: Literal["balance", "fixed"] = "balance"
    allocated_bankroll_usdc: float = Field(500.0, ge=0)
    per_trade_pct: float = Field(0.02, gt=0, le=0.25)
    min_trade_usdc: float = Field(2.0, ge=1.0)
    max_trade_usdc: float = Field(150.0, ge=1.0)
    max_market_exposure_pct: float = Field(0.08, gt=0, le=1)
    max_leader_exposure_pct: float = Field(0.30, gt=0, le=1)
    max_total_exposure_pct: float = Field(0.75, gt=0, le=1)
    cash_reserve_pct: float = Field(0.05, ge=0, le=0.9)
    max_open_positions: int = Field(30, ge=1, le=500)
    # New entries stop for the rest of the UTC day once the day's loss exceeds this.
    daily_loss_limit_pct: float = Field(0.12, gt=0, le=1)
    # The bot pauses itself entirely if equity falls this far below its peak.
    max_drawdown_pause_pct: float = Field(0.30, gt=0, le=1)


class FilterSettings(BaseModel):
    """Which leader trades are worth copying."""

    max_hours_to_resolution: float = Field(48.0, gt=0, le=24 * 30)
    min_minutes_to_resolution: float = Field(20.0, ge=0)
    min_entry_price: float = Field(0.05, gt=0, lt=1)
    max_entry_price: float = Field(0.94, gt=0, lt=1)
    # Never pay more than the leader's price + max_chase_cents, nor more than
    # leader_price * (1 + max_chase_pct). The tighter of the two applies.
    max_chase_cents: float = Field(0.02, ge=0, le=0.5)
    max_chase_pct: float = Field(0.06, ge=0, le=1)
    max_spread: float = Field(0.06, gt=0, le=1)
    max_signal_age_sec: float = Field(120.0, gt=0)
    min_leader_trade_usdc: float = Field(25.0, ge=0)
    skip_in_play_sports: bool = True
    min_market_liquidity_usdc: float = Field(500.0, ge=0)
    allow_adds: bool = True
    skip_conflicting_signals: bool = True
    excluded_keywords: list[str] = Field(default_factory=list)


class ExitSettings(BaseModel):
    """How positions are closed."""

    mirror_leader_sells: bool = True
    exit_slippage_cents: float = Field(0.04, ge=0, le=0.5)
    exit_retry_minutes: float = Field(30.0, ge=0)
    # Sell once the price reaches this level instead of waiting for resolution
    # (frees capital early). None keeps positions until resolution.
    take_profit_price: float | None = Field(None, gt=0.5, lt=1)
    auto_redeem: bool = True


class DiscoverySettings(BaseModel):
    """How leaders are found and scored."""

    interval_hours: float = Field(6.0, ge=0.5)
    leaderboard_windows: list[Literal["day", "week", "month", "all"]] = Field(
        default_factory=lambda: ["week", "month"]
    )
    leaderboard_categories: list[str] = Field(default_factory=lambda: ["sports", "crypto"])
    leaderboard_depth: int = Field(150, ge=10, le=1000)
    max_candidates: int = Field(300, ge=10, le=2000)
    lookback_days: int = Field(30, ge=3, le=120)
    max_trades_per_trader: int = Field(3000, ge=100, le=20000)
    min_resolved_positions: int = Field(15, ge=3)
    min_score: float = Field(40.0, ge=0, le=100)
    max_followed: int = Field(12, ge=1, le=50)
    sim_stake_usdc: float = Field(100.0, gt=0)
    sim_slippage_cents: float = Field(0.01, ge=0, le=0.2)
    max_trades_per_day: float = Field(150.0, gt=0)
    min_short_horizon_share: float = Field(0.25, ge=0, le=1)
    # Live feedback: a followed leader whose copied positions lose more than this
    # (as a fraction of the capital we put in) after enough samples is benched.
    bench_live_roi: float = Field(-0.15, le=0)
    bench_min_positions: int = Field(8, ge=3)


class ExecutionSettings(BaseModel):
    """How leader trades are detected."""

    use_realtime_stream: bool = True
    poll_interval_sec: float = Field(4.0, ge=1.0, le=120)
    aggregation_window_sec: float = Field(1.5, ge=0, le=30)


class Settings(BaseModel):
    mode: Mode = "paper"
    risk_profile: RiskProfile = "balanced"
    paper_starting_balance: float = Field(1000.0, gt=0)
    auto_start: bool = True
    risk: RiskSettings = Field(default_factory=RiskSettings)
    filters: FilterSettings = Field(default_factory=FilterSettings)
    exits: ExitSettings = Field(default_factory=ExitSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)


# Presets that overwrite the risk block. "balanced" equals the defaults above.
RISK_PRESETS: dict[str, dict[str, float | int]] = {
    "conservative": {
        "per_trade_pct": 0.01,
        "max_trade_usdc": 75.0,
        "max_market_exposure_pct": 0.05,
        "max_leader_exposure_pct": 0.20,
        "max_total_exposure_pct": 0.50,
        "cash_reserve_pct": 0.15,
        "max_open_positions": 20,
        "daily_loss_limit_pct": 0.07,
        "max_drawdown_pause_pct": 0.20,
    },
    "balanced": {
        "per_trade_pct": 0.02,
        "max_trade_usdc": 150.0,
        "max_market_exposure_pct": 0.08,
        "max_leader_exposure_pct": 0.30,
        "max_total_exposure_pct": 0.75,
        "cash_reserve_pct": 0.05,
        "max_open_positions": 30,
        "daily_loss_limit_pct": 0.12,
        "max_drawdown_pause_pct": 0.30,
    },
    "aggressive": {
        "per_trade_pct": 0.04,
        "max_trade_usdc": 400.0,
        "max_market_exposure_pct": 0.12,
        "max_leader_exposure_pct": 0.40,
        "max_total_exposure_pct": 0.90,
        "cash_reserve_pct": 0.02,
        "max_open_positions": 45,
        "daily_loss_limit_pct": 0.20,
        "max_drawdown_pause_pct": 0.40,
    },
}


def apply_risk_profile(settings: Settings, profile: RiskProfile) -> Settings:
    """Return a copy of ``settings`` with the risk block replaced by a preset."""
    if profile == "custom":
        return settings.model_copy(update={"risk_profile": "custom"})
    risk = settings.risk.model_copy(update=RISK_PRESETS[profile])
    return settings.model_copy(update={"risk": risk, "risk_profile": profile})


class SettingsStore:
    """Loads and saves :class:`Settings` as JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "settings.json")
        self._settings = self._load()

    @property
    def settings(self) -> Settings:
        return self._settings

    def _load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            raw = json.loads(self.path.read_text())
            return Settings.model_validate(raw)
        except (OSError, ValueError, ValidationError) as error:
            log.warning("Could not read %s (%s); using defaults", self.path, error)
            return Settings()

    def save(self, settings: Settings) -> Settings:
        validated = Settings.model_validate(settings.model_dump())
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(validated.model_dump(), indent=2))
        tmp.replace(self.path)
        self._settings = validated
        return validated

    def update(self, patch: dict) -> Settings:
        """Deep-merge ``patch`` into the current settings and save."""
        merged = _deep_merge(self._settings.model_dump(), patch)
        settings = Settings.model_validate(merged)
        profile = patch.get("risk_profile")
        if profile and profile != "custom" and "risk" not in patch:
            settings = apply_risk_profile(settings, profile)
        elif "risk" in patch:
            settings = settings.model_copy(update={"risk_profile": detect_profile(settings.risk)})
        return self.save(settings)


def detect_profile(risk: RiskSettings) -> RiskProfile:
    """Name of the preset the risk limits match, or "custom"."""
    values = risk.model_dump()
    for name, preset in RISK_PRESETS.items():
        if all(abs(float(values[k]) - float(v)) < 1e-9 for k, v in preset.items()):
            return name  # type: ignore[return-value]
    return "custom"


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
