"""A small HTTP API in front of the risk rules and the alert channels. It runs on Modal (serverless/modal_app.py) or under any ASGI server.

    GET  /health        no secrets, no key: answers {"status": "ok", "mode": ...}
    POST /risk/check    one proposed order plus an account snapshot in, the risk verdict out (needs X-Api-Key)
    POST /alerts        a subject and a body in, sent through the configured alert channels (needs X-Api-Key)

The risk endpoint holds no state and calls no broker: it is `risk.check` behind a JSON door, so a remote caller can ask "would this order pass" without
getting any way to place one. The alert endpoint never takes a recipient from the request, so it cannot be used to mail anyone else.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import risk
from .alerts import Notifier
from .alpaca import Account, Position
from .config import AlertSettings, Config, ConfigError


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    equity: float = Field(ge=0)
    last_equity: float = Field(ge=0)
    cash: float = 0.0
    buying_power: float = Field(ge=0)
    trading_blocked: bool = False


class PositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=12)
    qty: int
    avg_entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    unrealized_pl: float = 0.0
    unrealized_plpc: float = 0.0


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=12)
    side: str
    qty: int
    price: float
    reason: str = Field(default="", max_length=300)


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal: ProposalIn
    account: AccountIn
    positions: list[PositionIn] = Field(default_factory=list, max_length=100)
    kill_engaged: bool = False


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


def create_app(cfg: Config, api_key: str, notifier: Notifier | None = None) -> FastAPI:
    if not api_key:
        raise ConfigError("the API needs a non-empty api key: it fails closed, never open")
    notifier = notifier or Notifier(cfg.alerts)
    app = FastAPI(title="ledgerbell api", docs_url=None, redoc_url=None, openapi_url=None)

    def require_key(x_api_key: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="bad api key")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": cfg.mode}

    @app.post("/risk/check", dependencies=[Depends(require_key)])
    def risk_check(req: CheckIn) -> dict:
        proposal = risk.Proposal(req.proposal.symbol.upper(), req.proposal.side, req.proposal.qty, req.proposal.price, req.proposal.reason, source="api")
        account = Account(req.account.equity, req.account.last_equity, req.account.cash, req.account.buying_power, req.account.trading_blocked)
        positions = [Position(p.symbol.upper(), p.qty, p.avg_entry_price, p.current_price, p.unrealized_pl, p.unrealized_plpc) for p in req.positions]
        verdict = risk.check(proposal, account, positions, cfg.risk, cfg.strategy.watchlist, req.kill_engaged)
        return {"allowed": verdict.allowed, "qty": verdict.qty, "stop_price": verdict.stop_price, "reasons": verdict.reasons}

    @app.post("/alerts", dependencies=[Depends(require_key)])
    def alerts(req: AlertIn) -> dict:
        problems = notifier.send(req.subject, req.body)
        return {"sent": not problems, "problems": problems}

    return app


def app_from_env(env: Mapping[str, str] | None = None) -> FastAPI:
    """The app a hosting platform starts: settings come from the environment (on Modal, from a Secret), never from a file in the image."""
    env = os.environ if env is None else env
    cfg = Config(
        alerts=AlertSettings(
            ses_to=env.get("LEDGERBELL_SES_TO", ""),
            ses_from=env.get("LEDGERBELL_SES_FROM", ""),
            ses_region=env.get("LEDGERBELL_SES_REGION", ""),
        )
    )
    return create_app(cfg, env.get("LEDGERBELL_API_KEY", ""))
