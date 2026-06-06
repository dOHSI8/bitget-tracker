import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

BKK = timezone(timedelta(hours=7))
BITGET_BASE = "https://www.bitget.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "120"))
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))

# ── Parse TRADERS env var ─────────────────────────────────────────────────────
# Format: "Name:portfolioId[:type],..."  where type = "cfd" (default) or "futures"
# Example: "DKTrading:1443199880395776000,Futures:1427930164156649472:futures"

_TRADERS: dict[str, str] = {}       # {name: portfolioId}
_TRADER_TYPES: dict[str, str] = {}  # {name: "cfd" | "futures"}

_TRADERS_ENV = os.environ.get("TRADERS", "")
if _TRADERS_ENV:
    for _item in _TRADERS_ENV.split(","):
        _parts = _item.strip().split(":")
        if len(_parts) >= 2:
            _name = _parts[0].strip()
            _pid  = _parts[1].strip()
            _type = _parts[2].strip() if len(_parts) >= 3 else "cfd"
            _TRADERS[_name] = _pid
            _TRADER_TYPES[_name] = _type
else:
    _name0 = os.environ.get("TRADER_NAME", "DKTrading")
    _TRADERS[_name0] = os.environ.get("PORTFOLIO_ID", "1443199880395776000")
    _TRADER_TYPES[_name0] = "cfd"

# Reverse lookup: portfolioId → trader name
_pid_to_name: dict[str, str] = {pid: name for name, pid in _TRADERS.items()}

# First CFD portfolio ID (used for the positions probe)
_cfd_pids = [p for n, p in _TRADERS.items() if _TRADER_TYPES.get(n) == "cfd"]
PORTFOLIO_ID = _cfd_pids[0] if _cfd_pids else next(iter(_TRADERS.values()), "")

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
    "auth_ok": None,   # None=unknown, True=working, False=cookie expired/CF blocked
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
        "traders": list(_TRADERS.keys()),
        "trader_types": _TRADER_TYPES,
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

    def _counted_push(kind: str, data, trader: str = None):
        _status["pushes"] += 1
        logger.info("push_fn called: kind=%s trader=%s pushes=%d", kind, trader, _status["pushes"])
        push_fn(kind, data, trader)

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


# ── History polling ───────────────────────────────────────────────────────────

async def _active_poll(page, push_fn: Callable):
    logger.info("Polling APIs via page.evaluate...")

    # ── CFD open positions probe (global, usually 403 but kept for discovery) ──
    if PORTFOLIO_ID:
        pos_probes = []
        for label, body in [
            ("empty",          {}),
            ("portfolioId",    {"portfolioId": PORTFOLIO_ID}),
            ("followId",       {"followPortfolioId": PORTFOLIO_ID}),
        ]:
            try:
                result = await page.evaluate("""async ([body]) => {
                    try {
                        const r = await fetch('/v1/trace/mt5/trace/getFollowOpenPosition', {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(body),
                        });
                        const text = await r.text();
                        if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                        const j = JSON.parse(text);
                        return {status: r.status, code: j?.code, msg: j?.msg,
                                data_keys: j?.data != null ? Object.keys(Object(j.data)).slice(0,8) : null};
                    } catch(e) { return {status: 0, error: String(e)}; }
                }""", [body])
                code = result.get("code") if isinstance(result, dict) else None
                entry = {"body": label, "status": result.get("status"), "code": code,
                         "error": result.get("error"), "data_keys": result.get("data_keys")}
                pos_probes.append(entry)
                if isinstance(result, dict) and result.get("status") == 200 and code in ("00000", "200", "0"):
                    logger.info("CFD positions found body=%s keys=%s", label, result.get("data_keys"))
                    push_fn("positions", result.get("data") or {})
                    break
            except Exception as ex:
                pos_probes.append({"body": label, "error": str(ex)})
        _status["last_pos_response"] = pos_probes[0] if pos_probes else {}
        _status["last_pos_probes"] = pos_probes

    # ── History per trader, branching on type ────────────────────────────────
    for trader_name, pid in _TRADERS.items():
        ttype = _TRADER_TYPES.get(trader_name, "cfd")
        logger.info("Polling history: trader=%s type=%s pid=%s", trader_name, ttype, pid)
        if ttype == "futures":
            await _poll_futures_history(page, push_fn, trader_name, pid)
        else:
            await _poll_cfd_history(page, push_fn, trader_name, pid)

    _status["last_poll"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["polls"] += 1


async def _poll_cfd_history(page, push_fn: Callable, trader_name: str, pid: str):
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
        }""", pid)
        api_code = (hist.get("data") or {}).get("code")
        api_msg  = (hist.get("data") or {}).get("msg")
        logger.info("CFD history[%s]: HTTP %s code=%s err=%s",
                    trader_name, hist.get("status"), api_code, hist.get("error"))
        _status["last_hist_response"] = {
            "trader": trader_name, "type": "cfd", "http": hist.get("status"),
            "code": api_code, "msg": api_msg, "error": hist.get("error"),
        }
        if hist.get("status") == 200 and api_code in ("00000", "200", "0"):
            _status["auth_ok"] = True
            push_fn("history", hist["data"], trader_name)
        elif hist.get("error") == "html_redirect":
            _status["auth_ok"] = False
    except Exception as e:
        logger.warning("CFD history[%s] error: %s", trader_name, e)


async def _poll_futures_history(page, push_fn: Callable, trader_name: str, pid: str):
    """Probe multiple endpoint patterns for futures copy trading history."""
    probes = [
        # Most likely — mirrors the MT5/CFD pattern exactly
        ("/v1/trace/future/trace/positionHistory",
         {"portfolioId": pid, "pageNo": 1, "pageSize": 50}),
        # Bitget v2 mix (USDT perpetual) follower history
        ("/api/v2/copy/mix-follower/history-orders",
         {"portfolioId": pid, "pageNo": "1", "pageSize": "50"}),
        # Alternative v1 futures path
        ("/v1/copy/futures/follow/closePosition/list",
         {"portfolioId": pid, "pageNo": 1, "pageSize": 50}),
    ]
    results = []
    for ep, body in probes:
        try:
            result = await page.evaluate("""async ([ep, body]) => {
                try {
                    const r = await fetch(ep, {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, code: j?.code, msg: j?.msg, data: j?.data,
                            data_keys: j?.data ? Object.keys(Object(j.data)).slice(0,8) : null};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [ep, body])
            code = result.get("code") if isinstance(result, dict) else None
            ep_short = ep.split("/")[-1]
            logger.info("Futures history[%s] %s: HTTP %s code=%s keys=%s err=%s",
                        trader_name, ep_short, result.get("status"), code,
                        result.get("data_keys"), result.get("error"))
            results.append({"ep": ep_short, "http": result.get("status"), "code": code,
                             "error": result.get("error"), "data_keys": result.get("data_keys")})
            if result.get("error") == "html_redirect":
                _status["auth_ok"] = False
                continue
            if result.get("status") == 200 and code in ("00000", "200", "0"):
                _status["auth_ok"] = True
                push_fn("history", result["data"], trader_name)
                _status[f"futures_hist_{trader_name}"] = results
                return  # found working endpoint
        except Exception as e:
            ep_short = ep.split("/")[-1]
            logger.warning("Futures history[%s] %s error: %s", trader_name, ep_short, e)
            results.append({"ep": ep_short, "error": str(e)})
    _status[f"futures_hist_{trader_name}"] = results


# ── Balance / portfolio polling ───────────────────────────────────────────────

async def _fetch_balance(page, push_fn: Callable):
    cfd_traders   = {n: p for n, p in _TRADERS.items() if _TRADER_TYPES.get(n, "cfd") == "cfd"}
    fut_traders   = {n: p for n, p in _TRADERS.items() if _TRADER_TYPES.get(n, "cfd") == "futures"}

    if cfd_traders:
        await _fetch_cfd_balances(page, push_fn, cfd_traders)

    for trader_name, pid in fut_traders.items():
        await _fetch_futures_balance(page, push_fn, trader_name, pid)


async def _fetch_cfd_balances(page, push_fn: Callable, cfd_traders: dict):
    """Fetch balance for CFD traders via getFollowPortfolios (all-at-once, then per-trader)."""
    pid_set = set(cfd_traders.values())

    # Try all-at-once first (empty body — may return all CFD portfolios)
    try:
        result = await page.evaluate("""async () => {
            try {
                const r = await fetch('/v1/trace/mt5/trace/getFollowPortfolios', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({}),
                });
                const text = await r.text();
                if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                const j = JSON.parse(text);
                return {status: r.status, code: j?.code, data: j?.data};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""")

        code = result.get("code") if isinstance(result, dict) else None
        _status["last_balance_probes"] = {"getFollowPortfolios_all": {
            "http": result.get("status"), "code": code, "error": result.get("error")}}

        if result.get("error") == "html_redirect":
            _status["auth_ok"] = False
            logger.warning("CFD getFollowPortfolios all: html_redirect — cookie expired or CF blocked")
        elif result.get("status") == 200 and code in ("00000", "200", "0"):
            _status["auth_ok"] = True
            details = (result.get("data") or {}).get("portfolioDetails") or []
            matched = 0
            for portfolio in details:
                if not isinstance(portfolio, dict):
                    continue
                pid = str(portfolio.get("portfolioId") or portfolio.get("followPortfolioId") or "")
                trader_name = _pid_to_name.get(pid)
                if trader_name and pid in pid_set:
                    logger.info("CFD getFollowPortfolios all: matched trader=%s balance=%s",
                                trader_name, portfolio.get("balance"))
                    push_fn("copy_details", portfolio, trader_name)
                    matched += 1
            if matched > 0:
                _status["scrapes"] += 1
                _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                return
            logger.warning("CFD getFollowPortfolios all: %d details, none matched pids %s",
                           len(details), list(pid_set))
    except Exception as e:
        logger.warning("CFD getFollowPortfolios all error: %s", e)

    # Fallback: per-trader
    logger.info("CFD: falling back to per-trader getFollowPortfolios")
    for trader_name, pid in cfd_traders.items():
        try:
            result = await page.evaluate("""async (pid) => {
                try {
                    const r = await fetch('/v1/trace/mt5/trace/getFollowPortfolios', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({portfolioId: pid}),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, code: j?.code, data: j?.data};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", pid)

            code = result.get("code") if isinstance(result, dict) else None
            _status["last_balance_probes"][f"cfd_{trader_name}"] = {
                "http": result.get("status"), "code": code, "error": result.get("error")}

            if result.get("error") == "html_redirect":
                _status["auth_ok"] = False
            elif result.get("status") == 200 and code in ("00000", "200", "0"):
                _status["auth_ok"] = True
                details = (result.get("data") or {}).get("portfolioDetails") or []
                if details and isinstance(details[0], dict):
                    portfolio = details[0]
                    logger.info("CFD getFollowPortfolios[%s]: balance=%s investment=%s",
                                trader_name, portfolio.get("balance"), portfolio.get("totalInvestment"))
                    push_fn("copy_details", portfolio, trader_name)
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status["scrapes"] += 1
            else:
                logger.warning("CFD getFollowPortfolios[%s] failed: http=%s code=%s",
                               trader_name, result.get("status"), code)
        except Exception as e:
            logger.warning("CFD getFollowPortfolios[%s] error: %s", trader_name, e)


async def _fetch_futures_balance(page, push_fn: Callable, trader_name: str, pid: str):
    """Probe multiple endpoint patterns for futures copy trading portfolio balance."""
    probes = [
        # Most likely — mirrors MT5/CFD pattern
        ("/v1/trace/future/trace/getFollowPortfolios",
         {"portfolioId": pid}),
        # Bitget v2 mix follower settings / account info
        ("/api/v2/copy/mix-follower/settings",
         {"portfolioId": pid}),
        ("/api/v2/copy/mix-follower/query-settings",
         {"portfolioId": pid}),
    ]
    results = []
    for ep, body in probes:
        try:
            result = await page.evaluate("""async ([ep, body]) => {
                try {
                    const r = await fetch(ep, {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, code: j?.code, data: j?.data,
                            data_keys: j?.data ? Object.keys(Object(j.data)).slice(0,8) : null};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [ep, body])
            code = result.get("code") if isinstance(result, dict) else None
            ep_short = ep.split("/")[-1]
            logger.info("Futures balance[%s] %s: HTTP %s code=%s keys=%s err=%s",
                        trader_name, ep_short, result.get("status"), code,
                        result.get("data_keys"), result.get("error"))
            results.append({"ep": ep_short, "http": result.get("status"), "code": code,
                             "error": result.get("error"), "data_keys": result.get("data_keys")})
            if result.get("error") == "html_redirect":
                _status["auth_ok"] = False
                continue
            if result.get("status") == 200 and code in ("00000", "200", "0"):
                _status["auth_ok"] = True
                data = result.get("data") or {}
                # Data might be a dict directly, or wrapped in portfolioDetails
                details = data.get("portfolioDetails")
                if isinstance(details, list) and details:
                    push_fn("copy_details", details[0], trader_name)
                elif isinstance(data, dict) and data:
                    push_fn("copy_details", data, trader_name)
                _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                _status["scrapes"] += 1
                _status[f"futures_balance_{trader_name}"] = results
                return
        except Exception as e:
            ep_short = ep.split("/")[-1]
            logger.warning("Futures balance[%s] %s error: %s", trader_name, ep_short, e)
            results.append({"ep": ep_short, "error": str(e)})
    _status[f"futures_balance_{trader_name}"] = results
    logger.warning("Futures balance[%s]: all endpoints failed — check debug", trader_name)
