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
            for key in list(data.keys()):
                if any(pat in key.lower() for pat in ("balance", "equity", "totalbal", "totalequity")):
                    try:
                        val = round(float(data[key]), 2)
                        if val > 0:
                            _settings["balance"] = val
                            changed = True
                            logger.info("Auto-updated balance=%.2f from key=%s", val, key)
                            break
                    except (TypeError, ValueError):
                        pass
            for key in ("estNetProfit", "est_net_profit", "netProfit"):
                if key in data:
                    try:
                        val = round(float(data[key]), 2)
                        _settings["all_time_pnl"] = val
                        changed = True
                        break
                    except (TypeError, ValueError):
                        pass
            for key in ("realizedPnl", "realized_pnl", "realPnl"):
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

    if _mt5["positions_raw"] is not None:
        _rebuild_summary()


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    return {"balance": 0.0, "investment": 0.0}


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
    if isinstance(raw, dict):
        d = raw.get("data")
        if isinstance(d, list):
            return d
    return raw if isinstance(raw, list) else []


def _extract_history_rows(raw: Any) -> list:
    if isinstance(raw, dict):
        d = raw.get("data")
        if isinstance(d, dict):
            return d.get("rows") or d.get("list") or []
        if isinstance(d, list):
            return d
    return []


def _parse_positions(raw: Any) -> list[dict]:
    out = []
    for p in _extract_positions(raw):
        if not isinstance(p, dict):
            continue
        try:
            direction = int(p.get("directionType") or p.get("orderType") or 0)
            out.append({
                "symbol": str(p.get("symbol") or ""),
                "side": "short" if direction == 1 else "long",
                "size": float(p.get("volume") or 0),
                "entry_price": float(p.get("openPrice") or 0),
                "unrealized_pnl": round(float(p.get("profit") or 0), 4),
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
            direction = int(h.get("directionType") or h.get("orderType") or 0)
            ct = int(h.get("closeTime") or 0)
            out.append({
                "time": _ms_to_bkk_datetime(ct),
                "symbol": str(h.get("symbol") or ""),
                "side": "short" if direction == 1 else "long",
                "open_price": float(h.get("openPrice") or 0),
                "close_price": float(h.get("closePrice") or 0),
                "size": float(h.get("totalVolume") or h.get("closeVolume") or 0),
                "pnl": round(float(h.get("totalProfit") or h.get("profit") or 0), 4),
                "commission": round(abs(float(h.get("commission") or 0)), 4),
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
    raw = _mt5["positions_raw"]
    extracted = _extract_positions(raw)
    parsed = _parse_positions(raw)
    return {
        "positions_raw_type": type(raw).__name__,
        "positions_raw_keys": list(raw.keys()) if isinstance(raw, dict) else None,
        "data_field": raw.get("data") if isinstance(raw, dict) else None,
        "data_field_type": type(raw.get("data")).__name__ if isinstance(raw, dict) else None,
        "extracted_count": len(extracted),
        "extracted_sample": extracted[:1],
        "parsed_count": len(parsed),
        "parsed": parsed,
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
