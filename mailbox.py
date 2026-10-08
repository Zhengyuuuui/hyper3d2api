"""Mailbox adapter for the Cloudflare temp-email worker (yunex).

Backend API contract (verified 2026-10-08 against
https://yunex-temp-email.zysgm123.workers.dev):

  Admin (header x-admin-auth: <admin passkey>)
    POST /admin/new_address   {enablePrefix, name, domain}
                              -> {jwt, address, address_id}
    GET  /admin/address?limit&offset
    GET  /admin/mails?limit&offset&address=<addr>   (admin read)

  Address user (header Authorization: Bearer <address jwt>)
    GET  /api/mails?limit&offset          -> {results:[...], count}
    GET  /api/mail/<id>
    GET  /api/settings

The per-address JWT is obtained from /admin/new_address and never expires
in practice for our use (it carries address + address_id).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

import httpx


class MailError(RuntimeError):
    pass


class TempMailbox:
    """One disposable address + helpers to read its inbox."""

    def __init__(self, api_base: str, admin_passkey: str, address: str, jwt: str,
                 domain: str = "", name: str = "", address_id: Optional[int] = None,
                 timeout: float = 30.0):
        self.api_base = api_base.rstrip("/")
        self.admin_passkey = admin_passkey
        self.address = address
        self.jwt = jwt
        self.domain = domain
        self.name = name
        self.address_id = address_id
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- admin -------------------------------------------------------------
    @classmethod
    def create(cls, api_base: str, admin_passkey: str, domain: str = "",
               name: Optional[str] = None, prefix: bool = True) -> "TempMailbox":
        # `name` is REQUIRED by the server; `domain` is optional (server default).
        if not name:
            raise MailError("new_address requires a `name`")
        payload: Dict[str, Any] = {"name": name, "enablePrefix": prefix}
        if domain:
            payload["domain"] = domain
        r = httpx.post(f"{api_base.rstrip('/')}/admin/new_address",
                       headers={"x-admin-auth": admin_passkey,
                                "Content-Type": "application/json"},
                       json=payload, timeout=30)
        if r.status_code != 200:
            raise MailError(f"new_address failed {r.status_code}: {r.text[:200]}")
        d = r.json()
        return cls(api_base, admin_passkey, d["address"], d["jwt"],
                   domain=domain, name=name, address_id=d.get("address_id"))

    def delete(self) -> bool:
        if self.address_id is None:
            return False
        r = self._http.delete(f"{self.api_base}/admin/delete_address/{self.address_id}",
                              headers={"x-admin-auth": self.admin_passkey})
        return r.status_code in (200, 204)

    # -- inbox -------------------------------------------------------------
    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt}"}

    def list_mails(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        r = self._http.get(f"{self.api_base}/api/mails",
                           headers=self._auth_headers(),
                           params={"limit": limit, "offset": offset})
        if r.status_code != 200:
            raise MailError(f"list_mails failed {r.status_code}: {r.text[:200]}")
        d = r.json()
        return d.get("results", []) if isinstance(d, dict) else []

    def get_mail(self, mail_id: Any) -> Dict[str, Any]:
        r = self._http.get(f"{self.api_base}/api/mail/{mail_id}",
                           headers=self._auth_headers())
        r.raise_for_status()
        return r.json()

    # -- code extraction ---------------------------------------------------
    # Hyper3D's verification mail puts the code in an HTML <b> tag:
    #   Verification Code: <b>626651</b>
    # NOTE: a bogus "000000" also appears inside an awstrack.me tracking URL,
    # so a plain \d{6} regex is WRONG. Prefer the <b>-wrapped / labelled form.
    CODE_PATTERNS = [
        r"Verification\s*Code[^0-9]{0,40}(\d{6})",   # labelled
        r"<b>\s*(\d{6})\s*</b>",                      # bolded code
        r"code[^0-9]{0,20}(\d{6})",                   # generic label
    ]

    @staticmethod
    def _html_to_text(html: str) -> str:
        import re as _re
        t = _re.sub(r"<[^>]+>", " ", html)
        t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">"))
        return t

    def _extract_code(self, mail: Dict[str, Any]) -> Optional[str]:
        """Pull a 6-digit code out of a mail dict (raw/text/html)."""
        import email as _email
        candidates: List[str] = []

        raw = mail.get("raw")
        if raw:
            try:
                msg = _email.message_from_string(str(raw))
                for part in msg.walk():
                    if part.get_content_type().startswith("text"):
                        payload = part.get_payload(decode=True)
                        if payload:
                            candidates.append(
                                payload.decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                candidates.append(str(raw))
        for k in ("text", "content", "message", "html"):
            if mail.get(k):
                candidates.append(str(mail[k]))

        for blob in candidates:
            search_space = [blob]
            if "<" in blob:
                search_space.append(self._html_to_text(blob))
            for text in search_space:
                for pat in self.CODE_PATTERNS:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        return m.group(1)
        return None

    def wait_for_code(self, *, sender_contains: Optional[str] = None,
                      subject_contains: Optional[str] = None,
                      timeout: float = 180.0, interval: float = 5.0,
                      since_ts: Optional[float] = None) -> Optional[str]:
        """Poll the inbox until a verification code shows up.

        The list endpoint returns summaries only, so each new mail is fetched
        in full via /api/mail/<id> before extraction.
        """
        deadline = time.time() + timeout
        seen_ids: set = set()
        while time.time() < deadline:
            try:
                mails = self.list_mails(limit=25)
            except Exception:
                time.sleep(interval)
                continue

            for m in mails:
                mid = m.get("id")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)

                sender = str(m.get("source") or m.get("from") or "")
                subject = str(m.get("subject") or "")
                if sender_contains and sender_contains.lower() not in sender.lower():
                    continue
                if subject_contains and subject_contains.lower() not in subject.lower():
                    continue

                full = m
                if mid is not None:
                    try:
                        full = self.get_mail(mid)
                    except Exception:
                        pass
                code = self._extract_code(full) or self._extract_code(m)
                if code:
                    return code

            time.sleep(interval)
        return None