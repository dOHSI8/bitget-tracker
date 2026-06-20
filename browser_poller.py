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

# Trade history is persisted server-side (history.json) and merged on each push.
# So the poller only needs the latest page most cycles; a full 30-day backfill
# runs once per process (after startup / redeploy) and periodically thereafter.
_history_full_done: set[str] = set()
_HISTORY_FULL_EVERY = 120   # full re-scan every N cycles (~60 min at 30s interval)

_status = {
    "running": False,
    "browser_alive": False,
    "last_poll": None,
    "last_scrape": None,
    "last_scrape_ms": None,
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
        name0 = os.environ.get("TRADER_NAME", "TraderName")
        traders[name0] = os.environ.get("PORTFOLIO_ID", "YOUR_PORTFOLIO_ID")
        types[name0]   = "cfd"
    return traders, types


def _mark_scrape() -> None:
    now = datetime.now(BKK)
    _status["last_scrape"] = now.strftime("%Y-%m-%d %H:%M:%S")
    _status["last_scrape_ms"] = int(now.timestamp() * 1000)
    _status["scrapes"] += 1


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

            # Phase 1: /about warmup only (Cloudflare bypass).
            # Probe open positions immediately after — this is the fastest-changing
            # data so we want it as early as possible in the cycle.
            try:
                await page.goto(f"{BITGET_BASE}/about",
                                wait_until="domcontentloaded", timeout=30_000)
                logger.info("Warm-up nav OK: /about")
            except Exception as e:
                logger.info("Warm-up nav /about: %s", e)

            await _probe_positions(page, push_fn, traders, trader_types)

            # Phase 2: navigate to each CFD portfolio page to seed CFD-specific
            # cookies needed for balance / history API calls.
            # Futures-center pages are intentionally skipped — navigating there
            # triggers CF bot-detection that blocks /v1/trace/future/... with 403.
            for tname, pid in traders.items():
                if trader_types.get(tname, "cfd") == "cfd":
                    path = f"/copy-trading/cfd-center/my-portfolio/{pid}"
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


# ── Positions probe (runs early, right after /about warmup) ──────────────────

async def _probe_positions(page, push_fn: Callable,
                           traders: dict[str, str], trader_types: dict[str, str]):
    """Probe open positions as early as possible in the poll cycle.

    Called right after the /about Cloudflare warmup, before the heavier
    portfolio-page navigation.  tracePosition typically works without the
    portfolio-page cookie, so this gets position data ~15-20 s earlier than
    if we waited for the full warmup sequence.

    Stops probing on the FIRST 200 response (any code).  A 200/00004 means
    "no open positions but cookie is alive" — still calls _mark_scrape() so
    the dashboard age-timer resets even when there is nothing to push.
    Stops immediately on a browser/page-crash exception to avoid wasting time
    on probes that will all fail.
    """
    cfd_pids = [p for n, p in traders.items() if trader_types.get(n, "cfd") == "cfd"]
    if not cfd_pids:
        return
    pid0 = cfd_pids[0]
    pos_probes = []
    done = False   # set True to exit both loops

    for ep in [
        "/v1/trace/mt5/data/tracePosition",       # confirmed 200+rows
        "/v1/trace/mt5/trace/getFollowOpenPosition",
        "/v1/trace/mt5/trace/myFollowOpenPosition",
        "/v1/trace/mt5/trace/getFollowOpenOrder",
    ]:
        if done:
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
                msg  = (result.get("msg") or "") if isinstance(result, dict) else ""
                pos_probes.append({"ep": ep, "body": label, "status": result.get("status"),
                                   "code": code, "msg": msg or None,
                                   "rows": result.get("row_count"),
                                   "error": result.get("error")})
                if isinstance(result, dict) and result.get("status") == 200:
                    msg_lower = msg.lower()
                    if "expired" in msg_lower or ("log" in msg_lower and "in" in msg_lower):
                        # Bitget returns 200/00004 with this msg when the session
                        # has expired — not a genuine "no positions" response.
                        logger.info("CFD position probe: session expired (msg=%s)", msg)
                        _status["auth_ok"] = False
                        done = True
                        break
                    # 200 without expiry msg — cookie is alive
                    _status["auth_ok"] = True
                    _mark_scrape()
                    if code in ("00000", "200", "0"):
                        logger.info("CFD positions found ep=%s body=%s rows=%s",
                                    ep, label, result.get("row_count"))
                        push_fn("positions", result.get("data") or {})
                    else:
                        logger.info("CFD positions probe 200/%s (no positions) ep=%s body=%s",
                                    code, ep, label)
                    done = True   # cookie confirmed — no need to probe more endpoints
                    break
            except Exception as ex:
                err_str = str(ex)
                pos_probes.append({"ep": ep, "body": label, "error": err_str})
                # Page/browser crashed — stop all probing; remaining calls will
                # fail too and history/balance will be skipped this cycle.
                if "closed" in err_str.lower() or "browser" in err_str.lower():
                    done = True
                    break
    _status["last_pos_response"] = pos_probes[0] if pos_probes else {}
    _status["last_pos_probes"] = pos_probes


# ── History polling ───────────────────────────────────────────────────────────

async def _active_poll(page, push_fn: Callable,
                       traders: dict[str, str], trader_types: dict[str, str]):
    logger.info("Polling history/balance via page.evaluate... traders=%s", list(traders.keys()))

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
    Fetch closed position history.

    Server-side history.json is the source of truth — it merges and dedupes
    every push. So most cycles only fetch page 1 (the latest 50 closed trades),
    which is more than enough to catch newly-settled trades at a 30s interval.

    A full backfill (all pages) runs only:
      • the first time we poll this trader after process start / redeploy
      • every _HISTORY_FULL_EVERY cycles thereafter (gap recovery)

    The API caps each response at 50 rows regardless of pageSize.
    We walk backwards using endTime = oldest close time of previous batch.
    """
    polls = _status.get("polls", 0)
    need_full = (trader_name not in _history_full_done) or (polls % _HISTORY_FULL_EVERY == 0)
    max_batches = 200 if need_full else 1

    cutoff_ms = int((datetime.now(BKK) - timedelta(days=365)).timestamp() * 1000)
    all_rows: list = []
    end_time_ms: int | None = None
    API_PAGE_CAP = 50
    prev_oldest: int | None = None

    try:
        for batch in range(max_batches):
            body: dict = {"portfolioId": pid, "pageSize": API_PAGE_CAP}
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
            if batch == 0:
                _mark_scrape()
            rows = _extract_rows(hist.get("data") or {})
            if not rows:
                break

            all_rows.extend(rows)
            oldest = _oldest_close_ms(rows)

            if oldest is not None and oldest == prev_oldest:
                logger.info("CFD history[%s]: endTime ignored by API, stopping at batch %d",
                            trader_name, batch + 1)
                break
            prev_oldest = oldest

            if oldest and oldest < cutoff_ms:
                break
            if not oldest:
                break
            # Note: do NOT break on len(rows) < API_PAGE_CAP.
            # The API sometimes returns a short batch at a time-window boundary
            # even when older trades exist — stopping early would miss history.
            end_time_ms = oldest - 1

        if all_rows:
            mode = "full" if need_full else "page1"
            logger.info("CFD history[%s]: %d trades across %d batches (%s)",
                        trader_name, len(all_rows), batch + 1, mode)
            push_fn("history", {"code": "200", "data": {"rows": all_rows}}, trader_name)
            if need_full:
                _history_full_done.add(trader_name)

    except Exception as e:
        logger.warning("CFD history[%s] error: %s", trader_name, e)


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

    await _fetch_elite_trader(page, push_fn)


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
                _mark_scrape()
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
                    _mark_scrape()
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
                    _mark_scrape()
                    _status[f"futures_balance_{trader_name}"] = results
                    break  # found data, stop probing
                elif isinstance(pos_list, list) and pos_list:
                    total_upl = sum(float(p.get("profit") or p.get("unrealizedPnl") or
                                         p.get("unrealizedPL") or 0) for p in pos_list
                                    if isinstance(p, dict))
                    push_fn("copy_details", {"floatProfit": total_upl}, trader_name)
                    _mark_scrape()
                    _status[f"futures_balance_{trader_name}"] = results
                    break  # found data, stop probing
                elif isinstance(data, dict) and data:
                    push_fn("copy_details", data, trader_name)
                    _mark_scrape()
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

# Fields unique to cancelled copy summary rows (not present in active portfolios)
_CANCEL_KEYS = {"netProfit", "estNetProfit", "stopTime", "cancelTime",
                "traderNickName", "profitSharingAmount", "copyProfit", "cancelReason"}
# Fields that indicate active/live portfolio data — if present, row is NOT a cancelled copy
_ACTIVE_KEYS = {"marginCall", "marginFree", "credit", "connecting"}


def _extract_rows(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Direct keys first
        direct = data.get("rows") or data.get("list") or data.get("portfolioDetails")
        if direct:
            return direct
        # Unwrap one level: full API response {code, msg, data: {rows: [...]}}
        inner = data.get("data")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            return inner.get("rows") or inner.get("list") or inner.get("portfolioDetails") or []
    return []


def _oldest_close_ms(rows: list) -> int | None:
    """Return the oldest closeTime (ms) from a batch of history rows, or None."""
    oldest = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        for key in ("closeTime", "closedAt", "closeTs", "ctime"):
            v = r.get(key)
            if v:
                try:
                    t = int(v)
                    ms = t * 1000 if t < 10_000_000_000 else t
                    if oldest is None or ms < oldest:
                        oldest = ms
                except (TypeError, ValueError):
                    pass
    return oldest


_FOLLOW_HISTORY_EP   = "/v1/trace/mt5/trace/getFollowHistory"
_FOLLOW_HISTORY_BODY = {"pageNo": 1, "pageSize": 50}


async def _fetch_cancelled_copies(page, push_fn: Callable):
    """Fetch stopped copy-trading portfolio summaries via getFollowHistory.

    Confirmed working endpoint: POST /v1/trace/mt5/trace/getFollowHistory
    Returns rows with keys: netProfit, stopTime, traderName, portfolioId, etc.
    netProfit is already net of profit share, so we sum it directly.
    """
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
                const rows = j?.data?.rows || j?.data?.list || j?.data?.portfolioDetails || [];
                return {status: r.status, code: j?.code, rows: rows.length,
                        row0_keys: rows[0] ? Object.keys(rows[0]).slice(0, 15) : null,
                        data: j?.data};
            } catch(e) { return {status: 0, error: String(e)}; }
        }""", [_FOLLOW_HISTORY_EP, _FOLLOW_HISTORY_BODY])
    except Exception as e:
        _status["cancelled_copies_probe"] = {"error": str(e)}
        logger.warning("_fetch_cancelled_copies error: %s", e)
        return

    http = result.get("status") if isinstance(result, dict) else 0
    code = result.get("code")   if isinstance(result, dict) else None
    err  = result.get("error")  if isinstance(result, dict) else None

    _status["cancelled_copies_probe"] = {
        "ep": _FOLLOW_HISTORY_EP, "http": http, "code": code,
        "rows": result.get("rows", 0) if isinstance(result, dict) else 0,
        "error": err,
    }

    if err == "html_redirect":
        _status["auth_ok"] = False
        return
    if http != 200 or code not in ("00000", "200", "0"):
        logger.warning("getFollowHistory: http=%s code=%s", http, code)
        return

    rows = _extract_rows(result.get("data"))
    if not rows:
        return

    push_fn("cancelled_copies", rows)
    logger.info("Cancelled copies: %d rows from getFollowHistory", len(rows))


# ── Elite (lead) trader portfolio ─────────────────────────────────────────────

# (method, endpoint) — tries both POST and GET for promising paths
_ELITE_PROBES: list[tuple[str, str]] = [
    # ── Same trace/mt5 base, broader name variations ────────────────────────
    ("POST", "/v1/trace/mt5/trace/getTraderPortfolios"),
    ("POST", "/v1/trace/mt5/trace/getTraderPortfolio"),
    ("POST", "/v1/trace/mt5/trace/getMyTraderPortfolio"),
    ("POST", "/v1/trace/mt5/trace/getSelfTraderPortfolio"),
    ("POST", "/v1/trace/mt5/trace/getEliteTraderPortfolio"),
    ("POST", "/v1/trace/mt5/trace/getEliteCenterPortfolio"),
    ("GET",  "/v1/trace/mt5/trace/getTraderPortfolio"),
    ("GET",  "/v1/trace/mt5/trace/getMyTraderPortfolio"),
    ("GET",  "/v1/trace/mt5/trace/getEliteTraderPortfolio"),
    # ── elite / eliteCenter sub-path ─────────────────────────────────────────
    ("POST", "/v1/trace/mt5/elite/getPortfolio"),
    ("POST", "/v1/trace/mt5/elite/portfolio"),
    ("POST", "/v1/trace/mt5/elite/getTraderPortfolio"),
    ("GET",  "/v1/trace/mt5/elite/getPortfolio"),
    ("GET",  "/v1/trace/mt5/elite/portfolio"),
    ("POST", "/v1/trace/mt5/eliteCenter/getPortfolio"),
    ("POST", "/v1/trace/mt5/eliteCenter/portfolio"),
    ("POST", "/v1/trace/mt5/eliteCenter/getTraderPortfolio"),
    ("GET",  "/v1/trace/mt5/eliteCenter/getPortfolio"),
    # ── trader/ sub-path (both methods) ──────────────────────────────────────
    ("POST", "/v1/trace/mt5/trader/getPortfolio"),
    ("POST", "/v1/trace/mt5/trader/portfolio"),
    ("POST", "/v1/trace/mt5/trader/detail"),
    ("GET",  "/v1/trace/mt5/trader/getPortfolio"),
    ("GET",  "/v1/trace/mt5/trader/portfolio"),
    # ── cfd-center paths (like /cfd-center/followers for cancelled copies) ───
    ("POST", "/v1/trace/mt5/cfdCenter/getTraderPortfolio"),
    ("POST", "/v1/trace/mt5/cfdCenter/getElitePortfolio"),
    ("GET",  "/v1/trace/mt5/cfdCenter/getTraderPortfolio"),
    # ── Top-level /v1/copy/ paths ─────────────────────────────────────────────
    ("POST", "/v1/copy/mt5/getTraderPortfolio"),
    ("POST", "/v1/copy/mt5/getElitePortfolio"),
    ("POST", "/v1/copy/mt5/eliteCenter"),
    ("POST", "/v1/copy/mt5/getMyTraderPortfolio"),
    ("GET",  "/v1/copy/mt5/getTraderPortfolio"),
    ("GET",  "/v1/copy/mt5/getElitePortfolio"),
    # ── /v1/elite/ top-level ─────────────────────────────────────────────────
    ("POST", "/v1/elite/mt5/getPortfolio"),
    ("POST", "/v1/elite/mt5/portfolio"),
    ("GET",  "/v1/elite/mt5/getPortfolio"),
    ("POST", "/v1/elite/getPortfolio"),
    ("GET",  "/v1/elite/getPortfolio"),
    # ── /v1/mix/ (futures-style base, sometimes shared) ──────────────────────
    ("POST", "/v1/mix/trace/getTraderPortfolio"),
    ("POST", "/v1/mix/trace/getElitePortfolio"),
    ("GET",  "/v1/mix/trace/getTraderPortfolio"),
]
# Keys visible in the screenshot — any one of these indicates we have elite trader data
_ELITE_KEYS = {"aum", "copiersPnl", "copiers", "followerCount", "followCount",
               "currentFollowers", "totalProfit", "cumulativeProfit",
               "traderBalance", "eliteBalance", "leaderBalance",
               "equity", "unrealizedProfitShare", "roi"}
_elite_ep:     str | None = None
_elite_method: str        = "POST"
_ELITE_REPROBE_EVERY = 30   # full sweep is ~40 requests; only run it occasionally
_elite_probe_count = 0


async def _fetch_elite_trader(page, push_fn: Callable):
    """Find and fetch the user's own elite (lead) trader portfolio balance."""
    global _elite_ep, _elite_method, _elite_probe_count

    if _elite_ep:
        probes = [(_elite_method, _elite_ep)]
    else:
        # No known endpoint: run the full sweep on the first poll, then only
        # every _ELITE_REPROBE_EVERY polls — not 40 requests per cycle forever.
        sweep_due = _elite_probe_count % _ELITE_REPROBE_EVERY == 0
        _elite_probe_count += 1
        if not sweep_due:
            return
        probes = _ELITE_PROBES
    using_cache = _elite_ep is not None
    hits: list[dict] = []   # non-404 results for debugging

    for method, ep in probes:
        try:
            result = await page.evaluate("""async ([method, ep]) => {
                try {
                    const opts = {method, credentials: 'include',
                                  headers: {'Content-Type': 'application/json'}};
                    if (method === 'POST') opts.body = JSON.stringify({});
                    const r = await fetch(ep, opts);
                    const text = await r.text();
                    if (text.trimStart().startsWith('<')) return {status: r.status, error: 'html_redirect'};
                    const j = JSON.parse(text);
                    const d = j?.data;
                    const row = Array.isArray(d)
                        ? d[0]
                        : (d?.portfolioDetails?.[0] ?? d?.list?.[0] ?? d?.rows?.[0] ?? d);
                    return {status: r.status, code: j?.code, msg: j?.msg,
                            keys: row && typeof row === 'object' ? Object.keys(row).slice(0, 25) : null,
                            row: row};
                } catch(e) { return {status: 0, error: String(e)}; }
            }""", [method, ep])
        except Exception as e:
            logger.warning("Elite probe %s %s error: %s", method, ep, e)
            continue

        http = result.get("status") if isinstance(result, dict) else 0
        code = result.get("code")   if isinstance(result, dict) else None
        err  = result.get("error")  if isinstance(result, dict) else None

        if err == "html_redirect":
            _status["auth_ok"] = False
            break
        if http == 404:
            continue

        # Log every non-404 so we can see what's there
        hits.append({"method": method, "ep": ep, "http": http, "code": code,
                     "msg": result.get("msg") if isinstance(result, dict) else None,
                     "keys": result.get("keys") if isinstance(result, dict) else None})
        logger.info("Elite probe non-404: %s %s http=%s code=%s keys=%s",
                    method, ep, http, code,
                    result.get("keys") if isinstance(result, dict) else None)

        if http == 200 and code in ("00000", "200", "0"):
            row = result.get("row") if isinstance(result, dict) else None
            if not isinstance(row, dict):
                continue
            row_keys = set(row.keys())
            # Active follower portfolios also carry equity/totalProfit-style keys;
            # accepting one would double-count a balance already on a trader card.
            if (_ELITE_KEYS & row_keys) and not (_ACTIVE_KEYS & row_keys):
                _elite_ep     = ep
                _elite_method = method
                _status["elite_probe"] = {"found": True, "method": method, "ep": ep,
                                          "http": http, "code": code,
                                          "keys": list(row_keys)[:25], "hits": hits}
                push_fn("elite_trader", row)
                logger.info("Elite trader FOUND: %s %s balance=%s equity=%s",
                            method, ep, row.get("balance"), row.get("equity"))
                return
            elif row_keys:
                logger.info("Elite probe 200 wrong shape: %s %s keys=%s",
                            method, ep, list(row_keys)[:20])

    if using_cache:
        # Cached endpoint stopped returning usable data — clear it and let the
        # next poll run a full sweep to rediscover.
        _elite_ep = None
        _elite_probe_count = 0
    _status["elite_probe"] = {"found": False, "hits": hits}

