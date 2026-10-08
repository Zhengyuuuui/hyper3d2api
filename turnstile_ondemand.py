"""Plan A — on-demand headful Chrome for Turnstile tokens.

Self-contained: it LAUNCHES its own Chrome (fresh profile, remote-debugging),
navigates to hyper3d.ai, renders the widget, and returns the token. It does
NOT require a pre-opened tab and does NOT require the browser to be logged in
(the token is domain-bound, not session-bound). Call .shutdown() to close it.

Usage:
    from turnstile_ondemand import OnDemandTurnstile
    src = OnDemandTurnstile()          # lazily boots on first get_token()
    tok = src.get_token()              # boots chrome if needed, returns token
    src.shutdown()                     # close chrome + clean up

Notes:
  * headful (not --headless) because Cloudflare often blocks headless.
  * each call uses a throwaway-ish profile dir under /tmp so we can get a
    cleaner Cloudflare clearance than a long-lived session.
  * --remote-allow-origins=* is required for the CDP websocket handshake.
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import random
import shutil
import socket
import string
import subprocess
import tempfile
import time
import urllib.request
from typing import Optional

from turnstile import CDPClient, CDPError, SITE_KEY, TurnstileTokenSource

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE_URL = "https://hyper3d.ai/workspace/video"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class OnDemandTurnstile:
    """Launch Chrome -> grab token -> reuse process across calls -> shut down.

    Kept as a warm process (boot once, render many) because a full cold boot
    per request is slow; but it's on-demand in the sense that nothing needs to
    be pre-opened by the user and it self-terminates via shutdown()/atexit.
    """

    def __init__(self, site_key: str = SITE_KEY, page_url: str = PAGE_URL,
                 profile_dir: Optional[str] = None, keep_profile: bool = False,
                 human_pacing: bool = True):
        self.site_key = site_key
        self.page_url = page_url
        self.human_pacing = human_pacing
        self.port: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self._source: Optional[TurnstileTokenSource] = None
        if keep_profile and profile_dir:
            self.profile_dir = profile_dir
            self._own_profile = False
        else:
            self.profile_dir = profile_dir or os.path.join(
                tempfile.gettempdir(),
                "h3d_ts_" + "".join(random.choices(string.ascii_lowercase, k=8)))
            self._own_profile = True
        atexit.register(self.shutdown)

    # -- lifecycle ---------------------------------------------------------
    def _wait_devtools(self, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                    json.load(r)
                return True
            except Exception:
                time.sleep(0.4)
        return False

    def boot(self) -> None:
        if self._source is not None:
            return
        self.port = _free_port()
        os.makedirs(self.profile_dir, exist_ok=True)
        cmd = [
            CHROME,
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate",
            "--window-size=1000,900",
            "about:blank",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        if not self._wait_devtools():
            self.shutdown()
            raise CDPError("Chrome devtools endpoint never came up")

        # point a tab at the page
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/new?{self.page_url}",
                                   method="PUT", timeout=10)
        except Exception:
            pass
        time.sleep(3)

        # build a token source that attaches to OUR chrome by port + page hint
        src = TurnstileTokenSource(port=self.port, site_key=self.site_key,
                                   human_pacing=self.human_pacing)
        if not src.connect(reload=False):
            # fall back: navigate an existing blank tab
            self._navigate_any_tab(self.page_url)
            time.sleep(3)
            if not src.connect(reload=False):
                self.shutdown()
                raise CDPError(f"no page matching hyper3d.ai in our chrome")
        self._source = src

    def _navigate_any_tab(self, url: str) -> None:
        """Use CDP Page.navigate on the first page target we can find."""
        import urllib.request
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=5))
        except Exception:
            return
        for t in tabs:
            if t.get("type") == "page":
                c = CDPClient(port=self.port)
                c._sock = __import__("websocket").create_connection(
                    t["webSocketDebuggerUrl"], timeout=30)
                c.send("Page.enable")
                c.send("Page.navigate", {"url": url})
                c.close()
                return

    def shutdown(self) -> None:
        try:
            if self._source is not None and getattr(self._source, "_client", None):
                self._source._client.close()  # noqa: SLF001
        except Exception:
            pass
        self._source = None
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
        if self._own_profile and os.path.isdir(self.profile_dir):
            try:
                shutil.rmtree(self.profile_dir)
            except Exception:
                pass

    def is_up(self) -> bool:
        return self._source is not None

    # -- token -------------------------------------------------------------
    def get_token(self, attempts: int = 3) -> str:
        last = None
        for _ in range(attempts):
            try:
                self.boot()
                assert self._source is not None
                return self._source.get_token(attempts=2)
            except Exception as e:  # noqa: BLE001
                last = e
                # cold-start problems (profile/IP churn): tear down and retry fresh
                self.shutdown()
                time.sleep(random.uniform(2, 5))
        raise CDPError(f"on-demand token fetch failed after {attempts} tries: {last}")

    def healthy(self) -> bool:
        try:
            return bool(self._source and self._source.healthy())
        except Exception:
            return False


if __name__ == "__main__":
    src = OnDemandTurnstile()
    try:
        print("[demo] booting on-demand Chrome ...")
        t = src.get_token()
        print("[demo] TOKEN OK len", len(t))
        print("[demo] head:", t[:40])
    except Exception as e:  # noqa: BLE001
        print("[demo] failed:", e)
    finally:
        print("[demo] shutting down ...")
        src.shutdown()
