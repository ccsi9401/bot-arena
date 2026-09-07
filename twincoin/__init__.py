"""TWINCOIN — Twin-Coin Trend Bot. BTC/ETH long-only daily trend follower on Alpaca paper.

Package layout:
  strategy.py  indicators + the six-vote (verbatim from the backtest engine)
  data.py      Alpaca daily + 4H bars, Coinbase cross-check for phantom wicks
  broker.py    Alpaca REST adapter, paper-only
  engine.py    the cycle: sync fills, reconcile, mark, risk rules, floors, exits, entries

The strategy design is documented in the published spec ("Twin-Coin Trend Bot") and its
backtest in Documents/Alpaca Trading EJ/Twin-Coin-Trend-Bot/backtest/. The engine here
mirrors that backtest bar for bar; when they disagree, the backtest is the reference.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "twincoin.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Load KEY=VALUE lines from ROOT/.env into os.environ for local runs. Never logs values."""
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def credentials(cfg: dict | None = None) -> tuple[str | None, str | None]:
    """(key, secret) from TWINCOIN_API_KEY / TWINCOIN_API_SECRET. Never print these."""
    prefix = (cfg or {}).get("meta", {}).get("account_env_prefix", "TWINCOIN")
    return os.environ.get(f"{prefix}_API_KEY"), os.environ.get(f"{prefix}_API_SECRET")


__all__ = ["ROOT", "DEFAULT_CONFIG", "load_config", "load_dotenv", "credentials"]
