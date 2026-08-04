"""Shared plumbing: config loading, run journal, time helpers.

Every stage of the pipeline writes its artifact through Journal so a run is
fully reproducible from what's committed to the repo.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def now_et() -> datetime:
    return datetime.now(tz=ET)


def load_config(bot: str) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / f"{bot}.yaml").read_text())
    cfg["universe"] = yaml.safe_load((ROOT / "config" / "universe.yaml").read_text())
    return cfg


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:12]


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


@dataclass
class Journal:
    """One directory per run; each pipeline stage writes exactly one artifact."""

    bot: str
    run_id: str
    path: Path

    @classmethod
    def start(cls, bot: str, cfg: dict) -> "Journal":
        run_id = f"{bot}_{now_et():%Y%m%d_%H%M}"
        path = ROOT / "journal" / run_id
        path.mkdir(parents=True, exist_ok=True)
        j = cls(bot=bot, run_id=run_id, path=path)
        j.write("meta", {
            "run_id": run_id,
            "bot": bot,
            "started_et": now_et().isoformat(),
            "config_hash": config_hash(cfg),
            "code_sha": git_sha(),
        })
        return j

    def write(self, stage: str, payload: dict) -> None:
        payload = dict(payload)
        payload.setdefault("_written_et", now_et().isoformat())
        (self.path / f"{stage}.json").write_text(json.dumps(payload, indent=2, default=str))

    def read(self, stage: str) -> dict:
        return json.loads((self.path / f"{stage}.json").read_text())


class State:
    """Audit-copy state under state/<bot>/ — Alpaca is the authority; this is the record."""

    def __init__(self, bot: str):
        self.dir = ROOT / "state" / bot
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def read(self, name: str, default):
        f = self._file(name)
        return json.loads(f.read_text()) if f.exists() else default

    def write(self, name: str, payload) -> None:
        self._file(name).write_text(json.dumps(payload, indent=2, default=str))

    def append_equity_point(self, equity: float, cash: float, note: str = "") -> None:
        curve = self.read("equity_curve", [])
        curve.append({
            "ts_et": now_et().isoformat(),
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "note": note,
        })
        self.write("equity_curve", curve)

    # kill switch --------------------------------------------------------
    def kill_switch_tripped(self) -> bool:
        return bool(self.read("kill_switch", {}).get("tripped", False))

    def trip_kill_switch(self, reason: str) -> None:
        self.write("kill_switch", {"tripped": True, "reason": reason,
                                   "ts_et": now_et().isoformat()})

    # daily circuit breaker ---------------------------------------------
    def daily_halt_active(self) -> bool:
        h = self.read("daily_halt", {})
        return h.get("date") == f"{now_et():%Y-%m-%d}" and h.get("halted", False)

    def trip_daily_halt(self, reason: str) -> None:
        self.write("daily_halt", {"date": f"{now_et():%Y-%m-%d}", "halted": True,
                                  "reason": reason, "ts_et": now_et().isoformat()})
