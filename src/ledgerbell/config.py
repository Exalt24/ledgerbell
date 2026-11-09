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
class OptionRules:
    """The options leg. Off by default: a person turns it on in the config.

    Only a single leg long call or put bought to open is ever allowed (see `risk.check_option`); these are its limits.
    """

    enabled: bool = False
    max_premium_pct: float = 1.0  # all option premium held plus the new order, as a share of equity
    min_days_to_expiry: int = 7  # a contract about to expire is mostly time decay
    max_days_to_expiry: int = 45
    stop_premium_pct: float = 50.0  # the exit rule: sell to close when the premium falls this far under the entry
    limit_buffer_pct: float = 5.0  # the entry is a buy limit this far over the ask seen, because the free feed's quotes are modified
    feed: str = "indicative"  # Alpaca's free options feed; "opra" needs a subscription


@dataclass(frozen=True)
class MarketDataSettings:
    provider: str = "alpaca"  # alpaca or finnhub (a free API key, FINNHUB_API_KEY in the environment)
    finnhub_url: str = "https://finnhub.io"


@dataclass(frozen=True)
class AirtableSettings:
    enabled: bool = False  # one summary row per routine run, written with AIRTABLE_TOKEN from the environment
    base_id: str = ""
    table: str = "Ledgerbell run log (test)"  # a table name or a table id (tbl...)
    api_url: str = "https://api.airtable.com"


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
    options: OptionRules = field(default_factory=OptionRules)
    market_data: MarketDataSettings = field(default_factory=MarketDataSettings)
    airtable: AirtableSettings = field(default_factory=AirtableSettings)

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


def validate_options(opt: OptionRules) -> OptionRules:
    _positive("options.max_premium_pct", opt.max_premium_pct)
    _positive("options.stop_premium_pct", opt.stop_premium_pct)
    if not 0 <= opt.limit_buffer_pct <= 20:
        raise ConfigError("options.limit_buffer_pct must be between 0 and 20")
    if opt.max_premium_pct > 5:
        raise ConfigError("options.max_premium_pct above 5 is refused: option premium can go to zero")
    if opt.stop_premium_pct >= 100:
        raise ConfigError("options.stop_premium_pct of 100 or more is not a stop")
    if opt.min_days_to_expiry < 1 or opt.max_days_to_expiry < opt.min_days_to_expiry:
        raise ConfigError("options expiry window needs 1 <= min_days_to_expiry <= max_days_to_expiry")
    if opt.feed not in {"indicative", "opra"}:
        raise ConfigError(f"options.feed must be indicative or opra, got {opt.feed!r}")
    return opt


def _pick(cls, table: dict):
    return cls(**{k: v for k, v in table.items() if k in cls.__dataclass_fields__})


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
    options = validate_options(_pick(OptionRules, _section(raw, "options")))
    market_data = _pick(MarketDataSettings, _section(raw, "market_data"))
    if market_data.provider not in {"alpaca", "finnhub"}:
        raise ConfigError(f"market_data.provider must be alpaca or finnhub, got {market_data.provider!r}")
    airtable = _pick(AirtableSettings, _section(raw, "airtable"))
    if airtable.enabled and not airtable.base_id:
        raise ConfigError("airtable.enabled needs airtable.base_id (python scripts/airtable_setup.py makes the base)")
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
        options=options,
        market_data=market_data,
        airtable=airtable,
    )
    return cfg


def credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    """Trade-only keys from the environment. Nothing else is ever read, so nothing else can be leaked."""
    env = os.environ if env is None else env
    key, secret = env.get("APCA_API_KEY_ID", ""), env.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise ConfigError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY (trade-only keys, paper account)")
    return key, secret


def secret_from_env(name: str, env: dict[str, str] | None = None) -> str:
    """A key for an optional service (FINNHUB_API_KEY, AIRTABLE_TOKEN). Never read from the config file."""
    env = os.environ if env is None else env
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"set {name} in the environment")
    return value
