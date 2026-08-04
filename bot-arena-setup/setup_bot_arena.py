#!/usr/bin/env python3
"""Bot Arena one-shot launcher — run this on YOUR computer:

    python setup_bot_arena.py

It will: (1) create the private GitHub repo, (2) upload the bot code,
(3) store the Alpaca keys as encrypted Actions secrets, (4) keep the trading
schedules PAUSED, and (5) kick off the backtest validation gate.

Requires Python 3.9+ and internet. Installs 'pynacl' automatically if missing
(needed to encrypt the secrets the way GitHub requires).
DELETE THIS FILE after it succeeds — it contains your API keys.
"""
import base64
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# ----------------------------------------------------------------- credentials
TOKEN = "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"
OWNER = "ccsi9401"
REPO = "bot-arena"
SECRETS = {
    "SCALPEL_API_KEY": "PK5EDCDBDNNRNTXKDG5E42LF4W",
    "SCALPEL_API_SECRET": "EnD4dvKr2pVSu2SSLAKBTs97MeeBMn9ej8ECYYsfMDt5",
    "GPTDAY_API_KEY": "PKY6ZBTBIYXRQ4ICQTQOVDBG5Y",
    "GPTDAY_API_SECRET": "9bwJjbW56SnLuLrvVGydmxHfD9v9CvApigUeoAixo7Q5",
}
PAUSED_WORKFLOWS = ["scalpel.yml", "scoreboard.yml"]  # enabled on launch day
ZIP = Path(__file__).parent / "bot-arena-files.zip"


def api(path, method="GET", data=None, raw=False):
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        data=(data if raw else json.dumps(data).encode()) if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except Exception:
            return e.code, {}


def step(msg):
    print(f"\n=== {msg}")


def main() -> int:
    if not ZIP.exists():
        print(f"ERROR: {ZIP.name} must sit next to this script.")
        return 1

    step("0/6 Checking token")
    code, me = api("/user")
    if code != 200:
        print(f"ERROR: token rejected (HTTP {code}). Regenerate and retry.")
        return 1
    print(f"    authenticated as {me.get('login')}")

    step("1/6 Creating private repo")
    code, r = api("/user/repos", "POST", {
        "name": REPO, "private": True, "auto_init": False,
        "description": "Bot Arena: Claude vs ChatGPT paper-trading competition"})
    if code == 201:
        print(f"    created {r['full_name']}")
    elif code == 422:
        print("    repo already exists — continuing")
    else:
        print(f"ERROR creating repo: {code} {r.get('message')}")
        return 1

    step("2/6 Uploading code (single commit via Git Data API)")
    tree = []
    with zipfile.ZipFile(ZIP) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for i, name in enumerate(names, 1):
            content = z.read(name)
            code, blob = api(f"/repos/{OWNER}/{REPO}/git/blobs", "POST",
                             {"content": base64.b64encode(content).decode(),
                              "encoding": "base64"})
            if code != 201:
                print(f"ERROR uploading {name}: {code} {blob.get('message')}")
                return 1
            tree.append({"path": name, "mode": "100644", "type": "blob",
                         "sha": blob["sha"]})
            if i % 10 == 0 or i == len(names):
                print(f"    {i}/{len(names)} files")
    code, t = api(f"/repos/{OWNER}/{REPO}/git/trees", "POST", {"tree": tree})
    if code != 201:
        print(f"ERROR creating tree: {code} {t.get('message')}")
        return 1
    code, c = api(f"/repos/{OWNER}/{REPO}/git/commits", "POST",
                  {"message": "Bot Arena v1.0 — Round 1: SCALPEL vs GPT-DAY",
                   "tree": t["sha"], "parents": []})
    if code != 201:
        print(f"ERROR creating commit: {code} {c.get('message')}")
        return 1
    code, ref = api(f"/repos/{OWNER}/{REPO}/git/refs", "POST",
                    {"ref": "refs/heads/main", "sha": c["sha"]})
    if code == 422:  # branch exists (rerun) — force update
        code, ref = api(f"/repos/{OWNER}/{REPO}/git/refs/heads/main", "PATCH",
                        {"sha": c["sha"], "force": True})
    if code not in (200, 201):
        print(f"ERROR setting main branch: {code} {ref.get('message')}")
        return 1
    api(f"/repos/{OWNER}/{REPO}", "PATCH", {"default_branch": "main"})
    print(f"    pushed commit {c['sha'][:8]} to main")

    step("3/6 Encrypting + storing Actions secrets")
    try:
        from nacl import encoding, public  # noqa
    except ImportError:
        print("    installing pynacl ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "pynacl"], check=True)
        from nacl import encoding, public  # noqa
    code, pk = api(f"/repos/{OWNER}/{REPO}/actions/secrets/public-key")
    if code != 200:
        print(f"ERROR fetching public key: {code} {pk.get('message')}")
        return 1
    sealed = public.SealedBox(public.PublicKey(
        pk["key"].encode(), encoding.Base64Encoder()))
    for name, value in SECRETS.items():
        enc = base64.b64encode(sealed.encrypt(value.encode())).decode()
        code, r = api(f"/repos/{OWNER}/{REPO}/actions/secrets/{name}", "PUT",
                      {"encrypted_value": enc, "key_id": pk["key_id"]})
        print(f"    {name}: {'ok' if code in (201, 204) else f'FAILED {code}'}")
        if code not in (201, 204):
            return 1

    step("4/6 Pausing trading schedules (until launch day)")
    for wf in PAUSED_WORKFLOWS:
        code, r = api(f"/repos/{OWNER}/{REPO}/actions/workflows/{wf}/disable", "PUT")
        status = "paused" if code == 204 else f"HTTP {code} {r.get('message', '')}"
        print(f"    {wf}: {status}")

    step("5/6 Kicking off the backtest validation gate")
    code, r = api(f"/repos/{OWNER}/{REPO}/actions/workflows/backtest.yml/dispatches",
                  "POST", {"ref": "main"})
    if code == 204:
        print("    backtest-validation dispatched")
    else:
        print(f"    dispatch failed: {code} {r.get('message')} "
              f"(you can run it manually from the Actions tab)")

    step("6/6 Done")
    print(f"""
    Repo:    https://github.com/{OWNER}/{REPO}
    Watch:   https://github.com/{OWNER}/{REPO}/actions   (backtest-validation is running)

    Tell Claude the backtest finished (or paste any error from the Actions log).
    Trading schedules stay PAUSED until the gate passes and you set the start date.

    >>> DELETE this script now — it contains your API keys. <<<""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
