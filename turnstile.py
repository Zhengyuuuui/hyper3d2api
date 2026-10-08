"""Turnstile token supplier for Hyper3D.

The `/api/gateway/*` endpoints require:
    X-Hyper3D-Captcha: turnstile:<token>

The token MUST be rendered on the https://hyper3d.ai origin (Turnstile binds
the token to the sitekey's registered hostname). We therefore drive the
already-logged-in Chrome over CDP, render the widget invisibly-ish off to the
side, and grab the callback value.

Tokens are single-use and expire in ~5 minutes, so we fetch one per submission.
"""
from __future__ import annotations

import itertools
import json
import random
import threading
import time
import urllib.request
from typing import Optional

SITE_KEY = "0x4AAAAAADTv8yKBtK5-Zt1E"
PAGE_HINT = "hyper3d.ai"


class CDPError(RuntimeError):
    pass


class _Target:
    __slots__ = ("id", "url", "ws")

    def __init__(self, id_, url, ws):
        self.id, self.url, self.ws = id_, url, ws


class CDPClient:
    """Minimal websocket CDP client (no external deps beyond websocket-client)."""

    def __init__(self, port: int = 9366, timeout: int = 90):
        import websocket  # imported lazily so --help works without it

        self._ws_mod = websocket
        self.port = port
        self.timeout = timeout
        self._ids = itertools.count(1)
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/json/version", timeout=5) as r:
            self.browser = json.load(r)
        self._sock = None
        self._page: Optional[_Target] = None

    # -- connection --------------------------------------------------------
    def _targets(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=5) as r:
            return json.load(r)

    def attach(self, hint: str = PAGE_HINT, reload: bool = False):
        """Attach to an existing tab whose URL contains `hint`."""
        import websocket

        t = None
        for cand in self._targets():
            if cand.get("type") == "page" and hint in cand.get("url", ""):
                t = cand
                break
        if t is None:
            raise CDPError(f"no page containing {hint!r}; open it in the CDP Chrome first")
        self._sock = websocket.create_connection(
            t["webSocketDebuggerUrl"], timeout=self.timeout, max_size=64 * 1024 * 1024
        )
        self._page = _Target(t["id"], t["url"], t["webSocketDebuggerUrl"])
        self.send("Page.enable")
        self.send("Runtime.enable")
        if reload:
            self.navigate("https://hyper3d.ai/workspace/video?lang=zh")
            self.wait_ready()
        return self

    def navigate(self, url: str):
        self.send("Page.navigate", {"url": url})

    def close(self):
        try:
            if self._sock:
                self._sock.close()
        finally:
            self._sock = None

    # -- protocol ----------------------------------------------------------
    def send(self, method: str, params=None, timeout: Optional[float] = None):
        if not self._sock:
            raise CDPError("not attached")
        mid = next(self._ids)
        self._sock.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            msg = json.loads(self._sock.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expr: str, timeout: Optional[float] = None):
        r = self.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True,
             "userGesture": True},
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            raise CDPError(str(r["exceptionDetails"].get("exception", {}).get("description")))
        return r.get("result", {}).get("value")

    # -- helpers -----------------------------------------------------------
    def wait_ready(self, tries: int = 40):
        for _ in range(tries):
            try:
                if self.evaluate("document.readyState") in ("interactive", "complete"):
                    return True
            except CDPError:
                pass
            time.sleep(0.5)
        return False

    def token_from_localstorage(self, key: str = "token") -> Optional[str]:
        try:
            return self.evaluate(f"localStorage.getItem({json.dumps(key)})")
        except CDPError:
            return None

    def human_pause(self, lo: float = 0.8, hi: float = 2.2):
        """Small random delay so submissions don't look robotic."""
        time.sleep(random.uniform(lo, hi))

    def idle_mouse(self, moves: int = 3):
        """A few organic mouse moves inside the viewport."""
        try:
            vp = self.evaluate("[innerWidth, innerHeight]")
            if not vp:
                return
            w, h = vp
            for _ in range(random.randint(1, moves)):
                self.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": random.randint(60, max(61, w - 60)),
                    "y": random.randint(60, max(61, h - 60)),
                    "buttons": 0,
                })
                time.sleep(random.uniform(0.04, 0.2))
        except CDPError:
            pass


class TurnstileTokenSource:
    """Serialised, one-shot Turnstile tokens (each token used exactly once)."""

    def __init__(self, port: int = 9366, site_key: str = SITE_KEY,
                 human_pacing: bool = True):
        self.port = port
        self.site_key = site_key
        self.human_pacing = human_pacing
        self._lock = threading.Lock()
        self._client: Optional[CDPClient] = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self, reload: bool = True) -> bool:
        with self._lock:
            try:
                c = CDPClient(port=self.port).attach(reload=reload)
                self._client = c
                return True
            except Exception:
                self._client = None
                return False

    def _ensure(self) -> CDPClient:
        if self._client is None:
            c = CDPClient(port=self.port).attach(reload=False)
            if c.token_from_localstorage() is None:
                c.navigate("https://hyper3d.ai/workspace/video?lang=zh")
                c.wait_ready()
            self._client = c
        return self._client

    def healthy(self) -> bool:
        with self._lock:
            if self._client is None:
                return False
            try:
                return self._client.token_from_localstorage() is not None
            except Exception:
                self._client = None
                return False

    # -- token -------------------------------------------------------------
    _RENDER_JS = """
    (async () => {
      window.__tsToken = null; window.__tsDone = false; window.__tsErr = null;
      const old = document.getElementById('__h3d_ts_host');
      if (old) old.remove();
      if (!window.turnstile) {
        await new Promise((res, rej) => {
          const sc = document.createElement('script');
          sc.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
          sc.async = true; sc.defer = true;
          sc.onload = res; sc.onerror = () => rej(new Error('turnstile script failed'));
          document.head.appendChild(sc);
        });
      }
      const host = document.createElement('div');
      host.id = '__h3d_ts_host';
      host.style.cssText = 'position:fixed;top:70px;left:70px;z-index:2147483000;'
                         + 'background:#fff;padding:10px;width:300px';
      document.body.appendChild(host);
      window.turnstile.render(host, {
        sitekey: __SITEKEY__,
        appearance: 'always', retry: 'never', refreshExpired: 'manual',
        size: 'flexible', theme: 'light',
        callback: t => { window.__tsToken = t; window.__tsDone = true; },
        'error-callback': e => { window.__tsErr = String(e); window.__tsDone = true; }
      });
      return 'rendered';
    })()
    """

    def get_token(self, attempts: int = 3) -> str:
        with self._lock:
            last = None
            for attempt in range(attempts):
                try:
                    c = self._ensure()
                    if self.human_pacing:
                        c.idle_mouse()
                        c.human_pause(1.0, 2.5)
                    c.evaluate(self._RENDER_JS.replace("__SITEKEY__", json.dumps(self.site_key)), timeout=60)
                    for _ in range(25):
                        c.human_pause(0.6, 1.4)
                        st = c.evaluate("({d: window.__tsDone, t: !!window.__tsToken, e: window.__tsErr})")
                        if st and st.get("d"):
                            if st.get("e"):
                                raise CDPError("turnstile: " + str(st["e"]))
                            tok = c.evaluate("window.__tsToken")
                            try:
                                c.evaluate("(()=>{const e=document.getElementById('__h3d_ts_host');"
                                           "if(e)e.remove();return 1;})()")
                            except CDPError:
                                pass
                            if not tok:
                                raise CDPError("empty token")
                            return tok
                    raise TimeoutError("turnstile watchdog")
                except Exception as e:  # noqa: BLE001
                    last = e
                    try:
                        if self._client:
                            self._client.navigate("https://hyper3d.ai/workspace/video?lang=zh")
                            self._client.wait_ready()
                    except Exception:
                        pass
                    time.sleep(random.uniform(4, 8))
            raise CDPError(f"could not obtain Turnstile token after {attempts} attempts: {last}")