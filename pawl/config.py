from __future__ import annotations
import os
from typing import Any, Dict
import yaml

REQUIRED = ["meta", "universe", "regime", "momentum", "sizing", "floor", "risk", "costs", "gate"]


def load(path: str = "config/pawl.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"config is missing required sections: {missing}")
    if cfg["momentum"]["exit_threshold"] >= cfg["momentum"]["entry_threshold"]:
        raise ValueError("exit_threshold must sit BELOW entry_threshold -- the gap is the buffer band")
    if cfg["sizing"]["max_total_invested"] > 1.0:
        raise ValueError("max_total_invested > 1.0 would require margin; PAWL is unlevered by design")
    if cfg["meta"]["mode"] not in ("paper", "live"):
        raise ValueError("meta.mode must be paper or live")
    if cfg["meta"]["mode"] == "live" and os.environ.get("PAWL_ALLOW_LIVE") != "yes":
        raise ValueError("config says live but PAWL_ALLOW_LIVE is not set; refusing to trade real money")
    return cfg
