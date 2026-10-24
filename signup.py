"""Headful-Chrome batch registration, exposed to the console.

Mirrors the minimax2api signup panel shape: the console POSTs a job, then
polls its progress. Registration itself is already protocol-level (the upstream
register endpoint needs no Turnstile), but the *verification-code mail* does,
so the flow is:

    render Turnstile (CDP headful Chrome)
      -> POST /api/auth/send_email_verification_code {email, type:"Register"}
      -> poll CF temp mailbox for the 6-digit code
      -> POST /api/auth/register {...}
      -> verify balance, land the account in accounts.db

Progress is kept in memory per session; each step appends a line so the
operator can see where a slow run is stuck.
"""
from __future__ import annotations

import random
import string
import threading
import time
import uuid as uuidlib
from typing import Any, Dict, List, Optional

import mailbox as mailmod
import register as regmod
from store import AccountStore

# upstream register-field limits (server 400 text, Confirmed)
USERNAME_MAX = 24
EMAIL_MAX = 50
PASSWORD_MIN = 6


def rand_prefix(name: Optional[str] = None) -> str:
    base = (name or "h3d").strip()
    base = "".join(c for c in base if c.isalnum())[:10] or "h3d"
    return base + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


class SignupSession:
    """One background registration run."""

    def __init__(self, sid: str, count: int, cfg: Dict[str, Any]):
        self.sid = sid
        self.count = count
        self.cfg = dict(cfg)
        self.state = "queued"          # queued | running | done | cancelled | error
        self.lines: List[Dict[str, str]] = []
        self.results: List[Dict[str, Any]] = []
        self.created = 0
        self.failed = 0
        self.current: Optional[str] = None
        self.error: Optional[str] = None
        self.started_at = 0.0
        self.finished_at = 0.0
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- logging -----------------------------------------------------------
    def log(self, text: str, kind: str = "info") -> None:
        with self._lock:
            self.lines.append({"at": time.strftime("%H:%M:%S"),
                               "kind": kind, "text": text})
            if len(self.lines) > 400:
                self.lines = self.lines[-400:]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.sid,
                "state": self.state,
                "count": self.count,
                "created": self.created,
                "failed": self.failed,
                "current": self.current,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at else 0,
                "lines": self.lines[-120:],
                "results": self.results,
            }

    def cancel(self) -> None:
        self._cancel.set()


class SignupManager:
    def __init__(self, store: AccountStore, tokens, config: Dict[str, Any]):
        self.store = store
        self.tokens = tokens          # TurnstileTokenSource
        self.config = config
        self._sessions: Dict[str, SignupSession] = {}
        self._lock = threading.Lock()
        self._running: Optional[str] = None
        self._last: Optional[str] = None   # most recently finished session, for console replay

    # -- public ------------------------------------------------------------
    @property
    def mail_ready(self) -> bool:
        c = self.config
        return bool(c.get("mail_api_base") and c.get("mail_admin_passkey"))

    def active(self) -> Optional[SignupSession]:
        with self._lock:
            sid = self._running
        if not sid:
            return None
        s = self._sessions.get(sid)
        if s and s.state in ("queued", "running"):
            return s
        return None

    def last(self) -> Optional[SignupSession]:
        """Most recently finished session (kept so the console can show the
        final state + full log instead of blanking when the run ends)."""
        with self._lock:
            sid = self._last
        if not sid:
            return None
        return self._sessions.get(sid)

    def start(self, *, count: int = 1, name: Optional[str] = None,
              password: Optional[str] = None,
              mail: Optional[Dict[str, str]] = None) -> SignupSession:
        if count < 1 or count > 20:
            raise ValueError("count must be 1..20")
        cur = self.active()
        if cur:
            raise RuntimeError(f"a signup run is already active: {cur.sid}")

        cfg = dict(self.config)
        if mail:
            cfg.update({k: v for k, v in mail.items() if v})

        if not cfg.get("mail_api_base") or not cfg.get("mail_admin_passkey"):
            raise ValueError("邮箱服务未配置：需要 mail_api_base 与 mail_admin_passkey")
        if not cfg.get("mail_domain"):
            raise ValueError("邮箱域名未配置")

        sid = uuidlib.uuid4().hex[:12]
        sess = SignupSession(sid, count, {
            "mail_api_base": cfg["mail_api_base"],
            "mail_admin_passkey": cfg["mail_admin_passkey"],
            "mail_domain": cfg["mail_domain"],
            "name": name,
            "password": password,
            "delay": cfg.get("register_delay", [8, 20]),
            "code_timeout": cfg.get("code_timeout", 180),
            "min_credits": cfg.get("min_credits", 5),
        })
        with self._lock:
            self._sessions[sid] = sess
            self._running = sid
        sess._thread = threading.Thread(target=self._run, args=(sess,), daemon=True)
        sess._thread.start()
        return sess

    def get(self, sid: str) -> Optional[SignupSession]:
        return self._sessions.get(sid)

    def clear_finished(self) -> int:
        n = 0
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if s.state in ("done", "cancelled", "error"):
                    self._sessions.pop(sid)
                    n += 1
            if self._last is not None and self._last not in self._sessions:
                self._last = None
        return n

    # -- worker ------------------------------------------------------------
    def _run(self, sess: SignupSession) -> None:
        sess.state = "running"
        sess.started_at = time.time()
        sess.log(f"开始注册 {sess.count} 个账号（有头浏览器 CDP 取 Turnstile token）")

        try:
            for i in range(1, sess.count + 1):
                if sess._cancel.is_set():
                    sess.state = "cancelled"
                    sess.log("已取消", "warn")
                    break
                sess.current = f"account {i}/{sess.count}"
                ok = self._one(sess, i)
                if ok:
                    sess.created += 1
                else:
                    sess.failed += 1
                if i < sess.count and not sess._cancel.is_set():
                    lo, hi = sess.cfg["delay"]
                    d = random.uniform(lo, hi)
                    sess.log(f"间隔 {d:.0f}s 后注册下一个")
                    self._sleep(sess, d)

            if sess.state == "running":
                sess.state = "done"
        except Exception as e:  # noqa: BLE001
            sess.state = "error"
            sess.error = str(e)
            sess.log(f"运行异常：{e}", "bad")
        finally:
            sess.current = None
            sess.finished_at = time.time()
            sess.log(f"结束：成功 {sess.created}，失败 {sess.failed}", "ok" if sess.created else "warn")
            with self._lock:
                if self._running == sess.sid:
                    self._running = None
                self._last = sess.sid

    def _sleep(self, sess: SignupSession, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if sess._cancel.is_set():
                return
            time.sleep(0.4)

    def _one(self, sess: SignupSession, idx: int) -> bool:
        cfg = sess.cfg
        mb = None
        try:
            prefix = rand_prefix(cfg.get("name"))
            mb = mailmod.TempMailbox.create(
                cfg["mail_api_base"], cfg["mail_admin_passkey"],
                cfg["mail_domain"], name=prefix)
            sess.log(f"[{idx}] 临时邮箱 {mb.address} (id={mb.address_id})")

            sess.log(f"[{idx}] 渲染 Turnstile token…")
            try:
                captcha = self.tokens.get_token()
            except Exception as e:  # noqa: BLE001
                sess.log(f"[{idx}] ✗ Turnstile 取 token 失败：{e}", "bad")
                if "circuit breaker" in str(e):
                    sess.log("熔断中：等待冷却或更换出口 IP 后重试", "warn")
                return False

            reg = regmod.Hyper3DRegister()
            email = mb.address
            password = cfg.get("password") or regmod.random_password()
            username = regmod.random_username()
            try:
                reg.send_code(email, captcha, "Register")
                sess.log(f"[{idx}] 验证码邮件已发出，等待收信…")

                code = mb.wait_for_code(sender_contains="hyper3d",
                                        timeout=cfg["code_timeout"], interval=4)
                if not code:
                    code = mb.wait_for_code(timeout=60, interval=4)
                if not code:
                    sess.log(f"[{idx}] ✗ 未收到验证码（邮箱可能被上游拒绝或收信超时）", "bad")
                    return False
                sess.log(f"[{idx}] 收到验证码 {code}")

                out = reg.register(email=email, username=username,
                                   password=password, code=code)
                token = out.get("token")
                if not token:
                    sess.log(f"[{idx}] ✗ register 未返回 token", "bad")
                    return False

                acct = {
                    "email": email,
                    "username": out.get("username") or username,
                    "password": password,
                    "user_uuid": out.get("user_uuid"),
                    "token": token,
                    "mailbox_id": mb.address_id,
                    "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "status": "active",
                }
                try:
                    from hyper3d import Hyper3DClient
                    client = Hyper3DClient(token)
                    try:
                        meta = client.balance()
                        acct["balance"] = meta.get("balance")
                        acct["plan"] = (meta.get("subscriptions") or {}).get("highest_level")
                        acct["trial_eligibility"] = (meta.get("subscriptions") or {}).get("trial_eligibility")
                    finally:
                        client.close()
                except Exception as e:  # noqa: BLE001
                    sess.log(f"[{idx}] (余额查询失败：{e})", "warn")

                self.store.upsert(acct)
                sess.results.append({k: acct.get(k) for k in
                                     ("email", "username", "password", "user_uuid",
                                      "balance", "plan", "status")})
                sess.log(f"[{idx}] ✓ 入库 {acct['email']} "
                         f"balance={acct.get('balance')} plan={acct.get('plan')}", "ok")
                return True
            except Exception as e:  # noqa: BLE001
                code_attr = getattr(e, "code", "")
                sess.log(f"[{idx}] ✗ 注册失败{(' [' + code_attr + ']') if code_attr else ''}：{e}", "bad")
                return False
            finally:
                reg.close()
        except Exception as e:  # noqa: BLE001
            sess.log(f"[{idx}] ✗ 邮箱创建失败：{e}", "bad")
            return False
        finally:
            if mb:
                mb.close()
