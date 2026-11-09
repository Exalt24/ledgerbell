"""Settings, read from one TOML file. Keys never live in it: they come from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
DATA_HOST = "data.alpaca.markets"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RiskRules:
    max_position_pct: float = 5.0  # one symbol may hold at most this share of equity
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 2.0  # equity down this much since yesterday's close: flatten and stop
    stop_loss_pct: float = 3.0  # every entry carries a stop this far under the fill price
    entry_pct: float = 2.5  # the size the example strategy asks for, never above max_position_pct


@dataclass(frozen=True)
class StrategyRules:
    watchlist: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")
    sma_days: int = 20


@dataclass(frozen=True)
class AlertSettings:
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    webhook_url: str = ""  # a text message service: Slack style JSON {"text": ...}


@dataclass(frozen=True)
class Config:
    mode: str = "paper"  # paper, live (needs live_confirmed) or standin (a local test server)
    live_confirmed: bool = False
    base_url: str = f"https://{PAPER_HOST}"
    data_url: str = f"https://{DATA_HOST}"
    feed: str = "iex"
    state_dir: Path = Path("state")
    timezone: str = "America/New_York"
    risk: RiskRules = field(default_factory=RiskRules)
    strategy: StrategyRules = field(default_factory=StrategyRules)
    alerts: AlertSettings = field(default_factory=AlertSettings)

    @property
    def kill_file(self) -> Path:
        return self.state_dir / "KILL"

    @property
    def ledger_file(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    @property
    def plan_file(self) -> Path:
        return self.state_dir / "plan.json"


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _positive(name: str, value: float) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a number above zero, got {value!r}")
    return float(value)


def validate_risk(risk: RiskRules) -> RiskRules:
    _positive("risk.max_position_pct", risk.max_position_pct)
    _positive("risk.daily_loss_limit_pct", risk.daily_loss_limit_pct)
    _positive("risk.stop_loss_pct", risk.stop_loss_pct)
    _positive("risk.entry_pct", risk.entry_pct)
    if risk.max_position_pct > 25:
        raise ConfigError("risk.max_position_pct above 25 is refused: one symbol would be a quarter of the account")
    if risk.stop_loss_pct >= 50:
        raise ConfigError("risk.stop_loss_pct of 50 or more is not a stop")
    if risk.entry_pct > risk.max_position_pct:
        raise ConfigError("risk.entry_pct is above risk.max_position_pct")
    if risk.max_open_positions < 1:
        raise ConfigError("risk.max_open_positions must be at least 1")
    return risk


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    alpaca = _section(raw, "alpaca")
    risk = RiskRules(**{k: v for k, v in _section(raw, "risk").items() if k in RiskRules.__dataclass_fields__})
    strat = _section(raw, "strategy")
    strategy = StrategyRules(
        watchlist=tuple(str(s).upper() for s in strat.get("watchlist", StrategyRules.watchlist)),
        sma_days=int(strat.get("sma_days", StrategyRules.sma_days)),
    )
    alerts = AlertSettings(**{k: v for k, v in _section(raw, "alerts").items() if k in AlertSettings.__dataclass_fields__})
    mode = str(alpaca.get("mode", "paper"))
    if mode not in {"paper", "live", "standin"}:
        raise ConfigError(f"alpaca.mode must be paper, live or standin, got {mode!r}")
    defaults = Config()
    cfg = Config(
        mode=mode,
        live_confirmed=bool(alpaca.get("live_confirmed", False)),
        base_url=str(alpaca.get("base_url", defaults.base_url)),
        data_url=str(alpaca.get("data_url", defaults.data_url)),
        feed=str(alpaca.get("feed", "iex")),
        state_dir=Path(_section(raw, "paths").get("state_dir", "state")),
        timezone=str(raw.get("timezone", defaults.timezone)),
        risk=validate_risk(risk),
        strategy=strategy,
        alerts=alerts,
    )
    return cfg


def credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    """Trade-only keys from the environment. Nothing else is ever read, so nothing else can be leaked."""
    env = os.environ if env is None else env
    key, secret = env.get("APCA_API_KEY_ID", ""), env.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise ConfigError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY (trade-only keys, paper account)")
    return key, secret
