import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

BKK = timezone(timedelta(hours=7))
PORTFOLIO_ID = os.environ.get("PORTFOLIO_ID", "1443199880395776000")
BITGET_BASE = "https://www.bitget.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "120"))
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))

CHROMIUM_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
    "--disable-gpu", "--single-process", "--no-zygote",
    "--disable-extensions", "--disable-background-networking",
    "--disable-default-apps", "--disable-sync", "--no-first-run",
    "--mute-audio", "--disable-hang-monitor",
    "--disable-features=TranslateUI,site-per-process",
    "--js-flags=--max-old-space-size=64",
    "--enable-low-end-device-mode",
]

_status = {
    "running": False,
    "browser_alive": False,
    "last_poll": None,
    "last_scrape": None,
    "last_error": None,
    "polls": 0,
    "scrapes": 0,
    "pushes": 0,
    "last_pos_response": None,
    "last_hist_response": None,
}


def get_status() -> dict:
    cookie_str = _load_cookie_string()
    return {
        **_status,
        "has_cookie": bool(cookie_str),
        "cookie_preview": (cookie_str[:40] + "...") if len(cookie_str) > 40 else cookie_str,
        "poll_interval_sec": POLL_INTERVAL,
    }


def _load_cookie_string() -> str:
    if COOKIES_FILE.exists():
        try:
            data = json.loads(COOKIES_FILE.read_text())
            val = data.get("cookie", "")
            if val:
                return val
        except (json.JSONDecodeError, OSError):
            pass
    return os.environ.get("BITGET_COOKIE", "")


def _parse_cookie_string(cookie_str: str) -> list[dict]:
    cookies = []
    for pair in cookie_str.split("; "):
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value, "domain": ".bitget.com", "path": "/"})
    return cookies


async def start_poller(push_fn: Callable):
    _status["running"] = True
    await asyncio.sleep(3)

    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        logger.error("Playwright not installed — poller disabled")
        _status["last_error"] = "Playwright not installed"
        return

    # Wrap push_fn to count successful pushes
    def _counted_push(kind: str, data):
        _status["pushes"] += 1
        logger.info("push_fn called: kind=%s pushes=%d", kind, _status["pushes"])
        push_fn(kind, data)

    while True:
        cookie_str = _load_cookie_string()
        if not cookie_str:
            _status["last_error"] = "No cookie set"
            _status["browser_alive"] = False
            await asyncio.sleep(10)
            continue

        _status["last_error"] = None
        try:
            await _poll_once(_counted_push, cookie_str)
        except Exception as e:
            logger.error("Poll cycle crashed: %s", e)
            _status["last_error"] = f"Poll error: {e}"

        _status["browser_alive"] = False
        logger.info("Next poll in %ds… (pushes so far: %d)", POLL_INTERVAL, _status["pushes"])
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once(push_fn: Callable, cookie_str: str):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            context = await browser.new_context(
                viewport={"width": 800, "height": 600},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            )

            cookies = _parse_cookie_string(cookie_str)
            if cookies:
                await context.add_cookies(cookies)

            page = await context.new_page()

            # Capture every /v1/trace/mt5/ API call the page makes natively
            captured_apis: dict = {}

            async def _on_resp(resp):
                url = resp.url
                # Capture any Bitget API call (not just /v1/trace/mt5/)
                if "bitget.com/v" not in url and "bitget.com/api" not in url:
                    return
                try:
                    body = await resp.json()
                    path = url.split(".com")[-1].split("?")[0]
                    captured_apis[path] = {"status": resp.status, "body": body}
                except Exception:
                    pass

            page.on("response", _on_resp)

            async def _block(route):
                if route.request.resource_type in {"document", "script", "xhr", "fetch"}:
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", _block)
            _status["browser_alive"] = True

            # Navigate to the copy trading my-portfolio page so Bitget's JS fires API calls.
            # Try a few URL patterns in case one redirects.
            nav_urls = [
                f"{BITGET_BASE}/copy-trading/mt5/myPortfolio",
                f"{BITGET_BASE}/copy-trading/mt5/my-portfolio",
                f"{BITGET_BASE}/copy-trading/personal",
            ]
            for nav_url in nav_urls:
                try:
                    await page.goto(nav_url, wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(4000)
                    if captured_apis:
                        logger.info("Captured %d API calls from %s", len(captured_apis), nav_url)
                        break
                    logger.info("No API calls captured from %s, trying next", nav_url)
                except Exception as e:
                    logger.info("Nav %s ended: %s", nav_url, e)

            # Store all captured paths for inspection at /api/poller
            _status["captured_api_paths"] = [
                {"path": p, "code": (v["body"] or {}).get("code"),
                 "keys": list((v["body"].get("data") or {}).keys())[:8]
                         if isinstance((v["body"] or {}).get("data"), dict) else None}
                for p, v in captured_apis.items()
            ]

            # Process any balance-looking response from the page load
            for path, item in captured_apis.items():
                body = item.get("body") or {}
                code = body.get("code")
                if code not in ("00000", "200", "0"):
                    continue
                data = body.get("data")
                if isinstance(data, dict):
                    keys = list(data.keys())
                    _BAL_PATS = ("balance", "equity", "asset", "worth", "capital",
                                 "amount", "profit", "pnl", "netval")
                    if any(any(pat in k.lower() for pat in _BAL_PATS) for k in keys):
                        logger.info("Balance found via page capture: %s keys=%s", path, keys[:8])
                        push_fn("copy_details", data)

            # Manual polls for positions & history (reliable, fast)
            await _active_poll(page, push_fn)
            # Balance scan — will 404 on known paths but runs as fallback
            await _fetch_balance(page, push_fn)

            logger.info("Poll cycle complete — closing browser")
        finally:
            await browser.close()


async def _active_poll(page, push_fn: Callable):
    logger.info("Polling APIs via page.evaluate...")

    try:
        pos = await page.evaluate("""async (pid) => {
            try {
                const r = await fetch('/v1/trace/mt5/data/tracePosition', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({portfolioId: pid}),
                });
                const j = await r.json();
                return {status: r.status, data: j};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""", PORTFOLIO_ID)
        api_code = (pos.get("data") or {}).get("code")
        api_msg  = (pos.get("data") or {}).get("msg")
        logger.info("Positions: HTTP %s api_code=%s msg=%s", pos.get("status"), api_code, api_msg)
        _status["last_pos_response"] = {
            "http": pos.get("status"), "code": api_code, "msg": api_msg,
            "data_preview": str(pos.get("data"))[:200] if pos.get("data") else None,
        }
        if pos.get("status") == 200 and api_code in ("00000", "200", "0"):
            push_fn("positions", pos["data"])
    except Exception as e:
        logger.warning("Poll positions error: %s", e)

    try:
        hist = await page.evaluate("""async (pid) => {
            try {
                const r = await fetch('/v1/trace/mt5/trace/positionHistory', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({portfolioId: pid, pageNo: 1, pageSize: 50}),
                });
                const j = await r.json();
                return {status: r.status, data: j};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""", PORTFOLIO_ID)
        api_code = (hist.get("data") or {}).get("code")
        api_msg  = (hist.get("data") or {}).get("msg")
        logger.info("History: HTTP %s api_code=%s msg=%s", hist.get("status"), api_code, api_msg)
        _status["last_hist_response"] = {
            "http": hist.get("status"), "code": api_code, "msg": api_msg,
            "data_preview": str(hist.get("data"))[:200] if hist.get("data") else None,
        }
        if hist.get("status") == 200 and api_code in ("00000", "200", "0"):
            push_fn("history", hist["data"])
    except Exception as e:
        logger.warning("Poll history error: %s", e)

    bh_probes = []
    for ep in ["/v1/trace/mt5/trace/balanceHistory",
               "/v1/trace/mt5/data/balanceHistory",
               "/v1/trace/mt5/trace/fundFlow"]:
        try:
            result = await page.evaluate("""async ([ep, pid]) => {
                try {
                    const r = await fetch(ep, {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({portfolioId: pid, pageNo: 1, pageSize: 100}),
                    });
                    const j = await r.json();
                    if (!r.ok) return {status: r.status, code: j?.code};
                    const rows = j?.data?.rows || j?.data?.list || j?.data || [];
                    return {status: r.status, code: j?.code,
                            rows: Array.isArray(rows) ? rows : null,
                            sample: !Array.isArray(rows) && typeof j?.data === 'object'
                                ? Object.keys(j?.data||{}).slice(0,6) : null};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [ep, PORTFOLIO_ID])
            bh_probes.append({"ep": ep.split("/")[-1], **result})
            rows = result.get("rows") if isinstance(result, dict) else None
            if rows:
                logger.info("Polled balance_history from %s (%d rows)", ep, len(rows))
                push_fn("balance_history", rows)
                break
        except Exception as ex:
            bh_probes.append({"ep": ep.split("/")[-1], "error": str(ex)})
    _status["last_bh_probes"] = bh_probes

    _status["last_poll"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["polls"] += 1


async def _fetch_balance(page, push_fn: Callable):
    # Phase 1: broad scan across many candidate paths to find which exist
    scan = await page.evaluate("""async (pid) => {
        const candidates = [
            ['/v1/trace/mt5/data/traceHome',      'POST'],
            ['/v1/trace/mt5/trace/traceHome',      'POST'],
            ['/v1/trace/mt5/data/overview',         'POST'],
            ['/v1/trace/mt5/trace/overview',        'POST'],
            ['/v1/trace/mt5/data/traderInfo',       'POST'],
            ['/v1/trace/mt5/trace/traderInfo',      'POST'],
            ['/v1/trace/mt5/data/traderDetail',     'POST'],
            ['/v1/trace/mt5/trace/traderDetail',    'POST'],
            ['/v1/trace/mt5/data/portfolioInfo',    'POST'],
            ['/v1/trace/mt5/trace/portfolioInfo',   'POST'],
            ['/v1/trace/mt5/data/profitInfo',       'POST'],
            ['/v1/trace/mt5/trace/profitInfo',      'POST'],
            ['/v1/trace/mt5/data/summary',          'POST'],
            ['/v1/trace/mt5/trace/summary',         'POST'],
            ['/v1/trace/mt5/data/traceAmount',      'POST'],
            ['/v1/trace/mt5/trace/traceAmount',     'POST'],
            ['/v1/trace/mt5/data/capitalInfo',      'POST'],
            ['/v1/trace/mt5/trace/capitalInfo',     'POST'],
            ['/v1/trace/mt5/data/tracePortfolio',   'POST'],
            ['/v1/trace/mt5/trace/tracePortfolio',  'POST'],
            ['/v1/trace/mt5/data/traceAnalysis',    'POST'],
            ['/v1/trace/mt5/trace/traceAnalysis',   'POST'],
            ['/v1/trace/mt5/data/profitSummary',    'POST'],
            ['/v1/trace/mt5/trace/profitSummary',   'POST'],
        ];
        const out = {};
        for (const [ep, method] of candidates) {
            try {
                const r = await fetch(ep, {
                    method, credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({portfolioId: pid}),
                });
                const j = await r.json();
                const d = j?.data;
                out[ep.split('/').pop()] = {
                    s: r.status, c: j?.code, msg: j?.msg,
                    k: (d && typeof d==='object' && !Array.isArray(d)) ? Object.keys(d).slice(0,10) : null,
                    sample: (d && typeof d==='object' && !Array.isArray(d))
                        ? Object.fromEntries(Object.entries(d).slice(0,4).map(([k,v])=>[k,String(v).slice(0,40)]))
                        : String(d).slice(0,80),
                };
            } catch(e) { out[ep.split('/').pop()] = {e: String(e).slice(0,40)}; }
        }
        return out;
    }""", PORTFOLIO_ID)

    _status["last_balance_probes"] = scan or {}
    logger.info("Balance scan: %s", {k: v.get("s") for k, v in (scan or {}).items()})

    # Phase 2: use any endpoint that returned success with data
    _BAL_PATS = ("balance", "equity", "totalasset", "accountval", "worth", "asset",
                 "capital", "amount", "profit", "pnl")
    for ep_name, info in (scan or {}).items():
        if info.get("s") != 200 or info.get("c") not in ("00000", "200", "0"):
            continue
        keys = info.get("k") or []
        if not keys:
            continue
        # Fetch full data from this endpoint
        try:
            full = await page.evaluate("""async ([ep, pid]) => {
                try {
                    const r = await fetch(ep, {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({portfolioId: pid}),
                    });
                    const j = await r.json();
                    return j?.data || null;
                } catch(e) { return null; }
            }""", [f"/v1/trace/mt5/data/{ep_name}", PORTFOLIO_ID])
            if not full:
                full = await page.evaluate("""async ([ep, pid]) => {
                    try {
                        const r = await fetch(ep, {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({portfolioId: pid}),
                        });
                        const j = await r.json();
                        return j?.data || null;
                    } catch(e) { return null; }
                }""", [f"/v1/trace/mt5/trace/{ep_name}", PORTFOLIO_ID])
            if full and isinstance(full, dict):
                logger.info("Using balance endpoint %s, keys: %s", ep_name, list(full.keys())[:8])
                push_fn("copy_details", full)
                _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                _status["scrapes"] += 1
                return
        except Exception as e:
            logger.warning("Balance full-fetch %s error: %s", ep_name, e)

    logger.warning("No balance endpoint found after scanning %d candidates", len(scan or {}))
