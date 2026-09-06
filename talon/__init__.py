"""TALON — long-only crypto trend/dip ensemble with a rising floor.

Package layout:
  strategies.py  indicators + (df, cfg) -> 0/1 Series strategy functions
  planner.py     scoring, cross-sectional momentum, sizing, gross cap, kill switch
  floor.py       PositionFloor (per-trade ratchet) + AccountFloor (locked reserve)
  data.py        Alpaca crypto bars (paginated, retried) + align()
  broker.py      Alpaca REST adapter with the two-switch live-money guard

The backtest (backtest/backtest.py) and the live cycle (run_talon.py) import and
call the SAME planner.plan() and floor.PositionFloor — there is no second copy of
the sizing loop anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "talon.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Read config/talon.yaml (or the given path). Every tunable lives there."""
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def credentials(cfg: dict | None = None) -> tuple[str | None, str | None]:
    """(key, secret) from TALON_API_KEY / TALON_API_SECRET, or (None, None).

    Never written to disk, never logged — callers must not print these.
    """
    prefix = (cfg or {}).get("meta", {}).get("account_env_prefix", "TALON")
    return os.environ.get(f"{prefix}_API_KEY"), os.environ.get(f"{prefix}_API_SECRET")


__all__ = ["ROOT", "DEFAULT_CONFIG", "load_config", "credentials"]
