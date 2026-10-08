"""Hyper3D account registration client.

Flow (confirmed from bundle + live probes, 2026-10-08):
  1) POST /api/auth/send_email_verification_code  {email, type:"Register"}
       headers: X-Hyper3D-Captcha: turnstile:<token>          (Turnstile REQUIRED)
  2) read the 6-digit code from the mailbox
  3) POST /api/auth/register  {username, email, password, email_verification_code,
                               referral_code?, marketing_email_consent?}
       no captcha required
       -> {token, user_uuid}
  4) result token goes into localStorage.token on the web app; usable as
       Authorization: Bearer <token> for /api/gateway/*
"""
from __future__ import annotations

import random
import string
import time
from typing import Any, Dict, Optional

import httpx


API_BASE = "https://api.hyper3d.com/api"


class RegisterError(RuntimeError):
    def __init__(self, message: str, *, code: str = "register_error",
                 http_status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.body = body


def random_username(n: int = 12) -> str:
    # <= 24 chars, server-side rule
    n = max(6, min(24, n))
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def random_password(n: int = 14) -> str:
    # >= 6 chars, server-side rule
    n = max(8, n)
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=n))


class Hyper3DRegister:
    def __init__(self, timeout: float = 40.0,
                 base: str = API_BASE,
                 user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/154.0.0.0 Safari/537.36"):
        self.base = base.rstrip("/")
        self._http = httpx.Client(timeout=timeout, follow_redirects=True,
                                  headers={"User-Agent": user_agent,
                                           "Origin": "https://hyper3d.ai",
                                           "Referer": "https://hyper3d.ai/workspace/register"})
        self.last_request: Optional[Dict[str, Any]] = None

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- step 1: send verification code ------------------------------------
    def send_code(self, email: str, captcha: str,
                  type_: str = "Register") -> Dict[str, Any]:
        if not captcha:
            raise RegisterError("captcha token required",
                                code="captcha_missing")
        headers = {
            "Content-Type": "application/json",
            "X-Hyper3D-Captcha": f"turnstile:{captcha}",
        }
        payload = {"email": email, "type": type_}
        r = self._http.post(f"{self.base}/auth/send_email_verification_code",
                            headers=headers, json=payload)
        self.last_request = {"url": r.request.url.__str__(), "method": "POST",
                            "headers": dict(r.request.headers), "body": payload}
        if r.status_code == 400:
            d = self._safe_json(r)
            code = d.get("code") or "bad_request"
            msg = d.get("message") or r.text
            if isinstance(msg, list):
                msg = "; ".join(map(str, msg))
            if code == "CAPTCHA_MISSING":
                raise RegisterError(msg, code="captcha_missing", http_status=400, body=d)
            if code == "CAPTCHA_FAILED":
                raise RegisterError(msg, code="captcha_failed", http_status=400, body=d)
            raise RegisterError(str(msg), code="bad_request", http_status=400, body=d)
        if r.status_code not in (200, 201):
            raise RegisterError(f"HTTP {r.status_code}: {r.text[:200]}",
                                code="http_error", http_status=r.status_code, body=r.text)
        return {"status": r.status_code, "body": self._safe_json(r)}

    # -- step 3: register --------------------------------------------------
    def register(self, *, email: str, username: str, password: str,
                 code: str, referral_code: Optional[str] = None,
                 marketing_email_consent: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "username": username,
            "email": email,
            "password": password,
            "email_verification_code": code,
        }
        if referral_code:
            payload["referral_code"] = referral_code
        payload["marketing_email_consent"] = marketing_email_consent

        r = self._http.post(f"{self.base}/auth/register",
                            headers={"Content-Type": "application/json"},
                            json=payload)
        self.last_request = {"url": r.request.url.__str__(), "method": "POST",
                            "headers": dict(r.request.headers), "body": payload}
        # success: {token, user_uuid}; failure: {"error": "CODE"}
        d = self._safe_json(r)
        if isinstance(d, dict) and d.get("token"):
            return d
        err = (d or {}).get("error") or (d or {}).get("message") or r.text
        mapping = {
            "INVALID_EMAIL_VERIFICATION_CODE": "bad_code",
            "USER_ALREADY_EXISTS": "duplicate",
            "EMAIL_ALREADY_EXISTS": "duplicate",
        }
        raise RegisterError(str(err), code=mapping.get(str(err), "register_failed"),
                            http_status=r.status_code, body=d)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _safe_json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:400]}

    # -- high level --------------------------------------------------------
    def register_account(self, *, email: str, get_captcha, get_code,
                         username: Optional[str] = None,
                         password: Optional[str] = None,
                         referral_code: Optional[str] = None,
                         code_timeout: float = 180.0) -> Dict[str, Any]:
        """Run the whole flow for one account.

        get_captcha: () -> str    supplies a fresh Turnstile token
        get_code:    (since_ts) -> Optional[str]   returns the emailed code
        """
        username = username or random_username()
        password = password or random_password()
        t0 = time.time()

        captcha = get_captcha()
        self.send_code(email, captcha, type_="Register")

        code = get_code(t0)
        if not code:
            raise RegisterError("verification code not received",
                                code="code_timeout")

        d = self.register(email=email, username=username, password=password,
                          code=code, referral_code=referral_code)
        return {
            "email": email,
            "username": username,
            "password": password,
            "user_uuid": d.get("user_uuid"),
            "token": d.get("token"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }