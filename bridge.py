"""hyper3d2api — OpenAI-compatible bridge for Hyper3D (Rodin) media generation.

Verified working models on a Free account (2026-10-08):
    pixverse         video  (v5 / v5.5 / v5.6 / v6 / c1)
    seedream-4.5     image  (seedream_4_5)
    seedream-5.0-lite image (seedream_5_0_lite)

Endpoints
    GET  /v1/models
    POST /v1/videos        text->video   (async, returns job)
    POST /v1/images        text->image   (async, returns job)
    GET  /v1/jobs/<uuid>   poll
    GET  /v1/balance
    GET  /console          web console

Run:  python3 bridge.py --port 4870
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import uuid as uuidlib
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

import registry as R
from hyper3d import Hyper3DClient, Hyper3DError
from store import AccountStore
from turnstile import TurnstileTokenSource

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DB_PATH = os.path.join(HERE, "accounts.db")
DEFAULT_CONFIG = {
    "api_token": "sk-hyper3d-local",
    "token": "",                 # Hyper3D JWT (localStorage.token)
    "cdp_port": 9366,
    "site_key": "0x4AAAAAADTv8yKBtK5-Zt1E",
    "port": 4870,
    "poll_interval": 6.0,
    "poll_timeout": 900.0,
    "wait_inline": True,
    "human_pacing": True,
    "min_credits": 5,   # refuse to burn a Turnstile token below this balance
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


CONFIG = load_config()

app = FastAPI(title="hyper3d2api", version="1.0.0")

_tokens = TurnstileTokenSource(port=CONFIG["cdp_port"], site_key=CONFIG["site_key"],
                               human_pacing=CONFIG["human_pacing"])
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

# account pool
_STORE: Optional[AccountStore] = None
_STORE_LOCK = threading.Lock()


def account_store() -> AccountStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = AccountStore(DB_PATH)
    return _STORE


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def check_auth(authorization: Optional[str] = Header(default=None)) -> None:
    expected = CONFIG["api_token"]
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"error": {
            "message": "missing bearer token", "type": "invalid_request_error"}})
    got = authorization.split(" ", 1)[1].strip()
    if got != expected:
        raise HTTPException(status_code=401, detail={"error": {
            "message": "invalid api key", "type": "invalid_request_error",
            "code": "invalid_api_key"}})


def need_upstream() -> Hyper3DClient:
    if not CONFIG.get("token"):
        raise HTTPException(status_code=503, detail={"error": {
            "message": "upstream token not configured (set `token` in config.json)",
            "type": "service_unavailable"}})
    return Hyper3DClient(CONFIG["token"])


def err(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {
        "message": message, "type": "hyper3d_error", "code": code}})


def map_upstream(e: Exception) -> HTTPException:
    if isinstance(e, Hyper3DError):
        status = {
            "captcha_missing": 502, "captcha_failed": 502, "empty_response": 502,
            "invalid_api": 400, "param_invalid": 400, "paywall": 402,
        }.get(e.code, 502)
        return err(e.code, str(e), status)
    return err("bridge_error", str(e), 500)


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
def _store(job: Dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job["id"]] = job


def _update(job_id: str, **kw) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


def _run_job(job_id: str, plan: Dict[str, Any]) -> None:
    """Execute one generation, polling to completion in the background."""
    _update(job_id, status="running")
    client = Hyper3DClient(CONFIG["token"])
    try:
        # Pre-flight: don't burn a one-shot Turnstile token if we're broke.
        # Observed floor is ~5 credits for image models; video similar or higher.
        if CONFIG.get("min_credits", 5) > 0:
            try:
                bal = client.credits()
            except Exception:
                bal = None
            if bal is not None and bal < CONFIG["min_credits"]:
                _update(job_id, status="failed",
                        error=f"insufficient credits: balance {bal} < required ~{CONFIG['min_credits']}",
                        error_code="insufficient_credits", balance=bal)
                return
            _update(job_id, balance=bal)

        token = _tokens.get_token()
        _update(job_id, status="submitted", captcha_ok=True)
        resp = client.generate_text(api=plan["api"], provider=plan["provider"],
                                    params=plan["params"], captcha=token)
        upstream = resp.get("uuid")
        _update(job_id, upstream_uuid=upstream, upstream_status=resp.get("status"))
        final = client.poll(upstream, interval=CONFIG["poll_interval"],
                            timeout=CONFIG["poll_timeout"])
        item = final.get("item") or {}
        try:
            after = client.credits()
        except Exception:
            after = None
        _update(
            job_id,
            status="succeeded" if final.get("status") == "Succeeded" else "failed",
            upstream_status=final.get("status"),
            output=item.get("output"),
            provider=item.get("provider"),
            item_api=item.get("api"),
            item_params=item.get("params"),
            error=final.get("error") or final.get("error_msg"),
            balance=after,
            balance_before=plan.get("_balance_before"),
        )
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", "bridge_error")
        _update(job_id, status="failed", error=str(e), error_code=code)
    finally:
        client.close()


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.get("/v1/models")
def list_models(authorization: Optional[str] = Depends(check_auth)):
    return R.list_models()


@app.get("/v1/blocked-models")
def blocked_models(authorization: Optional[str] = Depends(check_auth)):
    """Why the other models are unavailable (so clients can show a reason)."""
    return {"object": "list",
            "data": [{"id": k, "available": False, **v} for k, v in R.BLOCKED.items()]}


@app.get("/v1/balance")
def balance(authorization: Optional[str] = Depends(check_auth)):
    try:
        with need_upstream() as c:
            meta = c.balance()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise map_upstream(e)
    return {
        "balance": meta.get("balance"),
        "frozen": meta.get("frozen"),
        "plan": (meta.get("subscriptions") or {}).get("highest_level"),
        "quota_reset_remain": (meta.get("subscriptions") or {}).get("quota_reset_remain"),
    }


def _submit_plan(body: Dict[str, Any]) -> Dict[str, Any]:
    model_name = body.get("model")
    key, spec = R.resolve(model_name)
    if not spec:
        blocked = R.blocked_reason(model_name)
        if blocked:
            raise err("model_unavailable",
                      f"model {model_name!r} is not available: {blocked['reason']} "
                      f"[{blocked['class']}]", 400)
        raise err("invalid_model",
                  f"unknown model {model_name!r}. See /v1/models for the working list.", 400)

    model = R.pick_model(model_name, spec)
    prompt = body.get("prompt") or body.get("input")
    if not prompt:
        raise err("invalid_request", "prompt is required", 400)

    params: Dict[str, Any] = dict(spec["defaults"])
    warnings: list[str] = []
    if spec["family"] == "video":
        params["prompt"] = prompt
        params["model"] = model
        for f in ("aspect_ratio", "resolution", "duration"):
            if body.get(f) is None:
                continue
            want = body[f]
            if f == "duration":
                allowed = spec.get("durations")
            elif f == "resolution":
                allowed = spec.get("resolutions")
            elif f == "aspect_ratio":
                allowed = spec.get("aspect_ratios")
            if allowed and want not in allowed:
                fallback = spec["defaults"][f]
                warnings.append(f"{f}={want!r} not supported by {key}; using {fallback!r}")
                params[f] = fallback
            else:
                params[f] = want
        if body.get("audio") is not None:
            params["audio"] = bool(body["audio"])
    else:
        params["prompt"] = prompt
        params["model"] = model
        size = body.get("size") or body.get("resolution")
        if size:
            if size in spec["sizes"]:
                params["size"] = size
            else:
                fallback = spec["defaults"]["size"]
                warnings.append(f"size={size!r} not supported by {key}; using {fallback!r}")
                params["size"] = fallback

    return {"family": spec["family"], "api": spec["api"],
            "provider": spec["provider"], "params": params, "model_key": key,
            "warnings": warnings}


@app.post("/v1/videos")
def create_video(body: Dict[str, Any], authorization: Optional[str] = Depends(check_auth)):
    if not CONFIG.get("token"):
        raise err("no_upstream_token", "set `token` in config.json", 503)
    plan = _submit_plan(body)
    if plan["family"] != "video":
        raise err("wrong_family", "use /v1/images for image models", 400)

    job_id = f"job_{uuidlib.uuid4().hex[:24]}"
    _store({"id": job_id, "object": "video.job", "model": body.get("model"),
            "status": "queued", "created": int(time.time()), "plan": plan,
            "kind": "video", "warnings": plan.get("warnings") or []})
    th = threading.Thread(target=_run_job, args=(job_id, plan), daemon=True)
    th.start()

    if CONFIG.get("wait_inline"):
        deadline = time.time() + CONFIG["poll_timeout"]
        while time.time() < deadline:
            with _jobs_lock:
                j = dict(_jobs[job_id])
            if j["status"] in ("succeeded", "failed"):
                return _public_job(j)
            time.sleep(1.0)
    return _public_job({"id": job_id, "status": "queued", "model": body.get("model")})


@app.post("/v1/images")
def create_image(body: Dict[str, Any], authorization: Optional[str] = Depends(check_auth)):
    if not CONFIG.get("token"):
        raise err("no_upstream_token", "set `token` in config.json", 503)
    plan = _submit_plan(body)
    if plan["family"] != "image":
        raise err("wrong_family", "use /v1/videos for video models", 400)

    job_id = f"job_{uuidlib.uuid4().hex[:24]}"
    _store({"id": job_id, "object": "image.job", "model": body.get("model"),
            "status": "queued", "created": int(time.time()), "plan": plan,
            "kind": "image", "warnings": plan.get("warnings") or []})
    threading.Thread(target=_run_job, args=(job_id, plan), daemon=True).start()

    if CONFIG.get("wait_inline"):
        deadline = time.time() + CONFIG["poll_timeout"]
        while time.time() < deadline:
            with _jobs_lock:
                j = dict(_jobs[job_id])
            if j["status"] in ("succeeded", "failed"):
                return _public_job(j)
            time.sleep(1.0)
    return _public_job({"id": job_id, "status": "queued", "model": body.get("model")})


def _public_job(j: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "id": j.get("id"),
        "object": j.get("object", "job"),
        "model": j.get("model"),
        "status": j.get("status"),
        "created": j.get("created"),
        "upstream_uuid": j.get("upstream_uuid"),
        "upstream_status": j.get("upstream_status"),
        "provider": j.get("provider"),
        "output_url": j.get("output"),
        "error": j.get("error"),
    }
    if j.get("item_params"):
        out["upstream_params"] = j["item_params"]
    if j.get("error_code"):
        out["error_code"] = j["error_code"]
    if j.get("balance") is not None:
        out["balance_after"] = j["balance"]
    if j.get("warnings"):
        out["warnings"] = j["warnings"]
    return {k: v for k, v in out.items() if v is not None}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, authorization: Optional[str] = Depends(check_auth)):
    with _jobs_lock:
        j = _jobs.get(job_id)
    if not j:
        raise err("not_found", f"job {job_id} not found", 404)
    return _public_job(j)


@app.get("/health")
def health():
    try:
        accounts = account_store().stats()
    except Exception:
        accounts = None
    return {
        "ok": True,
        "upstream_token_configured": bool(CONFIG.get("token")),
        "turnstile_connected": _tokens.healthy(),
        "models": [m["id"] for m in R.list_models()["data"]],
        "accounts": accounts,
    }


# --------------------------------------------------------------------------
# admin API (used by console.html)
# --------------------------------------------------------------------------
@app.get("/admin/api/overview")
def admin_overview(authorization: Optional[str] = Depends(check_auth)):
    st = account_store()
    with _jobs_lock:
        jobs = list(_jobs.values())
    done = [j for j in jobs if j.get("status") in ("succeeded", "failed")]
    succeeded = [j for j in jobs if j.get("status") == "succeeded"]
    failed = [j for j in jobs if j.get("status") == "failed"]
    return {
        "accounts": st.stats(),
        "jobs": {"total": len(jobs), "succeeded": len(succeeded),
                 "failed": len(failed), "in_flight": len(jobs) - len(done)},
        "pool": {"usable": len(st.list(status="active", min_balance=CONFIG["min_credits"]))},
        "upstream": {
            "token_configured": bool(CONFIG.get("token")),
            "turnstile_connected": _tokens.healthy(),
            "cdp_port": CONFIG["cdp_port"],
            "min_credits": CONFIG["min_credits"],
        },
    }


@app.get("/admin/api/accounts")
def admin_accounts(authorization: Optional[str] = Depends(check_auth),
                   status: Optional[str] = None,
                   min_balance: Optional[float] = None):
    rows = account_store().list(status=status, min_balance=min_balance)
    # never expose raw tokens in listing
    for r in rows:
        r["token_preview"] = (r.get("token") or "")[:16] + "…" if r.get("token") else None
        r.pop("token", None)
    return rows


@app.get("/admin/api/accounts/{email}")
def admin_account(email: str, authorization: Optional[str] = Depends(check_auth)):
    r = account_store().get(email)
    if not r:
        raise err("not_found", f"account {email} not found", 404)
    r["token_preview"] = (r.get("token") or "")[:20] + "…"
    r.pop("token", None)
    return r


@app.get("/admin/api/accounts/{email}/token")
def admin_account_token(email: str, authorization: Optional[str] = Depends(check_auth)):
    r = account_store().get(email)
    if not r:
        raise err("not_found", f"account {email} not found", 404)
    return {"email": email, "token": r.get("token")}


@app.post("/admin/api/accounts/{email}/check")
def admin_account_check(email: str, authorization: Optional[str] = Depends(check_auth)):
    r = account_store().get(email)
    if not r:
        raise err("not_found", f"account {email} not found", 404)
    client = Hyper3DClient(r["token"], timeout=25)
    try:
        bal = client.credits()
        meta = client.balance()
        plan = (meta.get("subscriptions") or {}).get("highest_level")
        trial = (meta.get("subscriptions") or {}).get("trial_eligibility")
        status = "active"
        if bal is not None and bal < CONFIG["min_credits"]:
            status = "exhausted"
        account_store().set_balance(email, bal, plan, trial)
        account_store().mark_status(email, status)
        return {"email": email, "balance": bal, "plan": plan,
                "trial_eligibility": trial, "status": status}
    except Exception as e:  # noqa: BLE001
        account_store().mark_status(email, "invalid", str(e)[:200])
        raise err("upstream_error", str(e), 502)
    finally:
        client.close()


@app.post("/admin/api/accounts/{email}/status")
def admin_account_status(email: str, body: Dict[str, Any],
                         authorization: Optional[str] = Depends(check_auth)):
    if not account_store().get(email):
        raise err("not_found", f"account {email} not found", 404)
    status = body.get("status")
    if status not in ("active", "banned", "exhausted", "disabled"):
        raise err("bad_request", "status must be active|banned|exhausted|disabled", 400)
    account_store().mark_status(email, status, body.get("note"))
    return {"email": email, "status": status}


@app.delete("/admin/api/accounts/{email}")
def admin_account_delete(email: str, authorization: Optional[str] = Depends(check_auth)):
    st = account_store()
    if not st.get(email):
        raise err("not_found", f"account {email} not found", 404)
    st.delete(email)
    return {"deleted": email}


@app.get("/admin/api/jobs")
def admin_jobs(authorization: Optional[str] = Depends(check_auth),
               limit: int = 50):
    with _jobs_lock:
        jobs = list(_jobs.values())
    jobs.sort(key=lambda j: j.get("created") or 0, reverse=True)
    out = []
    for j in jobs[:limit]:
        p = _public_job(j)
        p["kind"] = j.get("kind")
        p["plan_model"] = (j.get("plan") or {}).get("params", {}).get("model")
        out.append(p)
    return out


@app.get("/admin/api/ledger")
def admin_ledger(authorization: Optional[str] = Depends(check_auth),
                 email: Optional[str] = None, limit: int = 100):
    return account_store().ledger(email=email, limit=limit)


@app.get("/")
@app.get("/console")
def console():
    path = os.path.join(HERE, "console.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"service": "hyper3d2api", "docs": "/docs"})


def main():
    ap = argparse.ArgumentParser(description="hyper3d2api bridge")
    ap.add_argument("--port", type=int, default=CONFIG.get("port", 4870))
    ap.add_argument("--cdp-port", type=int, default=CONFIG.get("cdp_port", 9366))
    args = ap.parse_args()
    CONFIG["cdp_port"] = args.cdp_port

    print(f"[hyper3d2api] connecting to Chrome CDP on :{args.cdp_port} ...")
    ok = _tokens.connect(reload=False)
    print(f"[hyper3d2api] CDP attached: {ok}")
    if not ok:
        print("[hyper3d2api] WARN: no hyper3d.ai tab found. Open it in that Chrome; "
              "token fetching will retry per request.")

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()