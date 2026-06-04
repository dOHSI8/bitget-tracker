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

            async def _block(route):
                if route.request.resource_type in {"document", "script", "xhr", "fetch"}:
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", _block)
            _status["browser_alive"] = True

            try:
                await page.goto(f"{BITGET_BASE}/about",
                                wait_until="domcontentloaded", timeout=30_000)
                logger.info("Navigation OK")
            except Exception as e:
                logger.info("Navigation ended early (%s) — continuing", e)

            # Use page.evaluate so JavaScript fetch runs in browser context
            # with full cookie access (credentials:include picks up cf_clearance etc.)
            await _active_poll(page, push_fn)
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
        logger.info("Positions: HTTP %s code=%s", pos.get("status"), (pos.get("data") or {}).get("code"))
        if pos.get("status") == 200 and pos.get("data"):
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
        logger.info("History: HTTP %s code=%s", hist.get("status"), (hist.get("data") or {}).get("code"))
        if hist.get("status") == 200 and hist.get("data"):
            push_fn("history", hist["data"])
    except Exception as e:
        logger.warning("Poll history error: %s", e)

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
                    if (!r.ok) return null;
                    const j = await r.json();
                    const rows = j?.data?.rows || j?.data?.list || j?.data || [];
                    return Array.isArray(rows) && rows.length > 0 ? rows : null;
                } catch(e) { return null; }
            }""", [ep, PORTFOLIO_ID])
            if result:
                logger.info("Polled balance_history from %s (%d rows)", ep, len(result))
                push_fn("balance_history", result)
                break
        except Exception:
            pass

    _status["last_poll"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["polls"] += 1


async def _fetch_balance(page, push_fn: Callable):
    endpoints = [
        ("/v1/trace/mt5/trace/traceDetail", True),
        ("/v1/trace/mt5/data/copyDetail", True),
        ("/v1/trace/mt5/data/accountInfo", True),
        ("/v1/trace/mt5/account/balance", True),
        ("/v1/trace/mt5/data/followerDetail", True),
        ("/v1/trace/mt5/trace/followerDetail", True),
    ]
    for ep, is_post in endpoints:
        try:
            result = await page.evaluate("""async ([ep, pid, isPost]) => {
                try {
                    const opts = {credentials: 'include'};
                    if (isPost) {
                        opts.method = 'POST';
                        opts.headers = {'Content-Type': 'application/json'};
                        opts.body = JSON.stringify({portfolioId: pid});
                    }
                    const r = await fetch(ep, opts);
                    if (!r.ok) return {status: r.status};
                    const j = await r.json();
                    return {status: r.status, data: j};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [ep, PORTFOLIO_ID, is_post])

            if not result or result.get("status") != 200:
                logger.info("Balance %s → HTTP %s", ep.split("/")[-1], result.get("status") if result else 0)
                continue

            j = result.get("data") or {}
            d = j.get("data", j) if isinstance(j, dict) else j
            if isinstance(d, dict):
                if any("balance" in k.lower() or "equity" in k.lower() for k in d):
                    logger.info("Found balance via %s", ep.split("/")[-1])
                    push_fn("copy_details", d)
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status["scrapes"] += 1
                    return
                logger.info("Balance %s → keys: %s", ep.split("/")[-1], list(d.keys())[:6])
        except Exception as e:
            logger.warning("Balance %s error: %s", ep.split("/")[-1], e)

    logger.warning("No balance endpoint found")
