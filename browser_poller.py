import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

BKK = timezone(timedelta(hours=7))
PORTFOLIO_ID = os.environ.get("PORTFOLIO_ID", "1443199880395776000")
BITGET_BASE = "https://www.bitget.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "120"))
COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))
IMPERSONATE = "chrome120"

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


async def start_poller(push_fn: Callable):
    _status["running"] = True
    await asyncio.sleep(3)

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
            _status["last_error"] = f"Poll error: {e}"

        _status["browser_alive"] = False
        logger.info("Next poll in %ds...", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once(push_fn: Callable, cookie_str: str):
    headers = {
        "Cookie": cookie_str,
        "Referer": f"https://www.bitget.com/copy-trading/mt5/follower/detail?portfolioId={PORTFOLIO_ID}",
        "Origin": "https://www.bitget.com",
    }
    async with AsyncSession(impersonate=IMPERSONATE) as s:
        _status["browser_alive"] = True
        await _active_poll(s, headers, push_fn)
        await _fetch_balance(s, headers, push_fn)
        logger.info("Poll cycle complete")


async def _active_poll(s: AsyncSession, headers: dict, push_fn: Callable):
    logger.info("Polling APIs...")

    try:
        r = await s.post(
            f"{BITGET_BASE}/v1/trace/mt5/data/tracePosition",
            json={"portfolioId": PORTFOLIO_ID},
            headers=headers,
        )
        data = _safe_json(r)
        logger.info("Positions: HTTP %s code=%s", r.status_code, data.get("code", "?") if data else "err")
        if r.status_code == 200 and data:
            push_fn("positions", data)
    except Exception as e:
        logger.warning("Poll positions error: %s", e)

    try:
        r = await s.post(
            f"{BITGET_BASE}/v1/trace/mt5/trace/positionHistory",
            json={"portfolioId": PORTFOLIO_ID, "pageNo": 1, "pageSize": 50},
            headers=headers,
        )
        data = _safe_json(r)
        logger.info("History: HTTP %s code=%s", r.status_code, data.get("code", "?") if data else "err")
        if r.status_code == 200 and data:
            push_fn("history", data)
    except Exception as e:
        logger.warning("Poll history error: %s", e)

    for ep in [
        "/v1/trace/mt5/trace/balanceHistory",
        "/v1/trace/mt5/data/balanceHistory",
        "/v1/trace/mt5/trace/fundFlow",
    ]:
        try:
            r = await s.post(
                f"{BITGET_BASE}{ep}",
                json={"portfolioId": PORTFOLIO_ID, "pageNo": 1, "pageSize": 100},
                headers=headers,
            )
            if r.status_code != 200:
                continue
            j = _safe_json(r) or {}
            rows = j.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("rows") or rows.get("list") or []
            if isinstance(rows, list) and rows:
                logger.info("Polled balance_history from %s", ep)
                push_fn("balance_history", rows)
                break
        except Exception:
            pass

    _status["last_poll"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    _status["polls"] += 1


async def _fetch_balance(s: AsyncSession, headers: dict, push_fn: Callable):
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
            r = await s.get(f"{BITGET_BASE}{ep}", headers=headers)
            if r.status_code != 200:
                logger.info("Balance GET %s → HTTP %s", ep.split("?")[0].split("/")[-1], r.status_code)
                continue
            result = _safe_json(r) or {}
            d = result.get("data", result) if isinstance(result, dict) else result
            if isinstance(d, dict):
                if any("balance" in k.lower() or "equity" in k.lower() for k in d):
                    logger.info("Found balance via GET %s", ep.split("?")[0].split("/")[-1])
                    push_fn("copy_details", d)
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status["scrapes"] += 1
                    return
                logger.info("Balance GET %s → keys: %s", ep.split("?")[0].split("/")[-1], list(d.keys())[:8])
        except Exception as e:
            logger.warning("Balance GET %s error: %s", ep.split("?")[0].split("/")[-1], e)

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
            r = await s.post(
                f"{BITGET_BASE}{ep}",
                json={"portfolioId": PORTFOLIO_ID},
                headers=headers,
            )
            if r.status_code != 200:
                logger.info("Balance POST %s → HTTP %s", ep.split("/")[-1], r.status_code)
                continue
            result = _safe_json(r) or {}
            d = result.get("data", result) if isinstance(result, dict) else result
            if isinstance(d, dict):
                if any("balance" in k.lower() or "equity" in k.lower() for k in d):
                    logger.info("Found balance via POST %s", ep.split("/")[-1])
                    push_fn("copy_details", d)
                    _status["last_scrape"] = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
                    _status["scrapes"] += 1
                    return
                logger.info("Balance POST %s → keys: %s", ep.split("/")[-1], list(d.keys())[:8])
        except Exception as e:
            logger.warning("Balance POST %s error: %s", ep.split("/")[-1], e)

    logger.warning("No balance endpoint found")


def _safe_json(r) -> dict | None:
    try:
        return r.json()
    except Exception:
        return None
