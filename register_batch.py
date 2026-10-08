#!/usr/bin/env python3
"""Batch registration runner for Hyper3D.

Usage:
    python3 register_batch.py --count 1 --dry-run     # validate wiring, no signup
    python3 register_batch.py --count 1               # register N accounts
    python3 register_batch.py --count 5 --concurrency 2

Each successful account is appended to accounts.jsonl (gitignored).

Config comes from config.json (gitignored). Relevant keys:
    mail_api_base, mail_admin_passkey, mail_domain,
    cdp_port, site_key, register_delay, register_concurrency

Per FEATURE.md: start with ONE account, verify, then scale.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
from typing import Dict, List, Optional

import mailbox as mailmod
import register as regmod
from store import AccountStore
from turnstile import TurnstileTokenSource

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
ACCOUNTS_PATH = os.path.join(HERE, "accounts.jsonl")
DB_PATH = os.path.join(HERE, "accounts.db")

DEFAULTS = {
    "mail_api_base": "https://yunex-temp-email.zysgm123.workers.dev",
    "mail_admin_passkey": "",
    "mail_domain": "yunex.ccwu.cc",
    "cdp_port": 9366,
    "site_key": "0x4AAAAAADTv8yKBtK5-Zt1E",
    "register_delay": [8, 20],      # seconds between accounts (human pacing)
    "register_concurrency": 1,       # keep at 1 until proven safe
    "code_timeout": 180,
}


def load_config() -> Dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def rand_name(prefix: str = "h3d", n: int = 8) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def register_one(cfg: Dict, tokens: TurnstileTokenSource, idx: int,
                 dry_run: bool = False) -> Optional[Dict]:
    print(f"\n=== account {idx} ===")
    mb = mailmod.TempMailbox.create(
        cfg["mail_api_base"], cfg["mail_admin_passkey"],
        cfg["mail_domain"], name=rand_name())
    print(f"  mailbox: {mb.address} (id={mb.address_id})")

    if dry_run:
        print("  dry-run: mailbox created, skipping signup")
        mb.close()
        return None

    reg = regmod.Hyper3DRegister()
    t0 = time.time()
    username = regmod.random_username()
    password = regmod.random_password()
    try:
        captcha = tokens.get_token()
        r = reg.send_code(mb.address, captcha, "Register")
        print(f"  send_code -> {r['status']}")

        code = mb.wait_for_code(sender_contains="hyper3d",
                                timeout=cfg["code_timeout"], interval=4)
        if not code:
            code = mb.wait_for_code(timeout=60, interval=4)
        if not code:
            print("  ✗ code not received")
            return None
        print(f"  code: {code}  ({time.time()-t0:.0f}s)")

        out = reg.register(
            email=mb.address,
            username=username,
            password=password,
            code=code,
        )
        acct = {
            "email": mb.address,
            "username": out.get("username") or username,
            "password": password,
            "user_uuid": out.get("user_uuid"),
            "token": out.get("token"),
            "mailbox_id": mb.address_id,
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        print(f"  ✓ registered uuid={out.get('user_uuid')}")
        return acct
    except Exception as e:
        print(f"  ✗ failed: {e}")
        return None
    finally:
        reg.close()
        mb.close()


def save_account(acct: Dict, store: AccountStore) -> int:
    """Persist to SQLite (single source of truth). Also mirrors to jsonl."""
    rid = store.upsert(acct)
    with open(ACCOUNTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(acct, ensure_ascii=False) + "\n")
    return rid


def verify_account(token: str, timeout: float = 30) -> Dict:
    """Confirm the fresh token reaches the API and report starting balance."""
    import httpx
    r = httpx.post("https://api.hyper3d.com/api/user/get_info",
                   headers={"Authorization": f"Bearer {token}",
                            "Ai-trace-id": f"verify-{int(time.time())}",
                            "Content-Type": "application/json"},
                   content=b"{}", timeout=timeout)
    meta = r.json().get("meta", {})
    return {"balance": meta.get("balance"),
            "plan": (meta.get("subscriptions") or {}).get("highest_level"),
            "trial_eligibility": (meta.get("subscriptions") or {}).get("trial_eligibility")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("mail_admin_passkey"):
        print("ERROR: mail_admin_passkey missing in config.json")
        sys.exit(1)

    print(f"[batch] mail={cfg['mail_api_base']} domain={cfg['mail_domain']}")
    store = AccountStore(DB_PATH)
    print(f"[batch] db={DB_PATH}  ({store.stats()['count']} existing)")
    tokens = TurnstileTokenSource(port=cfg["cdp_port"], site_key=cfg["site_key"])
    if not tokens.connect(reload=False):
        print("ERROR: cannot attach to Chrome CDP (is hyper3d.ai open?)")
        sys.exit(1)
    print("[batch] turnstile/CDP ready")

    ok = 0
    for i in range(1, args.count + 1):
        acct = register_one(cfg, tokens, i, dry_run=args.dry_run)
        if acct:
            try:
                info = verify_account(acct["token"])
                acct.update(info)
                print(f"  verified: balance={info['balance']} plan={info['plan']}")
            except Exception as e:
                print(f"  (verify failed: {e})")
            save_account(acct, store)
            ok += 1
        if i < args.count:
            lo, hi = cfg["register_delay"]
            d = random.uniform(lo, hi)
            print(f"  ... sleeping {d:.0f}s before next")
            time.sleep(d)

    print(f"\n[batch] done: {ok}/{args.count} registered -> {DB_PATH}")
    print(f"[batch] store: {json.dumps(store.stats(), ensure_ascii=False)}")
    store.close()


if __name__ == "__main__":
    main()