"""Hyper3D API client (`https://api.hyper3d.com/api`).

Everything here was confirmed by live traffic capture on 2026-10-08.

Required headers on /api/gateway/* generation calls:
    Authorization: Bearer <localStorage.token>
    X-Hyper3D-Captcha: turnstile:<one-shot token>
    Ai-trace-id: trace-<ms>-<rand36[9]>
    Content-Type: application/json     (JSON mode only)

Endpoints
    POST /api/gateway/generate_from_text   JSON  — text_to_video, text_to_image
    POST /api/gateway/generate_from_media  FormData — image_to_video, image_to_image, ...
    GET  /api/gateway/result/<uuid>       poll  — Queued/Running/Succeeded/Failed
    POST /api/user/get_info               balance (body `{}`, result under `meta`)
    GET  /api/init                         captcha config (no auth)
"""
from __future__ import annotations

import json
import random
import string
import time
from typing import Any, Dict, Optional

import httpx


API_BASE = "https://api.hyper3d.com/api"
GATEWAY = f"{API_BASE}/gateway"


class Hyper3DError(RuntimeError):
    def __init__(self, message: str, *, code: str = "upstream_error",
                 http_status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.body = body


def _trace_id() -> str:
    """Mirror the front-end generator exactly."""
    rand = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(9))
    return f"trace-{int(time.time() * 1000)}-{rand}"


class Hyper3DClient:
    def __init__(self, token: str, timeout: float = 60.0, user_agent: str = "hyper3d2api/1.0"):
        if not token:
            raise ValueError("token is required")
        self.token = token
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- headers -----------------------------------------------------------
    def _base_headers(self, *, json_body: bool, trace: Optional[str] = None) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Ai-trace-id": trace or _trace_id(),
            "Origin": "https://hyper3d.ai",
            "Referer": "https://hyper3d.ai/workspace/video",
            "Accept": "application/json",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    # -- balance -----------------------------------------------------------
    def balance(self) -> Dict[str, Any]:
        """POST /api/user/get_info -> meta (note: POST, and wrapped in `meta`)."""
        r = self._client.post(
            f"{API_BASE}/user/get_info",
            headers=self._base_headers(json_body=True),
            content=b"{}",
        )
        r.raise_for_status()
        return r.json().get("meta", {})

    def credits(self) -> Optional[float]:
        b = self.balance().get("balance")
        return float(b) if isinstance(b, (int, float)) else None

    # -- generation --------------------------------------------------------
    def _submit(self, path: str, *, json_body: bool, captcha: str,
                content: Any, files: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = self._base_headers(json_body=json_body)
        if not captcha:
            raise Hyper3DError("captcha token missing", code="captcha_missing")
        headers["X-Hyper3D-Captcha"] = f"turnstile:{captcha}"

        if files:
            r = self._client.post(f"{GATEWAY}{path}", headers=headers,
                                  data=content, files=files)
        else:
            r = self._client.post(f"{GATEWAY}{path}", headers=headers, content=content)
        text = r.text

        if r.status_code == 400:
            try:
                data = json.loads(text)
            except Exception:
                data = {}
            code = data.get("code") or "bad_request"
            msg = data.get("message") or text
            if isinstance(msg, list):
                msg = "; ".join(map(str, msg))
            if code == "CAPTCHA_MISSING":
                raise Hyper3DError(msg, code="captcha_missing", http_status=400, body=data)
            if code == "CAPTCHA_FAILED":
                raise Hyper3DError(msg, code="captcha_failed", http_status=400, body=data)
            raise Hyper3DError(str(msg), code="bad_request", http_status=400, body=data)

        if r.status_code not in (200, 201):
            raise Hyper3DError(f"HTTP {r.status_code}: {text[:300]}",
                               code="http_error", http_status=r.status_code, body=text)

        # 201 with an empty object means the captcha was stale/replayed: the job
        # is silently dropped and no credits are consumed.  Treat as failure.
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not data.get("uuid"):
            err = data.get("error")
            mapping = {
                "INVALID_API": ("invalid_api", "provider is valid but this api+model combination does not exist"),
                "PARAMETER_VALIDATION_FAILED": ("param_invalid", "provider+api valid but model/param rejected"),
                "NEED_ADVANCED_SUBSCRIPTION": ("paywall", "model requires a higher subscription (server-side check)"),
                "INSUFFICIENT_UNFROZEN_BALANCE": ("insufficient_credits",
                                                 "account balance is too low for this generation (top up credits)"),
                "SENSITIVE_CONTENT": ("safety", "prompt/image rejected by safety review"),
                "INVALID_INPUT": ("bad_request", "upstream rejected the input"),
                "INTERNAL_PROCESSING_FAILED": ("upstream_error", "upstream generation failure"),
            }
            if err in mapping:
                code, human = mapping[err]
                raise Hyper3DError(f"{err}: {human}", code=code, http_status=r.status_code, body=data)
            raise Hyper3DError(
                f"empty submit response (captcha likely stale; no credits consumed): {text[:200]!r}",
                code="empty_response", http_status=r.status_code, body=data,
            )
        return data

    def generate_text(self, *, api: str, provider: str, params: Dict[str, Any],
                      captcha: str) -> Dict[str, Any]:
        payload = {"api": api, "provider": provider, "params": params}
        return self._submit("/generate_from_text", json_body=True, captcha=captcha,
                            content=json.dumps(payload).encode())

    def generate_media(self, *, api: str, provider: str, params: Dict[str, Any],
                       files: Dict[str, Any], captcha: str) -> Dict[str, Any]:
        # FormData: provider + api + params(JSON string) + repeated file fields.
        data = {"provider": provider, "api": api, "params": json.dumps(params)}
        return self._submit("/generate_from_media", json_body=False, captcha=captcha,
                            content=data, files=files)

    # -- polling -----------------------------------------------------------
    def result(self, uuid: str) -> Dict[str, Any]:
        r = self._client.get(f"{GATEWAY}/result/{uuid}",
                             headers=self._base_headers(json_body=False))
        r.raise_for_status()
        return r.json()

    def poll(self, uuid: str, *, interval: float = 6.0, timeout: float = 900.0,
             on_update=None) -> Dict[str, Any]:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                d = self.result(uuid)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(interval)
                continue
            st = d.get("status") or d.get("error")
            if st != last:
                last = st
                if on_update:
                    on_update(st, d)
            if st in ("Succeeded", "Failed", "PaymentFailed"):
                return d
            if d.get("error") and not d.get("status"):
                return d
            time.sleep(interval)
        raise TimeoutError(f"poll timed out after {timeout}s (last status: {last})")