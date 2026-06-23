import asyncio
import base64
import hashlib
import hmac
import logging
import time

import httpx

logger = logging.getLogger(__name__)
BITGET_API_BASE = "https://api.bitget.com"


def _sign(ts: str, method: str, path_and_query: str, body: str, secret: str) -> str:
    prehash = ts + method.upper() + path_and_query + (body or "")
    return base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()


def _auth_headers(method: str, path_and_query: str, body: str,
                  api_key: str, secret: str, passphrase: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": _sign(ts, method, path_and_query, body, secret),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "locale": "en-US",
    }


def _row_amount(row: dict) -> float:
    """Extract the transaction amount — Bitget uses 'size' on v2, 'amount' on older paths."""
    for field in ("size", "amount", "qty", "quantity"):
        v = row.get(field)
        if v not in (None, "", "0", 0):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _is_success(row: dict) -> bool:
    """Return True for completed/confirmed records regardless of case."""
    status = str(row.get("status", "")).lower().strip()
    return status in ("success", "successful", "complete", "completed", "4")


async def _get_all_pages(client: httpx.AsyncClient, path: str, base_params: dict,
                         api_key: str, secret: str, passphrase: str,
                         max_pages: int = 20) -> tuple[list[dict], dict]:
    """Fetch all pages for a single time window."""
    rows: list[dict] = []
    cursor: str | None = None
    meta: dict = {"pages_fetched": 0, "last_code": None, "last_msg": None, "error": None,
                  "first_response": None}

    for _ in range(max_pages):
        params = {**base_params, "limit": "100"}
        if cursor:
            params["idLessThan"] = cursor
        qs = "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hdrs = _auth_headers("GET", path + qs, "", api_key, secret, passphrase)
        try:
            r = await client.get(BITGET_API_BASE + path + qs, headers=hdrs)
            resp_body = r.json()
        except Exception as exc:
            meta["error"] = str(exc)
            logger.warning("Bitget API GET %s: %s", path, exc)
            break

        code = str(resp_body.get("code", ""))
        meta["last_code"] = code
        meta["last_msg"] = resp_body.get("msg")
        if meta["first_response"] is None:
            meta["first_response"] = {k: v for k, v in resp_body.items() if k != "data"}

        if code not in ("00000", "0", "200"):
            meta["error"] = f"code={code} msg={resp_body.get('msg')}"
            logger.warning("Bitget API %s code=%s msg=%s", path, code, resp_body.get("msg"))
            break

        meta["pages_fetched"] += 1
        page: list = []
        d = resp_body.get("data") or {}
        if isinstance(d, dict):
            page = d.get("rows") or d.get("list") or []
        elif isinstance(d, list):
            page = d

        if not page:
            break
        rows.extend(page)

        if len(page) < 100:
            break

        last = page[-1]
        cursor = str(last.get("orderId") or last.get("id") or last.get("billId") or "")
        if not cursor:
            break

    return rows, meta


async def _get_windowed(client: httpx.AsyncClient, path: str, base_params: dict,
                         api_key: str, secret: str, passphrase: str,
                         start_ms: int, now_ms: int,
                         window_days: int = 85) -> tuple[list[dict], dict]:
    """
    Fetch all records across multiple 85-day windows to work around Bitget's 90-day limit.
    Walks backwards from now_ms to start_ms.
    """
    all_rows: list[dict] = []
    combined: dict = {"pages_fetched": 0, "windows_fetched": 0,
                      "last_code": None, "last_msg": None, "error": None,
                      "first_response": None}

    window_ms = window_days * 24 * 3600 * 1000
    end = now_ms

    while end > start_ms:
        win_start = max(end - window_ms, start_ms)
        params = {**base_params, "startTime": str(win_start), "endTime": str(end)}
        rows, meta = await _get_all_pages(client, path, params, api_key, secret, passphrase)

        combined["pages_fetched"] += meta["pages_fetched"]
        combined["last_code"] = meta["last_code"]
        combined["last_msg"] = meta["last_msg"]
        if combined["first_response"] is None:
            combined["first_response"] = meta.get("first_response")

        if meta.get("error"):
            combined["error"] = meta["error"]
            break

        combined["windows_fetched"] += 1
        all_rows.extend(rows)
        end = win_start - 1  # next window ends just before this one started

    return all_rows, combined


async def fetch_net_investment(api_key: str, secret: str, passphrase: str,
                                coins: list[str] | None = None) -> dict:
    """
    Return net investment = total deposits - total withdrawals across all requested coins.

    Bitget enforces a 90-day max window per request, so we walk backwards
    in 85-day chunks covering 2 years.

    Primary deposit source: deposit-records (no coin filter — 400172 if coin= passed).
    Fallback: spot account bills (if deposit-records returns a non-range error).
    Withdrawals are fetched per-coin in parallel.
    """
    if coins is None:
        coins = ["USDT"]
    coins = [c.upper() for c in coins]
    coin = coins[0]  # used for bills fallback and result label

    now_ms = int(time.time() * 1000)
    # 2 years back covers all realistic deposit history
    start_ms = now_ms - 2 * 365 * 24 * 3600 * 1000

    async with httpx.AsyncClient(timeout=60) as client:
        # Fetch deposits (no coin filter) + one withdrawal request per coin, all concurrently
        wdw_tasks = [
            _get_windowed(client, "/api/v2/spot/wallet/withdrawal-records",
                          {"coin": c}, api_key, secret, passphrase, start_ms, now_ms)
            for c in coins
        ]
        results = await asyncio.gather(
            _get_windowed(client, "/api/v2/spot/wallet/deposit-records",
                          {},  # no coin filter — 400172 if coin= passed
                          api_key, secret, passphrase, start_ms, now_ms),
            *wdw_tasks,
        )
        (deposits_all, dep_meta) = results[0]
        wdw_results = results[1:]

        # Combine withdrawals across all coins
        withdrawals: list[dict] = []
        wdw_meta = {"pages_fetched": 0, "windows_fetched": 0,
                    "last_code": None, "last_msg": None, "error": None, "first_response": None}
        for rows, meta in wdw_results:
            withdrawals.extend(rows)
            wdw_meta["pages_fetched"] += meta.get("pages_fetched", 0)
            wdw_meta["windows_fetched"] += meta.get("windows_fetched", 0)
            wdw_meta["last_code"] = meta.get("last_code") or wdw_meta["last_code"]
            if meta.get("error"):
                wdw_meta["error"] = meta["error"]
            if wdw_meta["first_response"] is None:
                wdw_meta["first_response"] = meta.get("first_response")

        bills_meta = None
        bills_rows: list[dict] = []

        # Fall back to spot account bills if deposit-records has a non-range error
        dep_error_code = dep_meta.get("last_code") or ""
        if dep_meta.get("error") and dep_error_code not in ("00000", "0", "200", "00001"):
            logger.info("deposit-records failed (%s), trying spot account bills", dep_error_code)
            bills_rows, bills_meta = await _get_windowed(
                client, "/api/v2/spot/account/bills",
                {"coin": coin, "bizType": "deposit"},
                api_key, secret, passphrase, start_ms, now_ms,
            )

    dep_source = "deposit-records"
    dep_all_coins = list({str(r.get("coin", "")) for r in deposits_all})

    if bills_rows:
        # Bills fallback: all ledger entries are final, no status check needed
        dep_source = "bills"
        dep_total = sum(abs(_row_amount(r)) for r in bills_rows)
        deposits_for_count = bills_rows
        dep_success_count = len(bills_rows)
        dep_sample = bills_rows[:3]
        dep_statuses: list = []
    else:
        # Prefix match: catches USDT-TRC20, USDT-ERC20, USDC-ERC20 etc.
        deposits_filtered = [r for r in deposits_all
                              if any(str(r.get("coin", "")).upper().startswith(c) for c in coins)]
        dep_success = [r for r in deposits_filtered if _is_success(r)]
        dep_total = sum(_row_amount(r) for r in dep_success)
        deposits_for_count = deposits_filtered
        dep_success_count = len(dep_success)
        dep_sample = deposits_all[:3]
        dep_statuses = list({str(r.get("status", "")) for r in deposits_filtered})

    wdw_success = [r for r in withdrawals if _is_success(r)]
    wdw_total = sum(_row_amount(r) for r in wdw_success)
    wdw_statuses = list({str(r.get("status", "")) for r in withdrawals})

    logger.info(
        "Investment [%s]: dep=%.2f (%d records, %d windows) wdw=%.2f (%d/%d success)",
        dep_source, dep_total, len(deposits_for_count),
        dep_meta.get("windows_fetched", 0),
        wdw_total, len(wdw_success), len(withdrawals),
    )

    return {
        "deposits": round(dep_total, 2),
        "withdrawals": round(wdw_total, 2),
        "net": round(dep_total - wdw_total, 2),
        "deposit_count": len(deposits_for_count),
        "withdrawal_count": len(withdrawals),
        "deposit_success_count": dep_success_count,
        "withdrawal_success_count": len(wdw_success),
        "coins": coins,
        "coin": "+".join(coins),
        "_dep_source": dep_source,
        "_dep_statuses": dep_statuses,
        "_wdw_statuses": wdw_statuses,
        "_dep_meta": dep_meta,
        "_wdw_meta": wdw_meta,
        "_bills_meta": bills_meta,
        "_dep_all_coins": dep_all_coins,
        "_dep_raw_count": len(deposits_all),
        "_dep_sample": dep_sample,
        "_wdw_sample": withdrawals[:3],
    }


async def fetch_futures_leader_portfolio(api_key: str, secret: str, passphrase: str) -> dict:
    """Fetch the user's own futures copy-trading leader portfolio via the official API.

    Uses GET /api/v2/copy/mix-leader/profit-summary which returns balance, equity,
    total profit, AUM, follower count and ROI without needing the browser session.
    """
    path = "/api/v2/copy/mix-leader/profit-summary"
    qs = "?productType=umcbl"   # USDT-margined futures (umcbl); try dmcbl as fallback
    async with httpx.AsyncClient(timeout=10) as client:
        for product_type in ("umcbl", "dmcbl", "cmcbl"):
            qs = f"?productType={product_type}"
            hdrs = _auth_headers("GET", path + qs, "", api_key, secret, passphrase)
            try:
                r = await client.get(BITGET_API_BASE + path + qs, headers=hdrs)
                body = r.json()
            except Exception as exc:
                logger.warning("fetch_futures_leader_portfolio %s: %s", product_type, exc)
                continue
            code = str(body.get("code", ""))
            data = body.get("data") or {}
            logger.info("futures_leader %s: code=%s keys=%s", product_type, code, list(data.keys())[:10] if isinstance(data, dict) else "?")
            if code in ("00000", "0", "200") and isinstance(data, dict) and data:
                return {
                    "ok": True,
                    "product_type": product_type,
                    "balance": _safe_float(data.get("balance") or data.get("avlBalance") or data.get("availableBalance")),
                    "equity": _safe_float(data.get("equity") or data.get("totalEquity") or data.get("usdtEquity")),
                    "total_profit": _safe_float(data.get("totalProfit") or data.get("profit") or data.get("cumulativeProfit")),
                    "aum": _safe_float(data.get("aum") or data.get("totalAum")),
                    "copiers": int(float(data.get("followerNum") or data.get("followerCount") or data.get("copiers") or 0)),
                    "roi": _safe_float(data.get("roi") or data.get("profitRate")),
                    "raw": data,
                    "_code": code,
                }
        return {"ok": False, "error": "no data from any product type"}


def _safe_float(v) -> float:
    try:
        return round(float(v), 4) if v not in (None, "", "null") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_earn_apr(it: dict) -> float | None:
    """Extract APR % from an earn position dict, normalising decimal vs percent."""
    for f in ("annualInterestRate", "interestRate", "apr", "apy",
              "annualRate", "rate", "profitRate", "currentRate",
              "yearRate", "annualYield", "currentApr"):
        v = it.get(f)
        if v not in (None, ""):
            try:
                fv = float(v)
                # Bitget sometimes returns decimal (0.05) sometimes percent (5.0)
                return fv if fv > 1 else fv * 100
            except (TypeError, ValueError):
                pass
    return None


async def fetch_earn_balance(api_key: str, secret: str, passphrase: str) -> dict:
    """
    Return earn balance + interest data by calling both endpoints concurrently:
    - savings/assets  → balance (resultList)
    - savings/account → 24h interest and cumulative interest
    """
    path_assets = "/api/v2/earn/savings/assets"
    path_acct   = "/api/v2/earn/savings/account"

    async with httpx.AsyncClient(timeout=30) as client:
        hdrs_a = _auth_headers("GET", path_assets, "", api_key, secret, passphrase)
        hdrs_b = _auth_headers("GET", path_acct,   "", api_key, secret, passphrase)
        try:
            ra, rb = await asyncio.gather(
                client.get(BITGET_API_BASE + path_assets, headers=hdrs_a),
                client.get(BITGET_API_BASE + path_acct,   headers=hdrs_b),
            )
            ba, bb = ra.json(), rb.json()
        except Exception as exc:
            logger.warning("Earn API fetch error: %s", exc)
            return {"total": 0.0, "items": [], "apr": None,
                    "interest_24h": None, "total_interest": None,
                    "source": None, "error": str(exc)}

        # ── Balance from savings/assets ───────────────────────────────────────
        total = 0.0
        items: list[dict] = []
        code_a = str(ba.get("code", ""))
        data_a = ba.get("data") or {}
        if code_a in ("00000", "0", "200") and isinstance(data_a, dict):
            rows = (data_a.get("resultList") or data_a.get("list") or
                    data_a.get("assets") or data_a.get("rows") or [])
            for it in rows:
                if not isinstance(it, dict):
                    continue
                coin = str(it.get("productCoin") or it.get("coinName") or it.get("coin") or "")
                amt = 0.0
                for f in ("holdAmount", "amount", "totalAmount", "principal",
                          "holdingAmount", "currentAmount", "holdSize"):
                    v = it.get(f)
                    if v not in (None, "", "0", 0):
                        try:
                            amt = float(v); break
                        except (TypeError, ValueError):
                            pass
                if amt > 0:
                    item: dict = {"coin": coin, "amount": round(amt, 4)}
                    # Per-item 24h interest (may be coin-specific, e.g. USDC)
                    for f in ("24hEarning", "dailyEarning", "earningAmount",
                              "yesterdayEarning", "lastDayEarning"):
                        v = it.get(f)
                        if v not in (None, "", "0", 0):
                            try:
                                item["interest_24h"] = round(float(v), 6); break
                            except (TypeError, ValueError):
                                pass
                    items.append(item)
            total = sum(p["amount"] for p in items)

        # ── Interest from savings/account ─────────────────────────────────────
        interest_24h: float | None = None
        total_interest: float | None = None
        code_b = str(bb.get("code", ""))
        data_b = bb.get("data") or {}
        if code_b in ("00000", "0", "200") and isinstance(data_b, dict):
            for f in ("usdt24hEarning", "usdc24hEarning", "24hEarning",
                      "dailyEarning", "earningAmount"):
                v = data_b.get(f)
                if v not in (None, ""):
                    try:
                        interest_24h = round(float(v), 6); break
                    except (TypeError, ValueError):
                        pass
            # If the account endpoint only returns USDT interest, supplement from per-item interest
            if interest_24h is None:
                per_item_24h = [it["interest_24h"] for it in items if "interest_24h" in it]
                if per_item_24h:
                    interest_24h = round(sum(per_item_24h), 6)
            for f in ("usdtTotalEarning", "totalEarning", "totalInterest",
                      "accruedInterest", "cumulativeEarning"):
                v = data_b.get(f)
                if v not in (None, ""):
                    try:
                        total_interest = round(float(v), 4); break
                    except (TypeError, ValueError):
                        pass
            # Fall back to balance from account if assets call failed
            if not items:
                for f in ("usdtAmount", "totalUsdtAmount", "amount", "totalAmount"):
                    v = data_b.get(f)
                    if v not in (None, "", "0"):
                        try:
                            total = float(v)
                            items = [{"coin": "USDT", "amount": round(total, 4)}]
                            break
                        except (TypeError, ValueError):
                            pass

        logger.info("Earn: total=%.2f interest_24h=%s total_interest=%s",
                    total, interest_24h, total_interest)
        return {
            "total": round(total, 2),
            "items": items,
            "apr": None,
            "interest_24h": interest_24h,
            "total_interest": total_interest,
            "source": path_assets if items else path_acct,
            "error": None if (total > 0 or interest_24h is not None) else "no balance found",
        }
