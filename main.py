import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import hmac

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BKK = timezone(timedelta(hours=7))


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


SETTINGS_FILE    = Path(os.environ.get("SETTINGS_PATH", "settings.json"))
COOKIES_FILE     = Path(os.environ.get("COOKIES_PATH", "cookies.json"))
TRADERS_FILE     = Path(os.environ.get("TRADERS_PATH", "traders.json"))
CREDENTIALS_FILE = Path(os.environ.get("CREDENTIALS_PATH", "credentials.json"))
HISTORY_FILE     = Path(os.environ.get("HISTORY_PATH", "history.json"))

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
            "name": os.environ.get("TRADER_NAME", "TraderName"),
            "id":   os.environ.get("PORTFOLIO_ID", "YOUR_PORTFOLIO_ID"),
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


def _load_history() -> dict[str, list]:
    """Load persisted trade rows from history.json, keyed by trader name."""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_history(trader: str, rows: list) -> None:
    """Persist accumulated trade rows for one trader to disk."""
    try:
        data: dict = {}
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        data[trader] = rows
        HISTORY_FILE.write_text(json.dumps(data))
    except OSError as e:
        logger.warning("Could not save history.json: %s", e)


# ── Per-trader in-memory cache ────────────────────────────────────────────────

_traders_cache: dict[str, dict] = {}


def _tc(name: str) -> dict:
    if name not in _traders_cache:
        persisted = _load_history().get(name, [])
        history_raw = {"code": "200", "data": {"rows": persisted}} if persisted else None
        _traders_cache[name] = {
            "positions_raw": None,
            "history_raw": history_raw,
            "summary": None,
            "trades": None,
            "history": None,
            "pushed_at": None,
        }
        if persisted:
            logger.info("Seeded history[%s] from disk: %d rows", name, len(persisted))
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

_investment: dict = {
    "data": None,        # result from fetch_net_investment
    "fetched_at": None,  # ISO timestamp string
    "error": None,
}

_earn: dict = {
    "data": None,        # result from fetch_earn_balance
    "fetched_at": None,
    "error": None,
}

_elite: dict = {
    "data": None,        # dict with balance, all_time_pnl, daily_pnl, follower_count
    "fetched_at": None,
    "error": None,
}

_elite_overview: dict = {
    "data": None,        # publicPortfolioOverview fields + active:bool
    "fetched_at": None,
    "error": None,
}

_futures_leader: dict = {
    "data": None,        # result from fetch_futures_leader_portfolio
    "fetched_at": None,
    "error": None,
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
        # Top-level list keys (tracePosition returns {rows:[...]} or {list:[...]})
        for key in ("list", "rows", "positions", "posList", "data"):
            v = raw.get(key)
            if isinstance(v, list) and v:
                return v
        # One level deeper via "data" wrapper
        d = raw.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for key in ("list", "rows", "positions", "posList", "data"):
                v = d.get(key)
                if isinstance(v, list) and v:
                    return v
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
                "profit_share": round(abs(_fv("profitShare", "profitShareAmount",
                                              "profitSharingAmount", "shareAmount", src=h)), 4),
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


def _load_credentials() -> dict | None:
    """Return {api_key, secret, passphrase} or None if not configured."""
    if CREDENTIALS_FILE.exists():
        try:
            d = json.loads(CREDENTIALS_FILE.read_text())
            if d.get("api_key") and d.get("secret") and d.get("passphrase"):
                return d
        except (json.JSONDecodeError, OSError):
            pass
    # Fall back to env vars
    k = os.environ.get("BITGET_API_KEY", "")
    s = os.environ.get("BITGET_API_SECRET", "")
    p = os.environ.get("BITGET_PASSPHRASE", "")
    if k and s and p:
        return {"api_key": k, "secret": s, "passphrase": p}
    return None


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
        # Merge new rows with existing history instead of replacing.
        # This builds up 30-day data across poll cycles, since each poll
        # only returns the latest page(s) and an active trader's 50 most
        # recent trades can all be from the same day.
        new_rows = _extract_history_rows(data)
        if new_rows:
            existing_rows = _extract_history_rows(tc.get("history_raw"))
            # Deduplicate by close timestamp + symbol (unique per trade)
            def _trade_key(r: dict) -> str:
                ct = (r.get("closeTime") or r.get("closedAt") or
                      r.get("closeTs") or r.get("ctime") or "")
                return f"{ct}_{r.get('symbol', '')}"
            seen = {_trade_key(r) for r in new_rows if isinstance(r, dict)}
            merged = list(new_rows)
            for r in existing_rows:
                if isinstance(r, dict) and _trade_key(r) not in seen:
                    merged.append(r)
            # Trim to last 365 days to keep memory bounded while preserving full history
            cutoff_ms = int((datetime.now(BKK) - timedelta(days=365)).timestamp() * 1000)
            def _close_ms(r: dict) -> int:
                for k in ("closeTime", "closedAt", "closeTs", "ctime"):
                    v = r.get(k)
                    if v:
                        try:
                            t = int(v)
                            return t * 1000 if t < 10_000_000_000 else t
                        except (TypeError, ValueError):
                            pass
                return cutoff_ms + 1  # keep if unknown
            merged = [r for r in merged if _close_ms(r) > cutoff_ms]
            tc["history_raw"] = {"code": "200", "data": {"rows": merged}}
            _save_history(trader, merged)
            logger.info("History[%s]: merged %d new + kept old → %d total rows",
                        trader, len(new_rows), len(merged))
        else:
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
            for key in ("sharedProfit", "profitShareAmount", "profitSharingAmount",
                        "totalProfitShare", "paidProfitShare", "realizedProfitShare",
                        "settledProfitShare", "profitShare", "shareAmount"):
                if key in data:
                    try:
                        ts["profit_share"] = round(abs(float(data[key])), 2)
                        changed = True
                    except (TypeError, ValueError):
                        pass
                    break
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
            # Open (floating) PnL from portfolio details.
            # In MT5/CFD copy trading, Bitget uses "profit" for floating PnL
            # (equity = balance + profit). We also fall back to equity - balance.
            open_pnl_found = False
            for key in ("floatProfit", "floatingProfit", "unrealizedPnl", "openPnl",
                        "unrealizedPL", "upl", "floatPL", "profit", "pnl"):
                if key in data:
                    try:
                        ts["open_pnl"] = round(float(data[key]), 2)
                        open_pnl_found = True
                    except (TypeError, ValueError):
                        pass
                    break
            if not open_pnl_found:
                # equity - balance fallback (handles any field naming)
                try:
                    eq = float(data.get("equity") or data.get("netValue") or data.get("totalEquity") or 0)
                    bal = float(data.get("balance") or ts.get("balance") or 0)
                    if eq > 0 and bal > 0 and abs(eq - bal) > 0.001:
                        ts["open_pnl"] = round(eq - bal, 2)
                except (TypeError, ValueError):
                    pass
            # Log all keys on first push so we can diagnose field names
            if not ts.get("_copy_detail_keys"):
                ts["_copy_detail_keys"] = list(data.keys())[:20]
            # Open position count from portfolio details
            for key in ("openPositionNum", "openPositionCount", "positionCount",
                        "holdNum", "followOpenPositionNum", "openNum", "openCount"):
                if key in data:
                    try:
                        ts["open_position_count"] = int(data[key])
                    except (TypeError, ValueError):
                        pass
                    break
            if changed:
                _save_settings(_settings)
    elif kind == "cancelled_copies":
        # Account-level: net profit from all copy portfolios the user has stopped.
        # Stored globally (not per-trader) since these copies no longer exist as traders.
        rows = data if isinstance(data, list) else []
        if not rows:
            return
        r0 = rows[0] if isinstance(rows[0], dict) else {}
        # Reject if rows look like active portfolio data (live trading fields present).
        # Active portfolios have marginCall/credit/connecting; cancelled summaries don't.
        if any(k in r0 for k in ("marginCall", "credit", "connecting")):
            logger.warning("Cancelled copies: rejected active portfolio data (%d rows) — clearing stale value",
                           len(rows))
            _settings.pop("cancelled_copy_pnl", None)
            _settings.pop("cancelled_copy_count", None)
            _save_settings(_settings)
            return
        total = 0.0
        for r in rows:
            if not isinstance(r, dict):
                continue
            # Try direct netProfit field first; fall back to realizedPnl - profitShareAmount
            for key in ("netProfit", "net_profit", "estNetProfit", "totalProfit", "profit", "closedPnl"):
                if key in r:
                    try:
                        total += float(r[key])
                        break
                    except (TypeError, ValueError):
                        pass
            else:
                rpnl = 0.0
                pshare = 0.0
                for k in ("realizedPnl", "realizedProfit", "realPnl"):
                    if k in r:
                        try:
                            rpnl = float(r[k])
                            break
                        except (TypeError, ValueError):
                            pass
                for k in ("profitSharingAmount", "profitShareAmount", "shareProfit", "sharedProfit"):
                    if k in r:
                        try:
                            pshare = float(r[k])
                            break
                        except (TypeError, ValueError):
                            pass
                total += rpnl - pshare
        # Extract the trader/copy name from each stopped-copy row so we can
        # hide those traders from the active trader cards and avoid double-counting.
        _NAME_KEYS = ("traderName", "followName", "nickname", "name",
                      "traderNickName", "masterName", "copyTraderName")
        cancelled_names: list[str] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            for k in _NAME_KEYS:
                v = r.get(k)
                if v and isinstance(v, str) and v.strip():
                    cancelled_names.append(v.strip())
                    break

        new_pnl, new_count = round(total, 2), len(rows)
        if (_settings.get("cancelled_copy_pnl") != new_pnl
                or _settings.get("cancelled_copy_count") != new_count
                or _settings.get("cancelled_trader_names") != cancelled_names):
            _settings["cancelled_copy_pnl"] = new_pnl
            _settings["cancelled_copy_count"] = new_count
            _settings["cancelled_trader_names"] = cancelled_names
            _save_settings(_settings)
            logger.info("Cancelled copies: %d entries, total net_profit=%.2f, names=%s",
                        new_count, total, cancelled_names)
    elif kind == "elite_trader":
        # Elite (lead) trader portfolio — data is a single portfolio dict
        row = data if isinstance(data, dict) else {}
        if not row:
            return
        # Reject follower-portfolio shapes: those balances are already counted in
        # the trader cards, so accepting one here would double-count it.
        if any(k in row for k in ("marginCall", "credit", "connecting")):
            logger.warning("Elite trader push rejected: row looks like a follower portfolio")
            return
        # Fields from screenshot: equity, balance, aum, totalProfit, roi, copiers
        balance     = _fv("balance", "equity", "totalBalance", "totalAsset", src=row)
        all_time    = _fv("totalProfit", "allTimeProfit", "profit",
                          "realizedPnl", "realPnl", "cumulativeProfit", src=row)
        daily       = _fv("dailyProfit", "todayProfit", "dailyPnl",
                          "dayProfit", "profit24h", src=row)
        aum         = _fv("aum", "totalAum", "aumAmount", src=row)
        copiers_raw = row.get("copiers") or row.get("followerCount") or \
                      row.get("followCount") or row.get("currentFollowers") or "0"
        # copiers may be "0/100" string or a plain int
        try:
            followers = int(str(copiers_raw).split("/")[0])
        except (TypeError, ValueError):
            followers = 0
        _elite["data"] = {
            "balance": round(balance, 2),
            "all_time_pnl": round(all_time, 4),
            "daily_pnl": round(daily, 4),
            "aum": round(aum, 2),
            "follower_count": followers,
        }
        _elite["fetched_at"] = datetime.now(BKK).isoformat()
        _elite["error"] = None
        logger.info("Elite trader: balance=%.2f all_time=%.2f followers=%d",
                    balance, all_time, followers)
    elif kind == "elite_overview":
        row = data if isinstance(data, dict) else {}
        active = row.get("active", False)
        if active:
            settle = row.get("_settle") or {}
            total_profit     = _fv("profit", src=row)
            total_share_profit = _fv("totalShareProfit", src=settle)
            _elite_overview["data"] = {
                "active": True,
                "balance": _fv("avlBalance", src=row),
                "equity": _fv("totalEquity", "estimatedAssets", src=row),
                "unrealized_pnl": _fv("unrealizedProfit", src=row),
                "total_profit": total_profit,
                "total_share_profit": total_share_profit,
                "total_profit_combined": round(total_profit + total_share_profit, 4),
                "copiers_profit": _fv("followProfit", src=row),
                "roi": _fv("roi", src=row),
                "aum": _fv("aum", src=row),
                "copiers_current": int(row.get("curFollowCount") or 0),
                "copiers_max": int(row.get("maxFollowCount") or 100),
                "share_ratio": _fv("maxShareRatio", "sharingRatio", src=row),
                "portfolio_id": row.get("portfolioId"),
                "trading_days": int(row.get("tradingDay") or 0),
            }
        else:
            _elite_overview["data"] = {"active": False}
        _elite_overview["fetched_at"] = datetime.now(BKK).isoformat()
        _elite_overview["error"] = None
        logger.info("Elite overview: active=%s balance=%s roi=%s",
                    active, row.get("avlBalance"), row.get("roi"))
    elif kind == "fund_flow":
        # Futures copy trading transfer history — compute net investment.
        # Only applies to futures traders; ignore if type has already changed to cfd
        # (prevents a mid-cycle push from writing futures data back after a type switch).
        if _trader_type(trader) != "futures":
            logger.info("Ignoring fund_flow for %s (type is now %s)", trader, _trader_type(trader))
            return
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

    # Auto-clear stale settings when trader type changes (e.g. futures → cfd)
    current_type = _trader_type(name)
    prev_type = ts.get("_type")
    if prev_type and prev_type != current_type:
        logger.info("Trader %s type changed %s→%s, clearing stale settings", name, prev_type, current_type)
        ts.clear()
        ts["_type"] = current_type
        _save_settings(_settings)
    elif not prev_type:
        ts["_type"] = current_type

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

    # Profit share paid to the trader: prefer the portfolio-level scrape,
    # fall back to summing per-trade share amounts from history
    profit_share = ts.get("profit_share", 0.0)
    if not profit_share:
        profit_share = round(sum(t.get("profit_share", 0.0) for t in trades), 2)
    net_all_time_pnl = round(all_time_pnl - profit_share, 4)

    scraped_balance = ts.get("balance", 0.0)
    investment      = ts.get("investment", 0.0)
    open_pnl        = ts.get("open_pnl", 0.0)

    # For futures traders Bitget doesn't expose a direct balance field,
    # so compute it as: investment + all-time PnL + open (unrealized) PnL.
    if _trader_type(name) == "futures" and scraped_balance == 0.0:
        balance = round(investment + all_time_pnl + open_pnl, 2)
    else:
        balance = scraped_balance

    # Open position count: prefer explicit API field; fall back to 1 if PnL is non-zero
    open_pos_count = ts.get("open_position_count", 0)
    if open_pos_count == 0 and open_pnl != 0:
        open_pos_count = 1  # we know there's at least one

    summary = {
        "name": name,
        "type": _trader_type(name),
        "portfolio_id": _trader_id(name),
        "balance": balance,
        "investment": investment,
        "daily_pnl": round(daily_pnl, 4),
        "all_time_pnl": net_all_time_pnl,
        "gross_all_time_pnl": round(all_time_pnl, 4),
        "profit_share": round(profit_share, 2),
        "open_positions_pnl": open_pnl,
        "open_position_count": open_pos_count,
        "pushed_at": tc["pushed_at"],
        "has_data": tc["history_raw"] is not None or scraped_balance > 0 or investment > 0,
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
    total_open_count = 0

    for name in _trader_names():
        s = _rebuild_trader_summary(name)
        all_trades.extend(_tc(name)["trades"] or [])
        total_balance += s["balance"]
        total_investment += s["investment"]
        total_daily_pnl += s["daily_pnl"]
        total_all_time_pnl += s["all_time_pnl"]
        total_open_pnl += s["open_positions_pnl"]
        total_open_count += s.get("open_position_count", 0)

    # Open positions from the global probe; fall back to per-trader portfolio counts
    all_positions = _parse_positions(_mt5["positions_raw"])
    if all_positions:
        total_open_pnl = sum(p["unrealized_pnl"] for p in all_positions)
        total_open_count = len(all_positions)

    # If API investment is available and positive, use it as the authoritative total
    if _investment.get("data") and _investment["data"].get("net", 0) > 0:
        total_investment = _investment["data"]["net"]

    cancelled_copy_pnl = round(_settings.get("cancelled_copy_pnl", 0.0), 2)
    cancelled_copy_count = _settings.get("cancelled_copy_count", 0)
    _live_elite_pnl = (_elite.get("data") or {}).get("all_time_pnl")
    elite_all_time_pnl = round(
        _live_elite_pnl if _live_elite_pnl is not None else _settings.get("elite_all_time_pnl", 0.0),
        2,
    )

    _mt5["summary"] = {
        "daily_pnl": round(total_daily_pnl, 4),
        "open_positions": total_open_count,
        "open_positions_pnl": round(total_open_pnl, 4),
        "all_time_pnl": round(total_all_time_pnl + cancelled_copy_pnl + elite_all_time_pnl, 4),
        "active_trader_pnl": round(total_all_time_pnl, 4),
        "cancelled_copy_pnl": cancelled_copy_pnl,
        "cancelled_copy_count": cancelled_copy_count,
        "elite_all_time_pnl": elite_all_time_pnl,
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


async def _refresh_investment() -> None:
    global _investment
    creds = _load_credentials()
    if not creds:
        return
    try:
        from bitget_api import fetch_net_investment
        result = await fetch_net_investment(
            creds["api_key"], creds["secret"], creds["passphrase"],
            coins=["USDT", "USDC"],
        )
        _investment["data"] = result
        _investment["fetched_at"] = datetime.now(BKK).isoformat()
        _investment["error"] = None
        logger.info("Investment refreshed: net=%.2f deposits=%.2f withdrawals=%.2f",
                    result["net"], result["deposits"], result["withdrawals"])
        # Auto-apply net investment to global settings so widget uses it
        if result["net"] > 0:
            _settings["investment"] = result["net"]
            _save_settings(_settings)
    except Exception as e:
        _investment["error"] = str(e)
        logger.error("Investment refresh failed: %s", e)


async def _investment_poller():
    """Refresh investment data on startup then every 6 hours."""
    await asyncio.sleep(5)
    while True:
        await _refresh_investment()
        await asyncio.sleep(6 * 3600)


async def _refresh_earn() -> None:
    global _earn
    creds = _load_credentials()
    if not creds:
        return
    try:
        from bitget_api import fetch_earn_balance
        result = await fetch_earn_balance(
            creds["api_key"], creds["secret"], creds["passphrase"]
        )
        _earn["data"] = result
        _earn["fetched_at"] = datetime.now(BKK).isoformat()
        _earn["error"] = result.get("error")
        logger.info("Earn refreshed: total=%.2f apr=%s", result["total"], result.get("apr"))
    except Exception as e:
        _earn["error"] = str(e)
        logger.error("Earn refresh failed: %s", e)


async def _earn_poller():
    """Refresh earn balance on startup then every 30 minutes."""
    await asyncio.sleep(10)
    while True:
        await _refresh_earn()
        await asyncio.sleep(30 * 60)


async def _refresh_futures_leader() -> None:
    global _futures_leader
    creds = _load_credentials()
    if not creds:
        return
    try:
        from bitget_api import fetch_futures_leader_portfolio
        result = await fetch_futures_leader_portfolio(
            creds["api_key"], creds["secret"], creds["passphrase"]
        )
        _futures_leader["data"] = result if result.get("ok") else None
        _futures_leader["fetched_at"] = datetime.now(BKK).isoformat()
        _futures_leader["error"] = result.get("error") if not result.get("ok") else None
        logger.info("Futures leader refreshed: ok=%s balance=%s equity=%s",
                    result.get("ok"), result.get("balance"), result.get("equity"))
    except Exception as e:
        _futures_leader["error"] = str(e)
        logger.error("Futures leader refresh failed: %s", e)


async def _futures_leader_poller():
    """Refresh futures leader portfolio on startup then every 5 minutes."""
    await asyncio.sleep(15)
    while True:
        await _refresh_futures_leader()
        await asyncio.sleep(5 * 60)


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
    inv_task = asyncio.create_task(_investment_poller())
    earn_task = asyncio.create_task(_earn_poller())
    fl_task = asyncio.create_task(_futures_leader_poller())
    yield
    task.cancel()
    inv_task.cancel()
    earn_task.cancel()
    fl_task.cancel()


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


@app.get("/api/mt5/positions/raw")
async def get_mt5_positions_raw():
    """Return raw position data for field-name diagnosis."""
    raw = _mt5["positions_raw"]
    rows = _extract_positions(raw)
    return {"raw_type": type(raw).__name__,
            "raw_keys": list(raw.keys())[:20] if isinstance(raw, dict) else None,
            "row_count": len(rows),
            "row0_keys": list(rows[0].keys())[:30] if rows and isinstance(rows[0], dict) else None,
            "row0": rows[0] if rows else None}


# --- Live public-market prices (no auth — works even when the cookie is dead) ---
# MT5 CFD symbol -> Bitget spot symbol
_PRICE_SYMBOL_MAP = {
    "XAUUSD": "XAUUSDT",
    "XAGUSD": "XAGUSDT",
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
}
# Symbols that should use spot price rather than perpetual futures.
# Gold/silver spot tracks actual metal price; perp has funding-rate drift.
_SPOT_PREFERRED = {"XAUUSDT", "XAGUSDT"}

_price_cache: dict[str, dict] = {}  # CFD symbol -> {"price": float, "ts": float}
_PRICE_TTL = 3.0


async def _fetch_public_price(client: httpx.AsyncClient, bg_symbol: str) -> float | None:
    # For gold/silver prefer spot (no funding rate); for crypto prefer perp (more liquid)
    spot_first = bg_symbol in _SPOT_PREFERRED
    async def _spot():
        try:
            r = await client.get("https://api.bitget.com/api/v2/spot/market/tickers",
                                 params={"symbol": bg_symbol})
            d = r.json().get("data")
            if isinstance(d, list) and d:
                v = d[0].get("lastPr") or d[0].get("close")
                if v:
                    return float(v)
        except Exception:
            pass
        return None

    async def _perp():
        try:
            r = await client.get("https://api.bitget.com/api/v2/mix/market/ticker",
                                 params={"symbol": bg_symbol, "productType": "USDT-FUTURES"})
            d = r.json().get("data")
            if isinstance(d, list) and d:
                d = d[0]
            if isinstance(d, dict):
                v = d.get("lastPr") or d.get("last") or d.get("close")
                if v:
                    return float(v)
        except Exception:
            pass
        return None

    first, second = (_spot, _perp) if spot_first else (_perp, _spot)
    return await first() or await second()


@app.get("/api/prices")
async def get_prices():
    """Live prices for symbols in the current open positions.

    The dashboard uses these to recompute unrealized PnL between scraper
    polls — and while the cookie is expired, since this needs no auth.
    """
    positions = _parse_positions(_mt5["positions_raw"])
    symbols = sorted({(p.get("symbol") or "").upper() for p in positions} - {""})
    if not symbols:
        symbols = ["XAUUSD"]  # only instrument traded so far
    now = time.time()
    out = {}
    async with httpx.AsyncClient(timeout=8) as client:
        for sym in symbols:
            bg = _PRICE_SYMBOL_MAP.get(sym)
            if not bg:
                continue
            cached = _price_cache.get(sym)
            if cached and now - cached["ts"] < _PRICE_TTL:
                out[sym] = cached["price"]
                continue
            price = await _fetch_public_price(client, bg)
            if price:
                _price_cache[sym] = {"price": price, "ts": now}
                out[sym] = price
            elif cached:
                out[sym] = cached["price"]
    return {"prices": out, "ts": datetime.now(timezone.utc).isoformat()}


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
            "history_date_range": (
                f"{parsed_hist[-1]['time'][:10]} → {parsed_hist[0]['time'][:10]}"
                if len(parsed_hist) >= 2 else
                (parsed_hist[0]['time'][:10] if parsed_hist else "none")
            ),
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
        "cancelled_copy_pnl": _settings.get("cancelled_copy_pnl"),
        "cancelled_copy_count": _settings.get("cancelled_copy_count"),
        "cancelled_copies_probe": __import__("browser_poller")._status.get("cancelled_copies_probe"),
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
    if "cancelled_copy_pnl" in body:
        _settings["cancelled_copy_pnl"] = round(float(body["cancelled_copy_pnl"]), 2)
    if "elite_all_time_pnl" in body:
        _settings["elite_all_time_pnl"] = round(float(body["elite_all_time_pnl"]), 2)
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
    # Include earn + elite balances so the widget matches the dashboard total
    earn_total  = (_earn["data"] or {}).get("total", 0.0)
    elite_total = (_elite["data"] or {}).get("balance", 0.0)
    return {
        "daily_pnl": s["daily_pnl"],
        "daily_pnl_pct": 0.0,
        "open_positions": s["open_positions"],
        "open_positions_pnl": s["open_positions_pnl"],
        "all_time_pnl": s["all_time_pnl"],
        "total_balance": round(s["total_balance"] + earn_total + elite_total, 2),
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


@app.patch("/api/traders/{name}")
async def update_trader(name: str, request: Request):
    global _traders_list
    body = await request.json()
    new_id   = body.get("id", "").strip()
    new_name = body.get("name", "").strip()
    new_type = body.get("type", "").strip()
    trader = next((t for t in _traders_list if t["name"] == name), None)
    if not trader:
        return {"ok": False, "error": f"Trader '{name}' not found"}
    if new_id:
        trader["id"] = new_id
    if new_name and new_name != name:
        trader["name"] = new_name
    if new_type in ("cfd", "futures"):
        trader["type"] = new_type
    _save_traders_list(_traders_list)
    logger.info("Trader updated: %s → id=%s type=%s", name, trader["id"], trader["type"])
    return {"ok": True, "trader": trader}


@app.post("/api/traders/{name}/reset")
async def reset_trader_data(name: str):
    """Clear cached settings and in-memory data for a trader without removing it."""
    if not any(t["name"] == name for t in _traders_list):
        return {"ok": False, "error": f"Trader '{name}' not found"}
    _traders_cache.pop(name, None)
    _settings.get("traders", {}).pop(name, None)
    _save_settings(_settings)
    try:
        _rebuild_summary()
    except Exception:
        pass
    logger.info("Trader data reset: name=%s", name)
    return {"ok": True}


@app.get("/api/investment")
async def get_investment():
    creds = _load_credentials()
    data = _investment["data"] or {}
    # Strip internal diagnostic fields from the public response
    public = {k: v for k, v in data.items() if not k.startswith("_")}
    return {
        **public,
        "fetched_at": _investment["fetched_at"],
        "error": _investment["error"],
        "has_credentials": creds is not None,
    }


@app.get("/api/investment/debug")
async def get_investment_debug():
    """Full investment data including raw diagnostic fields."""
    creds = _load_credentials()
    return {
        **(_investment["data"] or {}),
        "fetched_at": _investment["fetched_at"],
        "error": _investment["error"],
        "has_credentials": creds is not None,
    }


@app.post("/api/investment/refresh")
async def refresh_investment():
    if not _load_credentials():
        return {"ok": False, "error": "No API credentials configured"}
    asyncio.create_task(_refresh_investment())
    return {"ok": True, "message": "Refresh started"}


@app.get("/api/earn")
async def get_earn():
    creds = _load_credentials()
    data = _earn["data"] or {}
    return {
        "total": data.get("total", 0.0),
        "items": data.get("items", []),
        "interest_24h": data.get("interest_24h"),
        "total_interest": data.get("total_interest"),
        "fetched_at": _earn["fetched_at"],
        "error": _earn["error"],
        "has_credentials": creds is not None,
    }


@app.post("/api/earn/refresh")
async def refresh_earn():
    if not _load_credentials():
        return {"ok": False, "error": "No API credentials configured"}
    asyncio.create_task(_refresh_earn())
    return {"ok": True, "message": "Refresh started"}


@app.get("/api/futures-leader")
async def get_futures_leader():
    data = _futures_leader["data"] or {}
    return {
        "available": _futures_leader["data"] is not None,
        "balance": data.get("balance", 0.0),
        "equity": data.get("equity", 0.0),
        "total_profit": data.get("total_profit", 0.0),
        "aum": data.get("aum", 0.0),
        "copiers": data.get("copiers", 0),
        "roi": data.get("roi", 0.0),
        "product_type": data.get("product_type"),
        "fetched_at": _futures_leader["fetched_at"],
        "error": _futures_leader["error"],
    }


@app.post("/api/futures-leader/refresh")
async def refresh_futures_leader():
    if not _load_credentials():
        return {"ok": False, "error": "No API credentials configured"}
    asyncio.create_task(_refresh_futures_leader())
    return {"ok": True, "message": "Refresh started"}


@app.get("/api/elite")
async def get_elite():
    data = _elite["data"] or {}
    # Manually-set PnL from settings is the fallback whenever live data lacks
    # a usable value — keeps the card consistent with the summary, which
    # always includes the settings value.
    settings_pnl = _settings.get("elite_all_time_pnl", 0.0)
    all_time = data.get("all_time_pnl") or settings_pnl or None
    return {
        "balance": data.get("balance", 0.0),
        "all_time_pnl": all_time,
        "daily_pnl": data.get("daily_pnl"),
        "aum": data.get("aum", 0.0),
        "follower_count": data.get("follower_count", 0),
        "fetched_at": _elite["fetched_at"],
        "error": _elite["error"],
        "available": _elite["data"] is not None or abs(settings_pnl) >= 0.01,
    }


@app.get("/api/elite/overview")
async def get_elite_overview():
    data = _elite_overview["data"] or {}
    return {
        "active": data.get("active", False),
        "balance": data.get("balance", 0.0),
        "equity": data.get("equity", 0.0),
        "unrealized_pnl": data.get("unrealized_pnl", 0.0),
        "total_profit": data.get("total_profit", 0.0),
        "total_share_profit": data.get("total_share_profit", 0.0),
        "total_profit_combined": data.get("total_profit_combined", 0.0),
        "copiers_profit": data.get("copiers_profit", 0.0),
        "roi": data.get("roi", 0.0),
        "aum": data.get("aum", 0.0),
        "copiers_current": data.get("copiers_current", 0),
        "copiers_max": data.get("copiers_max", 100),
        "share_ratio": data.get("share_ratio", 0.0),
        "portfolio_id": data.get("portfolio_id"),
        "trading_days": data.get("trading_days", 0),
        "fetched_at": _elite_overview["fetched_at"],
        "error": _elite_overview["error"],
    }


@app.post("/api/credentials")
async def save_credentials(request: Request):
    body = await request.json()
    api_key    = body.get("api_key", "").strip()
    secret     = body.get("secret", "").strip()
    passphrase = body.get("passphrase", "").strip()
    if not api_key or not secret or not passphrase:
        return {"ok": False, "error": "api_key, secret and passphrase are all required"}
    CREDENTIALS_FILE.write_text(json.dumps({
        "api_key": api_key,
        "secret": secret,
        "passphrase": passphrase,
        "updated": datetime.now(BKK).isoformat(),
    }))
    logger.info("API credentials saved")
    asyncio.create_task(_refresh_investment())
    asyncio.create_task(_refresh_earn())
    asyncio.create_task(_refresh_futures_leader())
    return {"ok": True}


@app.get("/api/credentials/status")
async def credentials_status():
    creds = _load_credentials()
    has = creds is not None
    key_preview = (creds["api_key"][:6] + "...") if has else None
    return {"configured": has, "key_preview": key_preview}


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
    local_storage = body.get("localStorage") or {}
    payload: dict = {
        "cookie": cookie,
        "updated": datetime.now(BKK).isoformat(),
    }
    if isinstance(local_storage, dict) and local_storage:
        payload["local_storage"] = local_storage
        logger.info("Poller cookie updated (%d chars) + localStorage (%d keys)",
                    len(cookie), len(local_storage))
    else:
        logger.info("Poller cookie updated (%d chars)", len(cookie))
    COOKIES_FILE.write_text(json.dumps(payload))
    from browser_poller import reset_auth_status
    reset_auth_status()
    return {"ok": True, "length": len(cookie)}


@app.delete("/api/poller/cookie")
async def clear_poller_cookie():
    if COOKIES_FILE.exists():
        COOKIES_FILE.unlink()
    return {"ok": True}


@app.get("/api/poller/cookie/export")
async def export_poller_cookie(request: Request):
    """Return the full cookie string so an external refresher (GitHub Actions /
    local script) can load it, renew the Bitget session, and POST it back.

    Disabled unless COOKIE_SYNC_TOKEN is set. The caller must present the same
    token via the X-Sync-Token header. Header-only: query params appear in
    server access logs and browser history — never put secrets there.
    """
    expected = os.environ.get("COOKIE_SYNC_TOKEN", "")
    if not expected:
        return JSONResponse({"ok": False, "error": "export disabled"}, status_code=404)
    provided = request.headers.get("x-sync-token") or ""
    if not _constant_time_eq(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=403)
    from browser_poller import _load_cookie_string
    cookie = _load_cookie_string()
    if not cookie:
        return JSONResponse({"ok": False, "error": "no cookie set"}, status_code=404)
    return {"ok": True, "cookie": cookie, "length": len(cookie)}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
