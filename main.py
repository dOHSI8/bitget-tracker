import asyncio
import json
import logging
import os
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


def _push_data(kind: str, data):
    """Shared push handler used by both HTTP endpoint and browser poller."""
    global _settings
    if kind == "positions":
        _mt5["positions_raw"] = data
        _mt5["pushed_at"] = datetime.now(BKK).strftime("%H:%M")
    elif kind == "history":
        _mt5["history_raw"] = data
    elif kind == "copy_details":
        if isinstance(data, dict):
            changed = False
            # Search for any key that suggests account balance
            _BALANCE_PATS = ("balance", "equity", "totalbal", "totalequity",
                             "totalasset", "accountval", "worth", "asset")
            for key in list(data.keys()):
                if any(pat in key.lower() for pat in _BALANCE_PATS):
                    try:
                        val = round(float(data[key]), 2)
                        if val > 0:
                            _settings["balance"] = val
                            changed = True
                            logger.info("Auto-updated balance=%.2f from key=%s", val, key)
                            break
                    except (TypeError, ValueError):
                        pass
            if not changed:
                logger.info("copy_details keys (no balance found): %s", list(data.keys())[:10])
            for key in ("estNetProfit", "est_net_profit", "netProfit", "totalProfit",
                        "cumProfitLoss", "totalPL"):
                if key in data:
                    try:
                        val = round(float(data[key]), 2)
                        _settings["all_time_pnl"] = val
                        changed = True
                        break
                    except (TypeError, ValueError):
                        pass
            for key in ("realizedPnl", "realized_pnl", "realPnl", "realizedPL",
                        "realizedProfit", "closedPL"):
                if key in data:
                    try:
                        _settings["realized_pnl"] = round(float(data[key]), 2)
                        changed = True
                    except (TypeError, ValueError):
                        pass
            if changed:
                _save_settings(_settings)
    elif kind == "balance_history":
        rows = []
        if isinstance(data, dict):
            rows = data.get("rows") or data.get("list") or data.get("data") or []
        elif isinstance(data, list):
            rows = data
        if rows:
            _mt5["balance_history_raw"] = rows
            inv = _calc_investment(rows)
            _settings["investment"] = inv
            _save_settings(_settings)
            logger.info("Auto-updated investment=%.2f from %d rows", inv, len(rows))
    elif kind == "balance":
        _mt5["balance_raw"] = data
    elif kind == "balance_sniff":
        url = data.get("url", "") if isinstance(data, dict) else ""
        if "balance_sniffs" not in _mt5:
            _mt5["balance_sniffs"] = []
        _mt5["balance_sniffs"].append(data)
        _mt5["balance_sniffs"] = _mt5["balance_sniffs"][-20:]

    try:
        _rebuild_summary()
    except Exception as e:
        logger.error("_rebuild_summary failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore cookie from env var if file was wiped by redeploy
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

BKK = timezone(timedelta(hours=7))

SETTINGS_FILE = Path(os.environ.get("SETTINGS_PATH", "settings.json"))


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "balance": float(os.environ.get("INIT_BALANCE", "0")),
        "investment": float(os.environ.get("INIT_INVESTMENT", "0")),
    }


def _save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s))


_settings = _load_settings()


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


# ── MT5 cache ─────────────────────────────────────────────────────────────────

_mt5: dict = {
    "positions_raw": None,
    "history_raw": None,
    "balance_raw": None,
    "summary": None,
    "trades": None,
    "history": None,
    "pushed_at": None,
}


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
    """Resolve position side from any of the known field variants."""
    raw = (p.get("side") or p.get("holdSide") or
           p.get("directionType") or p.get("orderType") or 0)
    if isinstance(raw, str):
        return "short" if raw.lower() in ("short", "sell", "1") else "long"
    return "short" if int(raw) == 1 else "long"


def _fv(*keys, src: dict, default=0.0) -> float:
    """Return the first non-None, non-empty value from src matching any key."""
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
            if 0 < ct < 10_000_000_000:  # seconds → ms
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


def _parse_balance(raw: Any) -> float:
    """Extract total balance from /v1/trace/mt5/account/balance response."""
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


def _parse_history_chart(trades: list[dict]) -> list[dict]:
    """Group closed trades by Bangkok date for the 30-day chart."""
    from collections import defaultdict
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


def _rebuild_summary() -> None:
    positions = _parse_positions(_mt5["positions_raw"])
    trades    = _parse_trades(_mt5["history_raw"])
    history   = _parse_history_chart(trades)

    open_pnl = sum(p["unrealized_pnl"] for p in positions)

    today_start_ms, today_end_ms = _bkk_today_range_ms()
    daily_pnl = sum(
        t["pnl"] for t in trades
        if today_start_ms <= t["close_time_ms"] < today_end_ms
    )
    trades_pnl = sum(t["pnl"] for t in trades)
    # Prefer scraped Realized PnL from Bitget page (accurate), fall back to trades sum
    scraped_pnl = _settings.get("realized_pnl", 0.0)
    all_time_pnl = scraped_pnl if scraped_pnl != 0.0 else trades_pnl

    _mt5["summary"] = {
        "daily_pnl": round(daily_pnl, 4),
        "open_positions": len(positions),
        "open_positions_pnl": round(open_pnl, 4),
        "all_time_pnl": round(all_time_pnl, 4),
        "total_balance": _settings.get("balance", 0.0),
        "total_investment": _settings.get("investment", 0.0),
        "pushed_at": _mt5["pushed_at"],
    }
    _mt5["trades"]  = trades
    _mt5["history"] = history

    logger.info(
        "MT5 rebuilt: positions=%d daily_pnl=%.4f all_time=%.4f",
        len(positions), daily_pnl, all_time_pnl,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

def _calc_investment(rows: list) -> float:
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        typ = str(r.get("type") or r.get("typeName") or "").lower()
        try:
            amt = abs(float(r.get("amount") or r.get("changeAmount") or 0))
        except (TypeError, ValueError):
            continue
        if "add" in typ or "deposit" in typ or "transfer in" in typ:
            total += amt
        elif "transfer out" in typ or "withdraw" in typ:
            total -= amt
    return round(total, 2)


@app.post("/api/push/mt5")
async def push_mt5(request: Request):
    body = await request.json()
    _push_data(body.get("kind"), body.get("data"))
    return {"ok": True}


@app.get("/api/mt5")
async def get_mt5():
    if not _mt5["summary"]:
        return {"available": False}
    return {**_mt5["summary"], "available": True}


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
    hist_raw = _mt5["history_raw"]
    extracted_pos = _extract_positions(pos_raw)
    extracted_hist = _extract_history_rows(hist_raw)
    parsed_pos = _parse_positions(pos_raw)
    parsed_hist = _parse_trades(hist_raw)
    return {
        "positions": {
            "raw_type": type(pos_raw).__name__,
            "raw_keys": list(pos_raw.keys()) if isinstance(pos_raw, dict) else None,
            "data_type": type(pos_raw.get("data")).__name__ if isinstance(pos_raw, dict) else None,
            "extracted_count": len(extracted_pos),
            "first_raw_item": extracted_pos[0] if extracted_pos else None,
            "parsed_count": len(parsed_pos),
            "parsed": parsed_pos,
        },
        "history": {
            "raw_type": type(hist_raw).__name__,
            "raw_keys": list(hist_raw.keys()) if isinstance(hist_raw, dict) else None,
            "extracted_count": len(extracted_hist),
            "first_raw_item": extracted_hist[0] if extracted_hist else None,
            "parsed_count": len(parsed_hist),
            "parsed_sample": parsed_hist[:3],
        },
        "settings": _settings,
    }


@app.get("/api/mt5/sniffs")
async def get_sniffs():
    return _mt5.get("balance_sniffs", [])


@app.get("/api/mt5/raw")
async def get_mt5_raw():
    return {
        "positions": _mt5["positions_raw"],
        "history": _mt5["history_raw"],
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
    if _mt5["positions_raw"] is not None:
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
    return {
        "daily_pnl": s["daily_pnl"],
        "daily_pnl_pct": 0.0,
        "open_positions": s["open_positions"],
        "open_positions_pnl": s["open_positions_pnl"],
        "all_time_pnl": s["all_time_pnl"],
        "total_balance": _settings.get("balance", 0.0),
        "total_investment": _settings.get("investment", 0.0),
        "updated_at": datetime.now(BKK).strftime("%H:%M"),
        "stale": False,
    }


# ── Browser poller endpoints ─────────────────────────────────────────────────

COOKIES_FILE = Path(os.environ.get("COOKIES_PATH", "cookies.json"))


@app.get("/api/poller")
async def get_poller_status():
    from browser_poller import get_status
    return get_status()


@app.get("/api/poller/test")
async def test_poller_cookie():
    """Run one full poll cycle and return diagnostic info."""
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
