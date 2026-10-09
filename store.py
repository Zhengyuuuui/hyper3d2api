"""SQLite account store for hyper3d2api.

Single source of truth for registered accounts. Replaces accounts.jsonl.

Schema
    accounts(
        id             INTEGER PK,
        email          TEXT UNIQUE,      -- login identity
        username       TEXT,
        password       TEXT,             -- generated at signup
        user_uuid      TEXT UNIQUE,
        token          TEXT,             -- JWT used as `Authorization: Bearer`
        mailbox_id     INTEGER,          -- CF temp-email address id
        balance        REAL,             -- last known balance
        plan           TEXT,             -- highest_level (Free/Lite/...)
        trial_eligibility INTEGER,
        status         TEXT,             -- active | banned | exhausted | disabled
        credits_used   REAL DEFAULT 0,
        gen_count      INTEGER DEFAULT 0,
        last_used_at   TEXT,
        last_checked_at TEXT,
        registered_at  TEXT,
        note           TEXT
    )
    ledger(
        id         INTEGER PK,
        email      TEXT,                 -- accounts.email
        job_id     TEXT,
        kind       TEXT,                 -- video | image
        model      TEXT,
        credits    REAL,
        balance_after REAL,
        created_at TEXT
    )

The DB file itself is gitignored (contains credentials).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "accounts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT    NOT NULL UNIQUE,
    username          TEXT,
    password          TEXT,
    user_uuid         TEXT    UNIQUE,
    token             TEXT,
    mailbox_id        INTEGER,
    balance           REAL,
    plan              TEXT,
    trial_eligibility INTEGER,
    status            TEXT    NOT NULL DEFAULT 'active',
    credits_used      REAL    NOT NULL DEFAULT 0,
    gen_count         INTEGER NOT NULL DEFAULT 0,
    last_used_at      TEXT,
    last_checked_at   TEXT,
    registered_at     TEXT,
    note              TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL,
    job_id        TEXT,
    kind          TEXT,
    model         TEXT,
    credits       REAL,
    balance_after REAL,
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_status  ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_balance ON accounts(balance);
CREATE INDEX IF NOT EXISTS idx_ledger_email     ON ledger(email);
"""

COLUMNS = [
    "email", "username", "password", "user_uuid", "token", "mailbox_id",
    "balance", "plan", "trial_eligibility", "status", "note",
    "credits_used", "gen_count", "last_used_at", "last_checked_at", "registered_at",
]

# columns that must never be overwritten by an import (runtime counters)
COUNTER_COLUMNS = {"credits_used", "gen_count", "last_used_at", "last_checked_at"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class AccountStore:
    """Thread-safe: sqlite connections are per-thread (FastAPI runs sync
    endpoints on arbitrary threadpool threads; a shared connection raises
    ProgrammingError)."""

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        # initialize schema once on a dedicated connection
        conn = self._new_conn()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- writes ------------------------------------------------------------
    def upsert(self, acct: Dict[str, Any]) -> int:
        """Insert or update by email. Returns the row id."""
        with self._write_lock:
            return self._upsert(acct)

    def _upsert(self, acct: Dict[str, Any]) -> int:
        data = {k: acct.get(k) for k in COLUMNS}
        if data.get("registered_at") is None:
            data["registered_at"] = now_iso()
        if data.get("status") is None:
            data["status"] = "active"
        if data.get("credits_used") is None:
            data["credits_used"] = 0.0
        if data.get("gen_count") is None:
            data["gen_count"] = 0

        row = self._conn.execute(
            "SELECT id FROM accounts WHERE email = ?", (data["email"],)
        ).fetchone()

        if row:
            # don't clobber runtime counters on re-import
            upd = [c for c in COLUMNS
                   if c != "email" and not (c in COUNTER_COLUMNS and data.get(c) is None)]
            sets = ", ".join(f"{c} = ?" for c in upd)
            vals = [data[c] for c in upd]
            self._conn.execute(
                f"UPDATE accounts SET {sets} WHERE email = ?", (*vals, data["email"])
            )
            rid = row["id"]
        else:
            cols = ", ".join(COLUMNS)
            ph = ", ".join("?" for _ in COLUMNS)
            cur = self._conn.execute(
                f"INSERT INTO accounts ({cols}) VALUES ({ph})",
                tuple(data[c] for c in COLUMNS),
            )
            rid = cur.lastrowid
        self._conn.commit()
        return rid

    def mark_status(self, email: str, status: str, note: Optional[str] = None) -> None:
        with self._write_lock:
            self._mark_status(email, status, note)

    def _mark_status(self, email: str, status: str, note: Optional[str] = None) -> None:
        if note is None:
            self._conn.execute(
                "UPDATE accounts SET status=?, last_checked_at=? WHERE email=?",
                (status, now_iso(), email))
        else:
            self._conn.execute(
                "UPDATE accounts SET status=?, note=?, last_checked_at=? WHERE email=?",
                (status, note, now_iso(), email))
        self._conn.commit()

    def set_balance(self, email: str, balance: float, plan: Optional[str] = None,
                    trial: Optional[bool] = None) -> None:
        with self._write_lock:
            self._set_balance(email, balance, plan, trial)

    def _set_balance(self, email: str, balance: float, plan: Optional[str] = None,
                     trial: Optional[bool] = None) -> None:
        sets = ["balance = ?", "last_checked_at = ?"]
        vals: List[Any] = [balance, now_iso()]
        if plan is not None:
            sets.append("plan = ?")
            vals.append(plan)
        if trial is not None:
            sets.append("trial_eligibility = ?")
            vals.append(1 if trial else 0)
        vals.append(email)
        self._conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE email = ?", vals)
        self._conn.commit()

    def record_use(self, email: str, *, job_id: Optional[str] = None,
                   kind: Optional[str] = None, model: Optional[str] = None,
                   credits: float = 0.0, balance_after: Optional[float] = None) -> None:
        with self._write_lock:
            self._record_use(email, job_id=job_id, kind=kind, model=model,
                             credits=credits, balance_after=balance_after)

    def _record_use(self, email: str, *, job_id: Optional[str] = None,
                    kind: Optional[str] = None, model: Optional[str] = None,
                    credits: float = 0.0, balance_after: Optional[float] = None) -> None:
        self._conn.execute(
            "UPDATE accounts SET gen_count = gen_count + 1, credits_used = credits_used + ?, "
            "last_used_at = ? WHERE email = ?", (credits, now_iso(), email))
        self._conn.execute(
            "INSERT INTO ledger (email, job_id, kind, model, credits, balance_after, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (email, job_id, kind, model, credits, balance_after, now_iso()))
        if balance_after is not None:
            self._conn.execute("UPDATE accounts SET balance = ?, last_checked_at = ? WHERE email = ?",
                               (balance_after, now_iso(), email))
        self._conn.commit()

    # -- reads -------------------------------------------------------------
    def get(self, email: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def list(self, *, status: Optional[str] = None,
             min_balance: Optional[float] = None,
             order: str = "balance DESC, id ASC") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM accounts WHERE 1=1"
        args: List[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if min_balance is not None:
            sql += " AND COALESCE(balance, 0) >= ?"
            args.append(min_balance)
        sql += f" ORDER BY {order}"
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def pick_usable(self, *, min_balance: float = 5.0) -> Optional[Dict[str, Any]]:
        """Cheapest-viable account: active and with enough credits."""
        rows = self._conn.execute(
            "SELECT * FROM accounts WHERE status='active' AND COALESCE(balance,0) >= ? "
            "ORDER BY COALESCE(balance,0) DESC, gen_count ASC LIMIT 1",
            (min_balance,)).fetchall()
        return dict(rows[0]) if rows else None

    def delete(self, email: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM accounts WHERE email = ?", (email,))
            self._conn.commit()
            return cur.rowcount > 0

    def ledger(self, email: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ledger"
        args: List[Any] = []
        if email:
            sql += " WHERE email = ?"
            args.append(email)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def stats(self) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(balance),0) total_credits, "
            "COALESCE(SUM(gen_count),0) gens, COALESCE(SUM(credits_used),0) used "
            "FROM accounts").fetchone()
        by_status = {r["status"]: r["n"] for r in self._conn.execute(
            "SELECT status, COUNT(*) n FROM accounts GROUP BY status")}
        return {"count": row["n"], "total_credits": row["total_credits"],
                "generations": row["gens"], "credits_used": row["used"],
                "by_status": by_status}

    # -- migration ---------------------------------------------------------
    def import_jsonl(self, path: str) -> Dict[str, int]:
        """Import legacy accounts.jsonl. Idempotent (upsert by email)."""
        added = updated = skipped = 0
        if not os.path.exists(path):
            return {"added": 0, "updated": 0, "skipped": 0}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if not a.get("email"):
                    skipped += 1
                    continue
                existed = self.get(a["email"]) is not None
                self.upsert(a)
                if existed:
                    updated += 1
                else:
                    added += 1
        return {"added": added, "updated": updated, "skipped": skipped}

# ---------------------------------------------------------------------------
# CLI:  python3 store.py list|stats|get|import-jsonl|verify
# ---------------------------------------------------------------------------
def _main():
    import argparse
    ap = argparse.ArgumentParser(description="account store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--db", default=DEFAULT_DB)
    p = sub.add_parser("list"); p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--status"); p.add_argument("--min-balance", type=float)
    p = sub.add_parser("stats"); p.add_argument("--db", default=DEFAULT_DB)
    p = sub.add_parser("get"); p.add_argument("email"); p.add_argument("--db", default=DEFAULT_DB)
    p = sub.add_parser("import-jsonl"); p.add_argument("path"); p.add_argument("--db", default=DEFAULT_DB)
    p = sub.add_parser("verify"); p.add_argument("--db", default=DEFAULT_DB)
    p = sub.add_parser("pick"); p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--min-balance", type=float, default=5.0)
    args = ap.parse_args()

    st = AccountStore(args.db)
    try:
        if args.cmd == "init":
            print(f"initialized {args.db}")
        elif args.cmd == "list":
            rows = st.list(status=args.status, min_balance=args.min_balance)
            print(f"{'id':>3} {'email':32s} {'bal':>6} {'plan':7} {'st':8} {'gens':>4}  uuid")
            for r in rows:
                print(f"{r['id']:>3} {r['email']:32s} {str(r['balance']):>6} "
                      f"{str(r['plan']):7} {r['status']:8} {r['gen_count']:>4}  "
                      f"{(r['user_uuid'] or '')[:8]}")
            print(f"-- {len(rows)} row(s)")
        elif args.cmd == "stats":
            print(json.dumps(st.stats(), ensure_ascii=False, indent=1))
        elif args.cmd == "get":
            print(json.dumps(st.get(args.email), ensure_ascii=False, indent=1))
        elif args.cmd == "import-jsonl":
            print(json.dumps(st.import_jsonl(args.path), ensure_ascii=False))
        elif args.cmd == "pick":
            print(json.dumps(st.pick_usable(min_balance=args.min_balance), ensure_ascii=False, indent=1))
        elif args.cmd == "verify":
            import httpx
            for r in st.list():
                try:
                    resp = httpx.post("https://api.hyper3d.com/api/user/get_info",
                                      headers={"Authorization": f"Bearer {r['token']}",
                                               "Ai-trace-id": f"verify-{int(time.time())}",
                                               "Content-Type": "application/json"},
                                      content=b"{}", timeout=25)
                    meta = resp.json().get("meta", {})
                    bal = meta.get("balance")
                    plan = (meta.get("subscriptions") or {}).get("highest_level")
                    st.set_balance(r["email"], bal, plan)
                    print(f"  {r['email']:32s} balance={bal} plan={plan}")
                except Exception as e:
                    print(f"  {r['email']:32s} ERROR {e}")
    finally:
        st.close()


if __name__ == "__main__":
    _main()
