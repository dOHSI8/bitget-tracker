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
BITGET_PAGE = os.environ.get(
    "BITGET_PAGE",
    f"https://www.bitget.com/copy-trading/mt5/follower/detail?portfolioId={PORTFOLIO_ID}",
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "120"))
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--no-zygote",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--no-first-run",
    "--mute-audio",
    "--disable-hang-monitor",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-ipc-flooding-protection",
    "--disable-features=TranslateUI,site-per-process",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=128",
    "--disable-canvas-aa",
    "--disable-2d-canvas-clip-aa",
    "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
]

BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}


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
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".bitget.com",
            "path": "/",
        })
    return cookies


_status = {
    "running": False,
    "browser_alive": False,
    "last_poll": None,
    "last_scrape": None,
    "last_error": None,
    "polls": 0,
    "scrapes": 0,
    "last_page_text": None,
}


def get_status() -> dict:
    cookie_str = _load_cookie_string()
    return {
        **_status,
        "has_cookie": bool(cookie_str),
        "cookie_preview": (cookie_str[:40] + "...") if len(cookie_str) > 40 else cookie_str,
        "poll_interval_sec": POLL_INTERVAL,
    }


async def start_poller(push_fn: Callable):
    _status["running"] = True
    await asyncio.sleep(3)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed — browser poller disabled")
        _status["last_error"] = "Playwright not installed"
        return

    while True:
        cookie_str = _load_cookie_string()
        if not cookie_str:
            _status["last_error"] = "No cookie set"
            _status["browser_alive"] = False
            await asyncio.sleep(10)
            continue

        _status["last_error"] = None
        try:
            await _poll_once(push_fn, cookie_str)
        except Exception as e:
            logger.error("Poll cycle crashed: %s", e)
            _status["last_error"] = f"Browser crashed: {e}"

        _status["browser_alive"] = False
        logger.info("Next poll in %ds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once(push_fn: Callable, cookie_str: str):
    """Launch browser, grab all data via API calls only, close browser."""
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

            # Block ALL non-essential resources — we only need fetch() to work
            async def _block(route):
                rt = route.request.resource_type
                if rt in {"document", "xhr", "fetch", "script"}:
                    await route.continue_()
                else:
                    await route.abort()
            await page.route("**/*", _block)

            # Navigate to a lightweight Bitget page (just to establish session)
            logger.info("Poll: launching browser...")
            try:
                await page.goto("https://www.bitget.com/about", wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                logger.warning("Navigation timeout (may still work): %s", e)

            _status["browser_alive"] = True
            _status["last_error"] = None

            # All data via direct fetch() calls
            await _active_poll(page, push_fn)
            await _fetch_balance(page, push_fn)

            logger.info("Poll cycle complete — closing browser")

        finally:
            await browser.close()


def _classify_and_push(url: str, data: dict, push_fn: Callable):
    if "tracePosition" in url or "trace_position" in url:
        logger.info("Browser: captured positions")
        push_fn("positions", data)
        return
    if "positionHistory" in url or "position_history" in url:
        logger.info("Browser: captured history")
        push_fn("history", data)
        return
    if any(x in url for x in ("balanceHistory", "balance_history", "balanceLog", "fundFlow")):
        logger.info("Browser: captured balance_history")
        push_fn("balance_history", data.get("data", data) if isinstance(data, dict) else data)
        return
    if any(x in url for x in ("traceDetail", "trace_detail", "copyDetail", "accountInfo")):
        d = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(d, dict) and (d.get("totalBalance") or d.get("totalEquity") or d.get("balance")):
            logger.info("Browser: captured copy_details")
            push_fn("copy_details", d)
        return
    if isinstance(data, dict):
        d = data.get("data", data)
        if isinstance(d, dict) and not isinstance(d, list):
            bal_key = next((k for k in d if any(pat in k.lower() for pat in ("balance", "equity"))), None)
            if bal_key:
                push_fn("copy_details", d)
                return


async def _active_poll(page, push_fn: Callable):
    logger.info("Browser: polling APIs...")
    try:
        pos = await page.evaluate("""async (pid) => {
            const r = await fetch('/v1/trace/mt5/data/tracePosition', {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ portfolioId: pid }),
            });
            return r.ok ? await r.json() : null;
        }""", PORTFOLIO_ID)
        if pos:
            push_fn("positions", pos)
    except Exception as e:
        logger.warning("Poll positions error: %s", e)

    try:
        hist = await page.evaluate("""async (pid) => {
            const r = await fetch('/v1/trace/mt5/trace/positionHistory', {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ portfolioId: pid, pageNo: 1, pageSize: 50 }),
            });
            return r.ok ? await r.json() : null;
        }""", PORTFOLIO_ID)
        if hist:
            push_fn("history", hist)
    except Exception as e:
        logger.warning("Poll history error: %s", e)

    for ep in [
        "/v1/trace/mt5/trace/balanceHistory",
        "/v1/trace/mt5/data/balanceHistory",
        "/v1/trace/mt5/trace/fundFlow",
    ]:
        try:
            bal = await page.evaluate("""async ([ep, pid]) => {
                const r = await fetch(ep, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ portfolioId: pid, pageNo: 1, pageSize: 100 }),
                });
                if (!r.ok) return null;
                const j = await r.json();
                const rows = j?.data?.rows || j?.data?.list || j?.data || [];
                return Array.isArray(rows) && rows.length > 0 ? rows : null;
            }""", [ep, PORTFOLIO_ID])
            if bal:
                logger.info("Browser: polled balance_history from %s", ep)
                push_fn("balance_history", bal)
                break
        except Exception:
            pass

    _status["last_poll"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["polls"] += 1


async def _fetch_balance(page, push_fn: Callable):
    """Try multiple endpoints to find balance/equity data."""

    # GET endpoints
    get_eps = [
        f"/v1/trace/mt5/trace/traceDetail?portfolioId={PORTFOLIO_ID}",
        f"/v1/trace/mt5/data/copyDetail?portfolioId={PORTFOLIO_ID}",
        f"/v1/trace/mt5/data/accountInfo?portfolioId={PORTFOLIO_ID}",
        f"/v1/trace/mt5/account/balance?portfolioId={PORTFOLIO_ID}",
        f"/v1/trace/mt5/data/followerDetail?portfolioId={PORTFOLIO_ID}",
        f"/v1/trace/mt5/trace/followerDetail?portfolioId={PORTFOLIO_ID}",
        f"/v1/trace/mt5/data/traceInfo?portfolioId={PORTFOLIO_ID}",
    ]
    for ep in get_eps:
        try:
            result = await page.evaluate("""async (ep) => {
                const r = await fetch(ep, { credentials: 'include' });
                if (!r.ok) return { _status: r.status, _url: ep };
                return await r.json();
            }""", ep)
            if result and isinstance(result, dict):
                status_code = result.get("_status")
                if status_code:
                    logger.info("Balance GET %s → %s", ep.split("?")[0].split("/")[-1], status_code)
                    continue
                d = result.get("data", result)
                if isinstance(d, dict):
                    has_bal = any(k for k in d if "balance" in k.lower() or "equity" in k.lower())
                    if has_bal:
                        logger.info("Browser: found balance via GET %s", ep)
                        push_fn("copy_details", d)
                        _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                        _status["scrapes"] += 1
                        return
                    logger.info("Balance GET %s → keys: %s", ep.split("?")[0].split("/")[-1], list(d.keys())[:10])
        except Exception as e:
            logger.warning("Balance GET %s error: %s", ep.split("?")[0].split("/")[-1], e)

    # POST endpoints
    post_eps = [
        "/v1/trace/mt5/trace/traceDetail",
        "/v1/trace/mt5/data/copyDetail",
        "/v1/trace/mt5/data/followerDetail",
        "/v1/trace/mt5/trace/followerDetail",
        "/v1/trace/mt5/data/accountInfo",
        "/v1/trace/mt5/account/balance",
    ]
    for ep in post_eps:
        try:
            result = await page.evaluate("""async ([ep, pid]) => {
                const r = await fetch(ep, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ portfolioId: pid }),
                });
                if (!r.ok) return { _status: r.status, _url: ep };
                return await r.json();
            }""", [ep, PORTFOLIO_ID])
            if result and isinstance(result, dict):
                status_code = result.get("_status")
                if status_code:
                    logger.info("Balance POST %s → %s", ep.split("/")[-1], status_code)
                    continue
                d = result.get("data", result)
                if isinstance(d, dict):
                    has_bal = any(k for k in d if "balance" in k.lower() or "equity" in k.lower())
                    if has_bal:
                        logger.info("Browser: found balance via POST %s", ep)
                        push_fn("copy_details", d)
                        _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                        _status["scrapes"] += 1
                        return
                    logger.info("Balance POST %s → keys: %s", ep.split("/")[-1], list(d.keys())[:10])
        except Exception as e:
            logger.warning("Balance POST %s error: %s", ep.split("/")[-1], e)

    logger.warning("Browser: no balance endpoint found")


async def _click_tab(page, tab_name: str):
    try:
        clicked = await page.evaluate("""(name) => {
            const els = document.querySelectorAll('[role="tab"], [class*="tab"], [class*="Tab"], button, span, div');
            for (const el of els) {
                const text = (el.innerText || '').trim();
                if (text === name || text.toLowerCase() === name.toLowerCase()) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""", tab_name)
        if clicked:
            logger.info("Browser: clicked tab '%s'", tab_name)
    except Exception as e:
        logger.warning("Click tab error: %s", e)
