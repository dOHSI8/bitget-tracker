import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BKK = timezone(timedelta(hours=7))
SETTINGS_FILE = Path(os.environ.get("SETTINGS_PATH", "settings.json"))
COOKIES_FILE  = Path(os.environ.get("COOKIES_PATH", "cookies.json"))
TRADERS_FILE  = Path(os.environ.get("TRADERS_PATH", "traders.json"))

_BALANCE_PATS = ("balance", "equity", "totalbal", "totalequity",
                 "totalasset", "accountval", "worth", "asset")


# ── Dynamic traders list ──────────────────────────────────────────────────────
# Stored in traders.json (runtime); falls back to TRADERS env var on startup.
# Add/remove traders via dashboard without redeploying.

def _parse_traders_env() -> list[dict]:
    result: list[dict] = []
    env = os.environ.get("TRADERS", "")
    if env:
        for item in env.split(","):
            parts = item.strip().split(":")
            if len(parts) >= 2:
                result.append({
                    "name": parts[0].strip(),
                    "id":   parts[1].strip(),
                    "type": parts[2].strip() if len(parts) >= 3 else "cfd",
                })
    else:
        result.append({
            "name": os.environ.get("TRADER_NAME", "DKTrading"),
            "id":   os.environ.get("PORTFOLIO_ID", "1443199880395776000"),
            "type": "cfd",
        })
    return result


def _load_traders_list() -> list[dict]:
    if TRADERS_FILE.exists():
        try:
            data = json.loads(TRADERS_FILE.read_text())
            entries = data.get("traders", [])
            if entries:
                return entries
        except (json.JSONDecodeError, OSError):
            pass
    return _parse_traders_env()


def _save_traders_list(traders: list[dict]) -> None:
    TRADERS_FILE.write_text(json.dumps({"traders": traders}))


# Module-level mutable list — the single source of truth for main.py
_traders_list: list[dict] = _load_traders_list()


def _trader_names() -> list[str]:
    return [t["name"] for t in _traders_list]

def _trader_id(name: str) -> str:
    for t in _traders_list:
        if t["name"] == name:
            return t["id"]
    return ""

def _trader_type(name: str) -> str:
    for t in _traders_list:
        if t["name"] == name:
            return t.get("type", "cfd")
    return "cfd"

_DEFAULT_TRADER = _traders_list[0]["name"] if _traders_list else "DKTrading"

_BALANCE_PATS = ("balance", "equity", "totalbal", "totalequity",
                 "totalasset", "accountval", "worth", "asset")


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "balance": float(os.environ.get("INIT_BALANCE", "0")),
        "investment": float(os.environ.get("INIT_INVESTMENT", "0")),
        "traders": {},
    }


def _save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s))


_settings = _load_settings()
if "traders" not in _settings:
    _settings["traders"] = {}


# ── Per-trader in-memory cache ────────────────────────────────────────────────

_traders_cache: dict[str, dict] = {}


def _tc(name: str) -> dict:
    if name not in _traders_cache:
        _traders_cache[name] = {
            "positions_raw": None,
            "history_raw": None,
            "summary": None,
            "trades": None,
            "history": None,
            "pushed_at": None,
        }
    return _traders_cache[name]


def _ts(name: str) -> dict:
    return _settings["traders"].setdefault(name, {})


# ── Global (aggregate) MT5 cache ─────────────────────────────────────────────

_mt5: dict = {
    "positions_raw": None,
    "history_raw": None,
    "balance_raw": None,
    "summary": None,
    "trades": None,
    "history": None,
    "pushed_at": None,
}


# ── Time helpers ──────────────────────────────────────────────────────────────

def _bkk_today_range_ms() -> tuple[int, int]:
    now = datetime.now(BKK)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _ms_to_bkk_datetime(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=BKK).strftime("%Y-%m-%d %H:%M")


def _ms_to_bkk_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=BKK).strftime("%Y-%m-%d")


# ── Parsers ───────────────────────────────────────────────────────────────────

def _extract_positions(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        d = raw.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get("list") or d.get("rows") or d.get("posList") or d.get("data") or []
    return []


def _extract_history_rows(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        d = raw.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get("rows") or d.get("list") or d.get("data") or []
    return []


def _parse_side(p: dict) -> str:
    raw = (p.get("side") or p.get("holdSide") or
           p.get("directionType") or p.get("orderType") or 0)
    if isinstance(raw, str):
        return "short" if raw.lower() in ("short", "sell", "1") else "long"
    return "short" if int(raw) == 1 else "long"


def _fv(*keys, src: dict, default=0.0) -> float:
    for k in keys:
        v = src.get(k)
        if v is not None and v != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def _parse_positions(raw: Any) -> list[dict]:
    out = []
    for p in _extract_positions(raw):
        if not isinstance(p, dict):
            continue
        try:
            out.append({
                "symbol": str(p.get("symbol") or p.get("symbolName") or p.get("instId") or ""),
                "side": _parse_side(p),
                "size": _fv("volume", "holdSize", "openSize", "size", "openOrderSize", "total", src=p),
                "entry_price": _fv("openPrice", "openPriceAvg", "averageOpenPrice", "entryPrice", src=p),
                "unrealized_pnl": round(_fv("profit", "unrealizedPL", "unrealizedPnl",
                                            "unrealizedPNL", "upl", src=p), 4),
            })
        except (TypeError, ValueError):
            continue
    return out


def _parse_trades(raw: Any) -> list[dict]:
    out = []
    for h in _extract_history_rows(raw):
        if not isinstance(h, dict):
            continue
        try:
            ct_raw = (h.get("closeTime") or h.get("closedAt") or
                      h.get("closeTs") or h.get("ctime") or 0)
            ct = int(ct_raw)
            if 0 < ct < 10_000_000_000:
                ct *= 1000
            out.append({
                "time": _ms_to_bkk_datetime(ct),
                "symbol": str(h.get("symbol") or h.get("symbolName") or h.get("instId") or ""),
                "side": _parse_side(h),
                "open_price": _fv("openPrice", "openPriceAvg", "averageOpenPrice", "entryPrice", src=h),
                "close_price": _fv("closePrice", "closePriceAvg", src=h),
                "size": _fv("totalVolume", "closeVolume", "size", "closeSize",
                            "totalSize", "volume", src=h),
                "pnl": round(_fv("totalProfit", "profit", "realizedPL", "realizedPnl",
                                 "realizedPNL", "pnl", src=h), 4),
                "commission": round(abs(_fv("commission", "fee", "tradeFee", src=h)), 4),
                "close_time_ms": ct,
            })
        except (TypeError, ValueError):
            continue
    return out


def _calc_futures_investment(rows: list) -> float:
    """Compute net investment from /v1/trigger/uta/trace/getBalanceHistory rows.
    transferType 0 = deposit into copy trading (+).
    transferType 1 = withdrawal from copy trading (-).
    """
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            amount = abs(float(r.get("transferAmount", 0)))
        except (TypeError, ValueError):
            continue
        ttype = r.get("transferType", -1)
        if ttype == 0:
            total += amount
        elif ttype == 1:
            total -= amount
    return round(total, 2)


def _parse_history_chart(trades: list[dict]) -> list[dict]:
    day_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        d = _ms_to_bkk_date(t["close_time_ms"]) if t["close_time_ms"] else None
        if d:
            day_pnl[d] += t["pnl"]
    cumulative = 0.0
    result = []
    for d in sorted(day_pnl):
        cumulative += day_pnl[d]
        result.append({"date": d, "pnl": round(day_pnl[d], 4), "cumulative_pnl": round(cumulative, 4)})
    return result


def _parse_balance(raw: Any) -> float:
    if not isinstance(raw, dict):
        return 0.0
    data = raw.get("data") or raw
    if isinstance(data, dict):
        for key in ("balance", "totalBalance", "total_balance", "equity", "totalEquity"):
            val = data.get(key)
            if val is not None:
                try:
                    return round(float(val), 2)
                except (TypeError, ValueError):
                    pass
    return 0.0


# ── Push handler ──────────────────────────────────────────────────────────────

def _push_data(kind: str, data, trader: str = None):
    global _settings
    if trader is None:
        trader = _DEFAULT_TRADER

    tc = _tc(trader)
    ts = _ts(trader)

    if kind == "positions":
        # Positions are global (can't split by trader yet)
        _mt5["positions_raw"] = data
        _mt5["pushed_at"] = datetime.now(BKK).strftime("%H:%M")
        tc["pushed_at"] = _mt5["pushed_at"]
    elif kind == "history":
        tc["history_raw"] = data
    elif kind == "copy_details":
        tc["pushed_at"] = datetime.now(BKK).strftime("%H:%M")
        _mt5["pushed_at"] = tc["pushed_at"]
        if isinstance(data, dict):
            changed = False
            for key in list(data.keys()):
                if any(pat in key.lower() for pat in _BALANCE_PATS):
                    try:
                        val = round(float(data[key]), 2)
                        if val > 0:
                            ts["balance"] = val
                            changed = True
                            logger.info("Auto-updated balance[%s]=%.2f from key=%s", trader, val, key)
                            break
                    except (TypeError, ValueError):
                        pass
            if not changed:
                logger.info("copy_details[%s] keys (no balance found): %s", trader, list(data.keys())[:10])
            for key in ("estNetProfit", "est_net_profit", "netProfit", "totalProfit",
                        "cumProfitLoss", "totalPL"):
                if key in data:
                    try:
                        val = round(float(data[key]), 2)
                        ts["all_time_pnl"] = val
                        changed = True
                        break
                    except (TypeError, ValueError):
                        pass
            for key in ("realizedPnl", "realized_pnl", "realPnl", "realizedPL",
                        "realizedProfit", "closedPL"):
                if key in data:
                    try:
                        ts["realized_pnl"] = round(float(data[key]), 2)
                        changed = True
                    except (TypeError, ValueError):
                        pass
            for key in ("totalInvestment", "total_investment", "investment"):
                if key in data:
                    try:
                        val = round(float(data[key]), 2)
                        if val > 0:
                            ts["investment"] = val
                            changed = True
                            logger.info("Auto-updated investment[%s]=%.2f from key=%s", trader, val, key)
                    except (TypeError, ValueError):
                        pass
            # Open (floating) PnL from portfolio details
            for key in ("floatProfit", "floatingProfit", "unrealizedPnl", "openPnl",
                        "unrealizedPL", "upl", "floatPL"):
                if key in data:
                    try:
                        ts["open_pnl"] = round(float(data[key]), 2)
                    except (TypeError, ValueError):
                        pass
                    break
            if changed:
                _save_settings(_settings)
    elif kind == "fund_flow":
        # Futures copy trading transfer history — compute net investment
        rows = data if isinstance(data, list) else []
        if rows:
            inv = _calc_futures_investment(rows)
            if inv > 0:
                ts["investment"] = inv
                _save_settings(_settings)
                logger.info("Auto-updated investment[%s]=%.2f from fund_flow (%d rows)",
                            trader, inv, len(rows))
    elif kind == "balance":
        _mt5["balance_raw"] = data
    elif kind == "balance_sniff":
        if "balance_sniffs" not in _mt5:
            _mt5["balance_sniffs"] = []
        _mt5["balance_sniffs"].append(data)
        _mt5["balance_sniffs"] = _mt5["balance_sniffs"][-20:]

    try:
        _rebuild_summary()
    except Exception as e:
        logger.error("_rebuild_summary failed: %s", e)


# ── Summary builders ──────────────────────────────────────────────────────────

def _rebuild_trader_summary(name: str) -> dict:
    tc = _tc(name)
    ts = _ts(name)

    trades = _parse_trades(tc["history_raw"])
    history = _parse_history_chart(trades)

    today_start_ms, today_end_ms = _bkk_today_range_ms()
    daily_pnl = sum(
        t["pnl"] for t in trades
        if today_start_ms <= t["close_time_ms"] < today_end_ms
    )
    trades_pnl = sum(t["pnl"] for t in trades)
    scraped_pnl = ts.get("realized_pnl", 0.0)
    all_time_pnl = scraped_pnl if scraped_pnl != 0.0 else trades_pnl

    summary = {
        "name": name,
        "type": _trader_type(name),
        "portfolio_id": _trader_id(name),
        "balance": ts.get("balance", 0.0),
        "investment": ts.get("investment", 0.0),
        "daily_pnl": round(daily_pnl, 4),
        "all_time_pnl": round(all_time_pnl, 4),
        "open_positions_pnl": ts.get("open_pnl", 0.0),
        "pushed_at": tc["pushed_at"],
        "has_data": tc["history_raw"] is not None or ts.get("balance", 0) > 0 or ts.get("investment", 0) > 0,
    }
    tc["summary"] = summary
    tc["trades"] = trades
    tc["history"] = history
    return summary


def _rebuild_summary() -> None:
    all_trades = []
    total_balance = 0.0
    total_investment = 0.0
    total_daily_pnl = 0.0
    total_all_time_pnl = 0.0
    total_open_pnl = 0.0

    for name in _trader_names():
        s = _rebuild_trader_summary(name)
        all_trades.extend(_tc(name)["trades"] or [])
        total_balance += s["balance"]
        total_investment += s["investment"]
        total_daily_pnl += s["daily_pnl"]
        total_all_time_pnl += s["all_time_pnl"]
        total_open_pnl += s["open_positions_pnl"]

    # Open positions from the global probe (fallback: sum from portfolio details)
    all_positions = _parse_positions(_mt5["positions_raw"])
    if all_positions:
        total_open_pnl = sum(p["unrealized_pnl"] for p in all_positions)

    _mt5["summary"] = {
        "daily_pnl": round(total_daily_pnl, 4),
        "open_positions": len(all_positions),
        "open_positions_pnl": round(total_open_pnl, 4),
        "all_time_pnl": round(total_all_time_pnl, 4),
        "total_balance": round(total_balance, 2),
        "total_investment": round(total_investment, 2),
        "pushed_at": _mt5["pushed_at"],
    }
    all_trades.sort(key=lambda t: t["close_time_ms"], reverse=True)
    _mt5["trades"] = all_trades
    _mt5["history"] = _parse_history_chart(all_trades)

    logger.info(
        "MT5 rebuilt: traders=%d daily_pnl=%.4f all_time=%.4f balance=%.2f",
        len(_traders_list), total_daily_pnl, total_all_time_pnl, total_balance,
    )


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    env_cookie = os.environ.get("BITGET_COOKIE", "")
    cookie_path = Path(os.environ.get("COOKIES_PATH", "cookies.json"))
    if env_cookie and not cookie_path.exists():
        cookie_path.write_text(json.dumps({
            "cookie": env_cookie,
            "updated": "from-env-var",
        }))
        logger.info("Restored cookie from BITGET_COOKIE env var (%d chars)", len(env_cookie))

    from browser_poller import start_poller
    task = asyncio.create_task(start_poller(_push_data))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/api/push/mt5")
async def push_mt5(request: Request):
    body = await request.json()
    _push_data(body.get("kind"), body.get("data"), body.get("trader"))
    return {"ok": True}


@app.get("/api/mt5")
async def get_mt5():
    if not _mt5["summary"]:
        return {"available": False}
    return {**_mt5["summary"], "available": True}


@app.get("/api/mt5/traders")
async def get_mt5_traders():
    summaries = []
    for name in _trader_names():
        tc = _tc(name)
        if tc["summary"] is None:
            _rebuild_trader_summary(name)
        summaries.append(tc["summary"])
    return summaries


@app.get("/api/mt5/positions")
async def get_mt5_positions():
    return _parse_positions(_mt5["positions_raw"])


@app.get("/api/mt5/trades")
async def get_mt5_trades():
    return _mt5["trades"] or []


@app.get("/api/mt5/history")
async def get_mt5_history():
    return _mt5["history"] or []


@app.get("/api/mt5/debug")
async def get_mt5_debug():
    pos_raw = _mt5["positions_raw"]
    traders_debug = {}
    for name in _trader_names():
        tc = _tc(name)
        hist_raw = tc["history_raw"]
        extracted_hist = _extract_history_rows(hist_raw)
        parsed_hist = _parse_trades(hist_raw)
        traders_debug[name] = {
            "history_raw_type": type(hist_raw).__name__,
            "extracted_count": len(extracted_hist),
            "parsed_count": len(parsed_hist),
            "parsed_sample": parsed_hist[:3],
            "settings": _ts(name),
            "pushed_at": tc["pushed_at"],
        }
    extracted_pos = _extract_positions(pos_raw)
    parsed_pos = _parse_positions(pos_raw)
    return {
        "positions": {
            "raw_type": type(pos_raw).__name__,
            "raw_keys": list(pos_raw.keys()) if isinstance(pos_raw, dict) else None,
            "api_code": pos_raw.get("code") if isinstance(pos_raw, dict) else None,
            "extracted_count": len(extracted_pos),
            "parsed_count": len(parsed_pos),
            "parsed": parsed_pos,
        },
        "traders": traders_debug,
        "aggregate_settings": {k: v for k, v in _settings.items() if k != "traders"},
    }


@app.get("/api/mt5/sniffs")
async def get_sniffs():
    return _mt5.get("balance_sniffs", [])


@app.get("/api/mt5/raw")
async def get_mt5_raw():
    return {
        "positions": _mt5["positions_raw"],
        "traders": {name: {"history": _tc(name)["history_raw"]} for name in _trader_names()},
        "balance": _mt5["balance_raw"],
    }


@app.get("/api/settings")
async def get_settings():
    return _settings


@app.post("/api/settings")
async def post_settings(request: Request):
    global _settings
    body = await request.json()
    if "balance" in body:
        _settings["balance"] = round(float(body["balance"]), 2)
    if "investment" in body:
        _settings["investment"] = round(float(body["investment"]), 2)
    _save_settings(_settings)
    if any(_tc(n)["history_raw"] is not None for n in _trader_names()):
        _rebuild_summary()
    return _settings


@app.get("/api/widget")
async def get_widget():
    s = _mt5["summary"]
    if not s:
        return {
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "open_positions": 0,
            "open_positions_pnl": 0.0,
            "all_time_pnl": 0.0,
            "total_balance": _settings.get("balance", 0.0),
            "total_investment": _settings.get("investment", 0.0),
            "updated_at": datetime.now(BKK).strftime("%H:%M"),
            "stale": True,
        }
    pushed_at = s.get("pushed_at")
    stale = True
    if pushed_at:
        try:
            last = datetime.strptime(
                datetime.now(BKK).strftime("%Y-%m-%d ") + pushed_at, "%Y-%m-%d %H:%M"
            ).replace(tzinfo=BKK)
            stale = (datetime.now(BKK) - last).total_seconds() > 900
        except Exception:
            stale = False
    return {
        "daily_pnl": s["daily_pnl"],
        "daily_pnl_pct": 0.0,
        "open_positions": s["open_positions"],
        "open_positions_pnl": s["open_positions_pnl"],
        "all_time_pnl": s["all_time_pnl"],
        "total_balance": s["total_balance"],
        "total_investment": s["total_investment"],
        "updated_at": pushed_at or datetime.now(BKK).strftime("%H:%M"),
        "stale": stale,
    }


# ── Trader management endpoints ──────────────────────────────────────────────

@app.get("/api/traders")
async def list_traders():
    return _traders_list


@app.post("/api/traders")
async def add_trader(request: Request):
    global _traders_list, _DEFAULT_TRADER
    body = await request.json()
    name  = str(body.get("name", "")).strip()
    pid   = str(body.get("id", "")).strip()
    ttype = str(body.get("type", "cfd")).strip().lower()
    if not name or not pid:
        return {"ok": False, "error": "name and id are required"}
    if ttype not in ("cfd", "futures"):
        ttype = "cfd"
    if any(t["name"] == name for t in _traders_list):
        return {"ok": False, "error": f"Trader '{name}' already exists"}
    _traders_list.append({"name": name, "id": pid, "type": ttype})
    _save_traders_list(_traders_list)
    if not _DEFAULT_TRADER:
        _DEFAULT_TRADER = name
    logger.info("Trader added: name=%s id=%s type=%s", name, pid, ttype)
    return {"ok": True, "traders": _traders_list}


@app.delete("/api/traders/{name}")
async def remove_trader(name: str):
    global _traders_list
    before = len(_traders_list)
    _traders_list = [t for t in _traders_list if t["name"] != name]
    if len(_traders_list) == before:
        return {"ok": False, "error": f"Trader '{name}' not found"}
    # Clean up in-memory cache for removed trader
    _traders_cache.pop(name, None)
    _settings.get("traders", {}).pop(name, None)
    _save_traders_list(_traders_list)
    _save_settings(_settings)
    logger.info("Trader removed: name=%s", name)
    try:
        _rebuild_summary()
    except Exception:
        pass
    return {"ok": True, "traders": _traders_list}


# ── Browser poller endpoints ─────────────────────────────────────────────────

@app.get("/api/poller")
async def get_poller_status():
    from browser_poller import get_status
    return get_status()


@app.get("/api/poller/test")
async def test_poller_cookie():
    from browser_poller import _load_cookie_string, _parse_cookie_string, BITGET_BASE, PORTFOLIO_ID, CHROMIUM_ARGS
    cookie_str = _load_cookie_string()
    if not cookie_str:
        return {"ok": False, "error": "No cookie set"}
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
            cookies = _parse_cookie_string(cookie_str)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            try:
                await page.goto(f"{BITGET_BASE}/about", wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            await page.close()
            r = await context.request.post(
                f"{BITGET_BASE}/v1/trace/mt5/data/tracePosition",
                data=json.dumps({"portfolioId": PORTFOLIO_ID}),
                headers={"Content-Type": "application/json"},
                timeout=20_000,
            )
            try:
                body = await r.json()
            except Exception:
                body = (await r.text())[:500]
            await browser.close()
        return {"http_status": r.status, "body": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/poller/cookie")
async def set_poller_cookie(request: Request):
    body = await request.json()
    cookie = body.get("cookie", "").strip()
    if not cookie:
        return {"ok": False, "error": "No cookie provided"}
    COOKIES_FILE.write_text(json.dumps({
        "cookie": cookie,
        "updated": datetime.now(BKK).isoformat(),
    }))
    logger.info("Poller cookie updated (%d chars)", len(cookie))
    return {"ok": True, "length": len(cookie)}


@app.delete("/api/poller/cookie")
async def clear_poller_cookie():
    if COOKIES_FILE.exists():
        COOKIES_FILE.unlink()
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
