"""State and journal. State is small, human-readable, and committed to the repo
so every decision the bot made is reviewable after the fact."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from typing import Any, Dict

STATE = "state/pawl_state.json"
JOURNAL = "journal/pawl.jsonl"

EMPTY: Dict[str, Any] = {
    "floors": {}, "roundtrips": {}, "high_water_equity": None,
    "equity_at_last_cycle": None, "consecutive_order_failures": 0,
    "manual_halt": False, "rotation_enabled": True, "last_cycle": None,
}


def load_state(path: str = STATE) -> Dict[str, Any]:
    if not os.path.exists(path):
        return json.loads(json.dumps(EMPTY))
    with open(path, "r", encoding="utf-8") as fh:
        s = json.load(fh)
    for k, v in EMPTY.items():
        s.setdefault(k, v)
    return s


def save_state(state: Dict[str, Any], path: str = STATE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["last_cycle"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def log(event: str, **fields) -> None:
    os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    print(f"[{rec['ts']}] {event}: " + " ".join(f"{k}={v}" for k, v in fields.items()))
