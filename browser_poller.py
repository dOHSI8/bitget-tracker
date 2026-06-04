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
                logger.info("CF challenge passed")
            except Exception as e:
                logger.info("About nav: %s", e)

            await _active_poll(page, push_fn)
            await _fetch_balance(page, push_fn)

            logger.info("Poll cycle complete — closing browser")
        finally:
            await browser.close()


async def _active_poll(page, push_fn: Callable):
    logger.info("Polling APIs via page.evaluate...")

    # ── Positions — probe /trace/ variants (follower-accessible) ─────────────
    pos_probes = []
    for ep in [
        "/v1/trace/mt5/trace/queryFollowPosition",
        "/v1/trace/mt5/trace/followPosition",
        "/v1/trace/mt5/trace/currentPosition",
        "/v1/trace/mt5/trace/openPosition",
        "/v1/trace/mt5/trace/holdingPosition",
        "/v1/trace/mt5/trace/positionDetail",
    ]:
        try:
            result = await page.evaluate("""async ([ep, pid]) => {
                try {
                    const r = await fetch(ep, {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({portfolioId: pid}),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, code: j?.code, msg: j?.msg,
                            data_keys: j?.data != null ? Object.keys(Object(j.data)).slice(0, 8) : null};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [ep, PORTFOLIO_ID])
            name = ep.split("/")[-1]
            code = result.get("code") if isinstance(result, dict) else None
            entry = {"ep": name, "status": result.get("status"), "code": code, "error": result.get("error"), "data_keys": result.get("data_keys")}
            pos_probes.append(entry)
            if isinstance(result, dict) and result.get("status") == 200 and code in ("00000", "200", "0"):
                logger.info("Positions found at %s code=%s keys=%s", ep, code, result.get("data_keys"))
                push_fn("positions", result.get("data") or {})
                break
        except Exception as ex:
            pos_probes.append({"ep": ep.split("/")[-1], "error": str(ex)})
    _status["last_pos_response"] = pos_probes[0] if pos_probes else {}
    _status["last_pos_probes"] = pos_probes

    # ── History — confirmed: /trace/positionHistory ───────────────────────────
    try:
        hist = await page.evaluate("""async (pid) => {
            try {
                const r = await fetch('/v1/trace/mt5/trace/positionHistory', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({portfolioId: pid, pageNo: 1, pageSize: 50}),
                });
                const text = await r.text();
                if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                const j = JSON.parse(text);
                return {status: r.status, data: j};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""", PORTFOLIO_ID)
        api_code = (hist.get("data") or {}).get("code")
        api_msg  = (hist.get("data") or {}).get("msg")
        logger.info("History: HTTP %s api_code=%s err=%s", hist.get("status"), api_code, hist.get("error"))
        _status["last_hist_response"] = {
            "http": hist.get("status"), "code": api_code, "msg": api_msg,
            "error": hist.get("error"),
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
    # getFollowPortfolios returns the follower's copy portfolio including balance,
    # investment, equity and realizedPnl — confirmed via Proxyman capture.
    try:
        result = await page.evaluate("""async (pid) => {
            try {
                const r = await fetch('/v1/trace/mt5/trace/getFollowPortfolios', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({portfolioId: pid}),
                });
                const j = await r.json();
                return {status: r.status, code: j?.code, data: j?.data};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""", PORTFOLIO_ID)

        code = result.get("code") if isinstance(result, dict) else None
        _status["last_balance_probes"] = {"getFollowPortfolios": {
            "http": result.get("status"), "code": code}}

        if result.get("status") == 200 and code in ("00000", "200", "0"):
            data = result.get("data") or {}
            details = data.get("portfolioDetails") or []
            if details and isinstance(details[0], dict):
                portfolio = details[0]
                logger.info("getFollowPortfolios OK: balance=%s investment=%s",
                            portfolio.get("balance"), portfolio.get("totalInvestment"))
                push_fn("copy_details", portfolio)
                _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                _status["scrapes"] += 1
                return
            logger.warning("getFollowPortfolios: no portfolioDetails in response")
        else:
            logger.warning("getFollowPortfolios failed: http=%s code=%s",
                           result.get("status"), code)
    except Exception as e:
        logger.warning("getFollowPortfolios error: %s", e)
        _status["last_balance_probes"] = {"getFollowPortfolios": {"error": str(e)}}

    logger.warning("Balance fetch failed")
