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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "30"))
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))
TRADERS_FILE = Path(os.environ.get("TRADERS_PATH", "traders.json"))

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


def _load_traders() -> tuple[dict[str, str], dict[str, str]]:
    """Load traders each poll cycle so runtime changes (add/remove via dashboard) take effect.
    Returns (traders {name: portfolioId}, types {name: "cfd"|"futures"}).
    Priority: traders.json  >  TRADERS env var  >  legacy PORTFOLIO_ID env var.
    """
    if TRADERS_FILE.exists():
        try:
            data = json.loads(TRADERS_FILE.read_text())
            entries = data.get("traders", [])
            if entries:
                traders = {t["name"]: t["id"] for t in entries}
                types   = {t["name"]: t.get("type", "cfd") for t in entries}
                return traders, types
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    traders: dict[str, str] = {}
    types:   dict[str, str] = {}
    env = os.environ.get("TRADERS", "")
    if env:
        for item in env.split(","):
            parts = item.strip().split(":")
            if len(parts) >= 2:
                name  = parts[0].strip()
                pid   = parts[1].strip()
                ttype = parts[2].strip() if len(parts) >= 3 else "cfd"
                traders[name] = pid
                types[name]   = ttype
    else:
        name0 = os.environ.get("TRADER_NAME", "DKTrading")
        traders[name0] = os.environ.get("PORTFOLIO_ID", "1443199880395776000")
        types[name0]   = "cfd"
    return traders, types


def reset_auth_status() -> None:
    """Call when a new cookie is saved so stale auth_ok=False doesn't persist."""
    _status["auth_ok"] = None
    _status["last_error"] = None


def get_status() -> dict:
    cookie_str = _load_cookie_string()
    traders, trader_types = _load_traders()
    return {
        **_status,
        "has_cookie": bool(cookie_str),
        "cookie_preview": (cookie_str[:40] + "...") if len(cookie_str) > 40 else cookie_str,
        "poll_interval_sec": POLL_INTERVAL,
        "traders": list(traders.keys()),
        "trader_types": trader_types,
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

        # Reload traders each cycle — picks up any add/remove done via dashboard
        traders, trader_types = _load_traders()
        if not traders:
            _status["last_error"] = "No traders configured"
            await asyncio.sleep(30)
            continue

        _status["last_error"] = None
        try:
            await _poll_once(_counted_push, cookie_str, traders, trader_types)
        except Exception as e:
            logger.error("Poll cycle crashed: %s", e)
            _status["last_error"] = f"Poll error: {e}"

        _status["browser_alive"] = False
        logger.info("Next poll in %ds… (pushes so far: %d)", POLL_INTERVAL, _status["pushes"])
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once(push_fn: Callable, cookie_str: str,
                     traders: dict[str, str], trader_types: dict[str, str]):
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

            # Warm-up: visit /about for Cloudflare bypass, then CFD portfolio pages
            # to seed CFD-specific cookies. Futures-center pages are intentionally
            # skipped — navigating there triggers CF bot-detection that blocks all
            # subsequent /v1/trace/future/... API calls with 403.
            warmup_pages = ["/about"]
            for tname, pid in traders.items():
                ttype = trader_types.get(tname, "cfd")
                if ttype == "cfd":
                    warmup_pages.append(f"/copy-trading/cfd-center/my-portfolio/{pid}")
            for path in warmup_pages:
                try:
                    await page.goto(f"{BITGET_BASE}{path}",
                                    wait_until="domcontentloaded", timeout=30_000)
                    logger.info("Warm-up nav OK: %s", path)
                except Exception as e:
                    logger.info("Warm-up nav %s: %s", path, e)

            await _active_poll(page, push_fn, traders, trader_types)
            await _fetch_balance(page, push_fn, traders, trader_types)

            logger.info("Poll cycle complete — closing browser")
        finally:
            await browser.close()


# ── History polling ───────────────────────────────────────────────────────────

async def _active_poll(page, push_fn: Callable,
                       traders: dict[str, str], trader_types: dict[str, str]):
    logger.info("Polling APIs via page.evaluate... traders=%s", list(traders.keys()))

    # CFD open-positions probe — try multiple endpoints and body variants
    cfd_pids = [p for n, p in traders.items() if trader_types.get(n, "cfd") == "cfd"]
    if cfd_pids:
        pid0 = cfd_pids[0]
        pos_probes = []
        pos_found = False
        for ep in [
            "/v1/trace/mt5/trace/getFollowOpenPosition",
            "/v1/trace/mt5/data/tracePosition",
            "/v1/trace/mt5/trace/myFollowOpenPosition",
            "/v1/trace/mt5/trace/getFollowOpenOrder",
        ]:
            if pos_found:
                break
            for label, body in [("portfolioId", {"portfolioId": pid0}),
                                 ("followId", {"followPortfolioId": pid0}),
                                 ("empty", {})]:
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
                            const d = j?.data;
                            const rows = Array.isArray(d) ? d : (d?.list || d?.rows || d?.positions || []);
                            return {status: r.status, code: j?.code, msg: j?.msg,
                                    data: j?.data,
                                    data_type: Array.isArray(d) ? 'array' : typeof d,
                                    data_keys: d != null ? Object.keys(Object(d)).slice(0,8) : null,
                                    row_count: rows.length};
                        } catch(e) { return {status: 0, error: String(e)}; }
                    }""", [ep, body])
                    code = result.get("code") if isinstance(result, dict) else None
                    pos_probes.append({"ep": ep, "body": label, "status": result.get("status"),
                                       "code": code, "rows": result.get("row_count"),
                                       "error": result.get("error")})
                    if isinstance(result, dict) and result.get("status") == 200 and code in ("00000", "200", "0"):
                        logger.info("CFD positions found ep=%s body=%s rows=%s",
                                    ep, label, result.get("row_count"))
                        push_fn("positions", result.get("data") or {})
                        pos_found = True
                        break
                except Exception as ex:
                    pos_probes.append({"ep": ep, "body": label, "error": str(ex)})
        _status["last_pos_response"] = pos_probes[0] if pos_probes else {}
        _status["last_pos_probes"] = pos_probes

    # History per trader, branching on type
    for trader_name, pid in traders.items():
        ttype = trader_types.get(trader_name, "cfd")
        logger.info("Polling history: trader=%s type=%s", trader_name, ttype)
        if ttype == "futures":
            await _poll_futures_history(page, push_fn, trader_name, pid)
        else:
            await _poll_cfd_history(page, push_fn, trader_name, pid)

    _status["last_poll"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["polls"] += 1


async def _poll_cfd_history(page, push_fn: Callable, trader_name: str, pid: str):
    """
    Fetch up to 30 days of closed position history.

    positionHistory doesn't support pageNo > 1 — page 2 returns empty.
    Instead we use time-based cursor pagination: each batch uses
    endTime = oldest_close_ms from the previous batch.
    """
    cutoff_ms = int((datetime.now(BKK) - timedelta(days=30)).timestamp() * 1000)
    all_rows: list = []
    end_time_ms: int | None = None   # None = no filter (get latest batch first)
    PAGE_SIZE = 50
    MAX_BATCHES = 25  # safety cap (~1250 trades / 30 days)

    try:
        for batch in range(MAX_BATCHES):
            body: dict = {"portfolioId": pid, "pageSize": PAGE_SIZE}
            if end_time_ms is not None:
                body["endTime"] = end_time_ms

            hist = await page.evaluate("""async ([pid, body]) => {
                try {
                    const r = await fetch('/v1/trace/mt5/trace/positionHistory', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, data: j};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [pid, body])

            api_code = (hist.get("data") or {}).get("code")
            api_msg  = (hist.get("data") or {}).get("msg")

            if batch == 0:
                logger.info("CFD history[%s]: HTTP %s code=%s err=%s",
                            trader_name, hist.get("status"), api_code, hist.get("error"))
                _status["last_hist_response"] = {
                    "trader": trader_name, "type": "cfd",
                    "http": hist.get("status"), "code": api_code,
                    "msg": api_msg, "error": hist.get("error"),
                }

            if hist.get("error") == "html_redirect" or api_code == "00004":
                _status["auth_ok"] = False
                break

            if hist.get("status") != 200 or api_code not in ("00000", "200", "0"):
                break

            _status["auth_ok"] = True
            raw_data = hist.get("data") or {}
            rows = _extract_rows(raw_data)
            if not rows:
                break

            all_rows.extend(rows)

            oldest = _oldest_close_ms(rows)

            # Stop if we've reached trades older than 30 days
            if oldest and oldest < cutoff_ms:
                break

            # Stop if this batch was partial (no more data on server)
            if len(rows) < PAGE_SIZE:
                break

            # Advance cursor: next batch ends just before oldest trade in this one
            if not oldest:
                break
            end_time_ms = oldest - 1

        if all_rows:
            logger.info("CFD history[%s]: %d trades across %d batches, oldest=%s",
                        trader_name, len(all_rows), batch + 1,
                        datetime.fromtimestamp(
                            (_oldest_close_ms(all_rows) or 0) / 1000, tz=BKK
                        ).strftime("%Y-%m-%d") if all_rows else "?")
            push_fn("history", {"code": "200", "data": {"rows": all_rows}}, trader_name)

    except Exception as e:
        logger.warning("CFD history[%s] error: %s", trader_name, e)


def _extract_rows(raw_data: dict) -> list:
    """Pull the list of rows out of a Bitget history response."""
    d = raw_data.get("data") or {}
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("rows") or d.get("list") or d.get("data") or []
    return []


def _oldest_close_ms(rows: list) -> float | None:
    """Return the close timestamp (ms) of the oldest trade in rows, or None."""
    times = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        for key in ("closeTime", "closedAt", "closeTs", "ctime"):
            v = r.get(key)
            if v:
                try:
                    t = int(v)
                    if 0 < t < 10_000_000_000:
                        t *= 1000
                    times.append(t)
                    break
                except (TypeError, ValueError):
                    pass
    return min(times) if times else None


async def _poll_futures_history(page, push_fn: Callable, trader_name: str, pid: str):
    # (method, endpoint, params)
    probes = [
        ("POST", "/v1/trace/future/trace/positionHistory",
         {"portfolioId": pid, "pageNo": 1, "pageSize": 50}),
        ("GET",  "/api/v2/copy/mix-follower/history-orders",
         {"portfolioId": pid, "pageNo": "1", "pageSize": "50"}),
        ("POST", "/v1/copy/futures/follow/closePosition/list",
         {"portfolioId": pid, "pageNo": 1, "pageSize": 50}),
    ]
    results = []
    for method, ep, params in probes:
        try:
            result = await page.evaluate("""async ([method, ep, params]) => {
                try {
                    let url = ep;
                    let opts = {method, credentials: 'include', headers: {}};
                    if (method === 'GET') {
                        url = ep + '?' + new URLSearchParams(params).toString();
                    } else {
                        opts.headers['Content-Type'] = 'application/json';
                        opts.body = JSON.stringify(params);
                    }
                    const r = await fetch(url, opts);
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, code: j?.code, msg: j?.msg, data: j?.data,
                            data_keys: j?.data != null ? Object.keys(Object(j.data)).slice(0,8) : null};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [method, ep, params])
            code = result.get("code") if isinstance(result, dict) else None
            ep_short = ep.split("/")[-1]
            logger.info("Futures history[%s] %s: HTTP %s code=%s keys=%s err=%s",
                        trader_name, ep_short, result.get("status"), code,
                        result.get("data_keys"), result.get("error"))
            results.append({"ep": ep_short, "http": result.get("status"), "code": code,
                             "error": result.get("error"), "data_keys": result.get("data_keys")})
            if result.get("error") == "html_redirect":
                # Don't flip auth_ok — some futures probe endpoints are CF-blocked
                # regardless of cookie health. Only CFD history controls auth status.
                continue
            if result.get("status") == 200 and code in ("00000", "200", "0"):
                _status["auth_ok"] = True
                data = result.get("data")
                if data:  # only stop probing if we actually got data
                    push_fn("history", data, trader_name)
                    _status[f"futures_hist_{trader_name}"] = results
                    return
        except Exception as e:
            ep_short = ep.split("/")[-1]
            logger.warning("Futures history[%s] %s error: %s", trader_name, ep_short, e)
            results.append({"ep": ep_short, "error": str(e)})
    _status[f"futures_hist_{trader_name}"] = results


# ── Balance / portfolio polling ───────────────────────────────────────────────

async def _fetch_balance(page, push_fn: Callable,
                         traders: dict[str, str], trader_types: dict[str, str]):
    cfd_traders = {n: p for n, p in traders.items() if trader_types.get(n, "cfd") == "cfd"}
    fut_traders = {n: p for n, p in traders.items() if trader_types.get(n, "cfd") == "futures"}

    if cfd_traders:
        await _fetch_cfd_balances(page, push_fn, cfd_traders)
        await _fetch_cancelled_copies(page, push_fn)

    for trader_name, pid in fut_traders.items():
        await _fetch_futures_balance(page, push_fn, trader_name, pid)


async def _fetch_cfd_balances(page, push_fn: Callable, cfd_traders: dict):
    pid_set = set(cfd_traders.values())
    pid_to_name = {p: n for n, p in cfd_traders.items()}

    # Try all-at-once first
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

        if result.get("error") == "html_redirect" or code == "00004":
            _status["auth_ok"] = False
            logger.warning("CFD getFollowPortfolios all: auth failure code=%s", code)
        elif result.get("status") == 200 and code in ("00000", "200", "0"):
            _status["auth_ok"] = True
            details = (result.get("data") or {}).get("portfolioDetails") or []
            matched = 0
            for portfolio in details:
                if not isinstance(portfolio, dict):
                    continue
                pid = str(portfolio.get("portfolioId") or portfolio.get("followPortfolioId") or "")
                trader_name = pid_to_name.get(pid)
                if trader_name:
                    logger.info("CFD getFollowPortfolios all: matched %s balance=%s",
                                trader_name, portfolio.get("balance"))
                    push_fn("copy_details", portfolio, trader_name)
                    matched += 1
            if matched > 0:
                _status["scrapes"] += 1
                _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                return
    except Exception as e:
        logger.warning("CFD getFollowPortfolios all error: %s", e)

    # Per-trader fallback
    logger.info("CFD: per-trader getFollowPortfolios fallback")
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
                    push_fn("copy_details", details[0], trader_name)
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status["scrapes"] += 1
        except Exception as e:
            logger.warning("CFD getFollowPortfolios[%s] error: %s", trader_name, e)


async def _fetch_futures_balance(page, push_fn: Callable, trader_name: str, pid: str):
    # (method, endpoint, params)
    # GET probes send params as query string; POST probes send as JSON body.
    probes = [
        ("POST", "/v1/trace/future/trace/getFollowPortfolios",
         {"portfolioId": pid}),
        ("POST", "/v1/trace/future/trace/getFollowPortfolios",
         {"followPortfolioId": pid}),
        ("POST", "/v1/trace/future/trace/getFollowPortfolios",
         {"portfolioId": pid, "productType": "USDT-FUTURES"}),
        ("POST", "/v1/trace/future/trace/getFollowOpenPosition",
         {"portfolioId": pid}),
    ]
    results = []
    for method, ep, params in probes:
        try:
            result = await page.evaluate("""async ([method, ep, params]) => {
                try {
                    let url = ep;
                    let opts = {method, credentials: 'include', headers: {}};
                    if (method === 'GET') {
                        url = ep + '?' + new URLSearchParams(params).toString();
                    } else {
                        opts.headers['Content-Type'] = 'application/json';
                        opts.body = JSON.stringify(params);
                    }
                    const r = await fetch(url, opts);
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    return {status: r.status, code: j?.code, data: j?.data,
                            data_keys: j?.data != null ? Object.keys(Object(j.data)).slice(0,8) : null};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [method, ep, params])
            code = result.get("code") if isinstance(result, dict) else None
            ep_short = ep.split("/")[-1] + (f"[{method}]" if method == "GET" else "")
            results.append({"ep": ep_short, "http": result.get("status"), "code": code,
                             "error": result.get("error"), "data_keys": result.get("data_keys")})
            logger.info("Futures balance[%s] %s: HTTP %s code=%s keys=%s err=%s",
                        trader_name, ep_short, result.get("status"), code,
                        result.get("data_keys"), result.get("error"))
            if result.get("error") == "html_redirect":
                # Don't flip auth_ok here — some probe endpoints are legitimately
                # Cloudflare-blocked regardless of cookie health. auth_ok is set
                # only by the CFD history poll which uses a confirmed working endpoint.
                continue
            if result.get("status") == 200 and code in ("00000", "200", "0"):
                _status["auth_ok"] = True
                raw_data = result.get("data")
                data = raw_data or {}
                details = data.get("portfolioDetails") if isinstance(data, dict) else None
                # open-position list — extract unrealized PnL as open_pnl
                pos_list = (data if isinstance(data, list) else
                            data.get("list") or data.get("rows") or []) if raw_data else []
                if isinstance(details, list) and details:
                    push_fn("copy_details", details[0], trader_name)
                    _status["scrapes"] += 1
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status[f"futures_balance_{trader_name}"] = results
                    break  # found data, stop probing
                elif isinstance(pos_list, list) and pos_list:
                    total_upl = sum(float(p.get("profit") or p.get("unrealizedPnl") or
                                         p.get("unrealizedPL") or 0) for p in pos_list
                                    if isinstance(p, dict))
                    push_fn("copy_details", {"floatProfit": total_upl}, trader_name)
                    _status["scrapes"] += 1
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status[f"futures_balance_{trader_name}"] = results
                    break  # found data, stop probing
                elif isinstance(data, dict) and data:
                    push_fn("copy_details", data, trader_name)
                    _status["scrapes"] += 1
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status[f"futures_balance_{trader_name}"] = results
                    break  # found data, stop probing
                # data was empty — continue to next probe
        except Exception as e:
            ep_short = ep.split("/")[-1]
            results.append({"ep": ep_short, "error": str(e)})
    else:
        _status[f"futures_balance_{trader_name}"] = results

    # Always fetch fund flow to compute net investment from transfer history
    await _fetch_futures_fund_flow(page, push_fn, trader_name, pid)


async def _fetch_futures_fund_flow(page, push_fn: Callable, trader_name: str, pid: str):
    """Fetch /v1/trigger/uta/trace/getBalanceHistory to compute net investment.
    transferType 0 = deposit into copy trading (+), 1 = withdrawal from copy trading (-).
    Net investment = sum(type-0) - sum(type-1).
    """
    try:
        result = await page.evaluate("""async (pid) => {
            try {
                // Try with portfolioId filter first, then account-wide
                for (const body of [
                    {portfolioId: pid, pageNo: 1, pageSize: 100},
                    {pageNo: 1, pageSize: 100}
                ]) {
                    const r = await fetch('/v1/trigger/uta/trace/getBalanceHistory', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    const rows = j?.data?.rows || [];
                    if (r.ok && ['200','00000','0'].includes(j?.code) && rows.length > 0)
                        return {status: r.status, code: j?.code, rows};
                    if (!r.ok) return {status: r.status, code: j?.code, rows: []};
                }
                return {status: 0, rows: []};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""", pid)

        code = result.get("code") if isinstance(result, dict) else None
        rows = result.get("rows") or []
        logger.info("Futures fund_flow[%s]: HTTP %s code=%s rows=%d err=%s",
                    trader_name, result.get("status"), code, len(rows), result.get("error"))
        _status[f"futures_fund_flow_{trader_name}"] = {
            "http": result.get("status"), "code": code,
            "rows": len(rows), "error": result.get("error"),
        }
        if rows:
            push_fn("fund_flow", rows, trader_name)
        elif result.get("error") == "html_redirect":
            _status["auth_ok"] = False
    except Exception as e:
        logger.warning("Futures fund_flow[%s] error: %s", trader_name, e)


# ── Cancelled copy portfolios ─────────────────────────────────────────────────

_PROFIT_KEYS = {"netProfit", "estNetProfit", "copyDays", "followDays",
                "stopTime", "cancelTime", "totalProfit", "realizedPnl"}


def _extract_rows(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("rows") or data.get("list") or data.get("portfolioDetails") or []
    return []


async def _fetch_cancelled_copies(page, push_fn: Callable):
    """Fetch cancelled CFD copy portfolios for all-time PnL.

    Strategy 1 (no extra navigation): call getFollowPortfolios with various
    status-filter bodies — if Bitget uses the same endpoint with a status param,
    this works without any page navigation.

    Strategy 2 (navigate + intercept): navigate to /cfd-center/followers and
    capture the API responses the page itself makes. Click the "Canceled" tab
    to trigger its data load, then read the captured rows.
    """
    # ── Strategy 1: status-filter probes on the known-working endpoint ────────
    status_bodies = [
        {"followStatus": 2},
        {"followStatus": "2"},
        {"followStatus": "closed"},
        {"status": 2},
        {"status": "CLOSED"},
    ]
    s1_results = []
    for body in status_bodies:
        try:
            result = await page.evaluate("""async (body) => {
                try {
                    const r = await fetch('/v1/trace/mt5/trace/getFollowPortfolios', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    const rows = j?.data?.portfolioDetails || j?.data?.rows || j?.data?.list || [];
                    return {status: r.status, code: j?.code, rows: rows.length,
                            row0_keys: rows[0] ? Object.keys(rows[0]).slice(0,15) : null,
                            data: j?.data};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", body)
            code = result.get("code") if isinstance(result, dict) else None
            s1_results.append({"body": str(body), "http": result.get("status"),
                                "code": code, "rows": result.get("rows"),
                                "row0_keys": result.get("row0_keys"), "error": result.get("error")})
            logger.info("Cancelled S1 body=%s: HTTP %s code=%s rows=%s err=%s",
                        body, result.get("status"), code, result.get("rows"), result.get("error"))
            if result.get("status") == 200 and code in ("00000", "200", "0"):
                rows = _extract_rows(result.get("data"))
                if rows and _PROFIT_KEYS & set(rows[0].keys()):
                    logger.info("Cancelled copies via status filter: %d entries body=%s", len(rows), body)
                    push_fn("cancelled_copies", rows)
                    _status["cancelled_copies_probe"] = {"strategy": "S1", "results": s1_results}
                    return
        except Exception as e:
            s1_results.append({"body": str(body), "error": str(e)})

    # ── Strategy 2: navigate + response interception ──────────────────────────
    captured: list[dict] = []

    async def on_response(response):
        url = response.url
        if response.status != 200:
            return
        if not any(p in url for p in ("/trace/mt5/trace/", "/api/v2/copy/mt5", "/copy/mt5/")):
            return
        try:
            text = await response.text()
            if not text.strip().startswith("{"):
                return
            j = json.loads(text)
            if j.get("code") not in ("00000", "200", "0"):
                return
            rows = _extract_rows(j.get("data"))
            if rows and isinstance(rows[0], dict):
                ep = url.split("?")[0].split("/")[-1]
                logger.info("Nav-captured %s: %d rows keys=%s", ep, len(rows),
                            list(rows[0].keys())[:12])
                captured.append({"ep": ep, "rows": rows})
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(f"{BITGET_BASE}/copy-trading/cfd-center/followers",
                        wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)

        # Click the "Canceled" tab to trigger its data load
        clicked = False
        for sel in [
            "text=Cancel",
            "[class*='tab']:has-text('Cancel')",
            "li:has-text('Cancel')",
            "div[role='tab']:has-text('Cancel')",
            ".tab-item:has-text('Cancel')",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    logger.info("Clicked cancelled tab via selector: %s", sel)
                    clicked = True
                    await asyncio.sleep(3)
                    break
            except Exception:
                pass
        if not clicked:
            logger.info("Cancelled tab not found — captured page-load responses only")
            await asyncio.sleep(2)
    except Exception as e:
        logger.warning("Cancelled copies nav: %s", e)
    finally:
        page.remove_listener("response", on_response)

    s2_probe = [{"ep": c["ep"], "rows": len(c["rows"]),
                 "row0_keys": list(c["rows"][0].keys())[:15]} for c in captured]
    _status["cancelled_copies_probe"] = {"strategy": "S2", "s1": s1_results, "s2": s2_probe}

    if not captured:
        logger.info("Cancelled copies: S2 captured nothing")
        return

    # Prefer rows that look like copy portfolio data (have profit/days fields)
    best = None
    for c in reversed(captured):
        if _PROFIT_KEYS & set(c["rows"][0].keys()):
            best = c
            break
    if best is None:
        best = captured[-1]

    logger.info("Cancelled copies S2: %d entries from %s", len(best["rows"]), best["ep"])
    push_fn("cancelled_copies", best["rows"])
