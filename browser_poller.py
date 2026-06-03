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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "60"))
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL_SEC", "30"))
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SEC", "3600"))
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))


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

    cookie_str = _load_cookie_string()
    if not cookie_str:
        logger.info("Browser poller: no cookie set, waiting...")
        _status["last_error"] = "No cookie set"
        while not _load_cookie_string():
            await asyncio.sleep(10)
        cookie_str = _load_cookie_string()

    logger.info("Browser poller: starting with %d-char cookie", len(cookie_str))

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed — browser poller disabled")
        _status["last_error"] = "Playwright not installed"
        return

    while True:
        _status["last_error"] = None
        try:
            await _run_browser_session(push_fn, cookie_str)
        except Exception as e:
            logger.error("Browser session crashed: %s", e)
            _status["last_error"] = f"Browser crashed: {e}"
            _status["browser_alive"] = False

        cookie_str = _load_cookie_string()
        if not cookie_str:
            logger.info("Browser poller: cookie cleared, waiting...")
            _status["last_error"] = "No cookie set"
            while not _load_cookie_string():
                await asyncio.sleep(10)
            cookie_str = _load_cookie_string()

        logger.info("Browser poller: restarting in 15s...")
        await asyncio.sleep(15)


async def _run_browser_session(push_fn: Callable, cookie_str: str):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
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
                "--disable-features=TranslateUI,site-per-process,NetworkService",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=128",
                "--disable-canvas-aa",
                "--disable-2d-canvas-clip-aa",
                "--disable-software-rasterizer",
                "--disable-accelerated-2d-canvas",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 800, "height": 600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )

        cookies = _parse_cookie_string(cookie_str)
        if cookies:
            await context.add_cookies(cookies)
            logger.info("Injected %d cookies", len(cookies))

        page = await context.new_page()

        # Block heavy resources to save memory
        BLOCKED_TYPES = {"image", "media", "font"}
        async def _block_resources(route):
            if route.request.resource_type in BLOCKED_TYPES:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", _block_resources)

        # Intercept API responses
        async def on_response(response):
            url = response.url
            if "/v1/" not in url:
                return
            try:
                data = await response.json()
                _classify_and_push(url, data, push_fn)
            except Exception:
                pass

        page.on("response", on_response)

        logger.info("Navigating to %s", BITGET_PAGE)
        try:
            await page.goto(BITGET_PAGE, wait_until="networkidle", timeout=60_000)
        except Exception as e:
            logger.warning("Navigation timeout (may still work): %s", e)

        title = await page.title()
        logger.info("Page title: %s", title)

        # Check if logged in
        text = await page.evaluate("document.body?.innerText?.slice(0, 500) || ''")
        if "Log In" in text and "Sign Up" in text:
            logger.error("Not logged in — cookie may be expired")
            _status["last_error"] = "Cookie expired — not logged in. Paste a fresh cookie."
            _status["browser_alive"] = False
            await browser.close()
            while _load_cookie_string() == cookie_str:
                await asyncio.sleep(10)
            return

        _status["browser_alive"] = True
        _status["last_error"] = None
        logger.info("Browser poller: logged in and running")

        # Auto tab cycle: Balance history → scrape → Positions
        await _auto_tab_cycle(page, push_fn)

        # Initial poll
        await _active_poll(page, push_fn)

        # Main loop
        poll_counter = 0
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                poll_counter += 1

                # Poll API
                await _active_poll(page, push_fn)

                # Scrape DOM every other cycle
                if poll_counter % 2 == 0:
                    await _scrape_copy_details(page, push_fn)

                # Full page refresh every hour
                if poll_counter * POLL_INTERVAL >= REFRESH_INTERVAL:
                    poll_counter = 0
                    logger.info("Auto-refreshing page...")
                    try:
                        await page.goto(BITGET_PAGE, wait_until="networkidle", timeout=60_000)
                        await _auto_tab_cycle(page, push_fn)
                    except Exception as e:
                        logger.warning("Refresh error: %s", e)

                # Check if cookie was updated
                current_cookie = _load_cookie_string()
                if current_cookie != cookie_str:
                    logger.info("Cookie changed — restarting browser session")
                    break

        finally:
            _status["browser_alive"] = False
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
    # Broad sniff for balance fields
    if isinstance(data, dict):
        d = data.get("data", data)
        if isinstance(d, dict) and not isinstance(d, list):
            bal_key = next((k for k in d if any(p in k.lower() for p in ("balance", "equity"))), None)
            if bal_key:
                push_fn("copy_details", d)
                return


async def _active_poll(page, push_fn: Callable):
    logger.info("Browser: polling...")
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


async def _scrape_copy_details(page, push_fn: Callable):
    try:
        page_text = await page.evaluate("() => (document.body?.innerText || '').slice(0, 1000)")
        _status["last_page_text"] = page_text
        logger.info("Browser: page text preview: %s", page_text[:300])
    except Exception as e:
        logger.warning("Page text capture error: %s", e)

    try:
        details = await page.evaluate("""() => {
            const text = document.body?.innerText || '';
            const balMatch = text.match(/Total\\s*balance\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*([\\d,]+\\.?\\d*)/i);
            const eqMatch = text.match(/Total\\s*equity\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*([\\d,]+\\.?\\d*)/i);
            const bal = balMatch ? parseFloat(balMatch[1].replace(/,/g, '')) : 0;
            const eq = eqMatch ? parseFloat(eqMatch[1].replace(/,/g, '')) : 0;
            const value = bal || eq;

            const netMatch = text.match(/Est\\.?\\s*net\\s*profit\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*[+\\-]?([\\d,]+\\.?\\d*)/i);
            const realMatch = text.match(/(?<![Uu]n)(?:^|[^a-zA-Z])[Rr]ealized\\s*PnL\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*[+\\-]?([\\d,]+\\.?\\d*)/);
            const unrealMatch = text.match(/Unrealized\\s*PnL\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*[+\\-]?([\\d,]+\\.?\\d*)/i);

            const netProfit = netMatch ? parseFloat(netMatch[1].replace(/,/g, '')) : null;
            const realPnl = realMatch ? parseFloat(realMatch[1].replace(/,/g, '')) : null;
            const unrealPnl = unrealMatch ? parseFloat(unrealMatch[1].replace(/,/g, '')) : null;

            const netSign = netMatch && text.match(/Est\\.?\\s*net\\s*profit\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*-/) ? -1 : 1;
            const realSign = realMatch && text.match(/(?<![Uu]n)(?:^|[^a-zA-Z])[Rr]ealized\\s*PnL\\s*\\(?USDT\\)?\\s*[\\n\\r]*\\s*-/) ? -1 : 1;

            if (value <= 0 && netProfit === null) return null;
            const result = { totalBalance: value, totalEquity: eq || value };
            if (netProfit !== null) result.estNetProfit = netProfit * netSign;
            if (realPnl !== null) result.realizedPnl = realPnl * realSign;
            if (unrealPnl !== null) result.unrealizedPnl = unrealPnl;
            return result;
        }""")
        if details:
            logger.info("Browser: DOM scraped copy_details")
            push_fn("copy_details", details)
    except Exception as e:
        logger.warning("Scrape copy_details error: %s", e)

    try:
        rows = await page.evaluate("""() => {
            const text = document.body?.innerText || '';
            const rows = [];
            for (const m of text.matchAll(/\\bAdd\\b[\\s\\S]{0,30}?([\\d,]+\\.?\\d+)\\s*USDT/gi)) {
                rows.push({ type: 'Add', amount: parseFloat(m[1].replace(/,/g, '')) });
            }
            for (const m of text.matchAll(/Transfer\\s*out[\\s\\S]{0,30}?([\\d,]+\\.?\\d+)\\s*USDT/gi)) {
                rows.push({ type: 'Transfer out', amount: parseFloat(m[1].replace(/,/g, '')) });
            }
            return rows;
        }""")
        if rows:
            logger.info("Browser: DOM scraped %d balance_history rows", len(rows))
            push_fn("balance_history", rows)
    except Exception as e:
        logger.warning("Scrape balance_history error: %s", e)

    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["scrapes"] += 1


async def _auto_tab_cycle(page, push_fn: Callable):
    await asyncio.sleep(10)
    await _click_tab(page, "Balance history")
    await asyncio.sleep(5)
    await _scrape_copy_details(page, push_fn)
    await asyncio.sleep(5)
    await _click_tab(page, "Positions")


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
