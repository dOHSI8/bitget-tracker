import asyncio

import bitget_api


def _row(coin, size, ts=1789004581198, status="success"):
    return {"coin": coin, "size": str(size), "status": status, "cTime": str(ts), "uTime": str(ts)}


def test_non_stable_deposits_valued_and_added(monkeypatch):
    deposits = [_row("USDT", 100), _row("USDC", 50), _row("ETH", 0.018),
                _row("ETH", 1, status="failed")]
    withdrawals = [_row("USDT", 30)]

    async def fake_windowed(client, path, params, *a, **k):
        if "deposit" in path:
            return deposits, {}
        return (withdrawals if params.get("coin") == "USDT" else []), {}

    async def fake_price(client, coin, ts):
        assert coin == "ETH" and ts == 1789004581198
        return 2500.0

    monkeypatch.setattr(bitget_api, "_get_windowed", fake_windowed)
    monkeypatch.setattr(bitget_api, "_usdt_price_at", fake_price)
    r = asyncio.run(bitget_api.fetch_net_investment("k", "s", "p", coins=["USDT", "USDC"]))
    assert r["other_deposits_usd"] == 45.0          # 0.018 * 2500; failed row ignored
    assert r["deposits"] == 195.0                   # 100 + 50 + 45
    assert r["net"] == 165.0
    assert r["other_deposits"][0]["coin"] == "ETH"


def test_unpriced_deposit_is_reported_not_counted(monkeypatch):
    async def fake_windowed(client, path, params, *a, **k):
        return ([_row("USDT", 10), _row("ETH", 1)] if "deposit" in path else []), {}

    async def no_price(client, coin, ts):
        return None

    monkeypatch.setattr(bitget_api, "_get_windowed", fake_windowed)
    monkeypatch.setattr(bitget_api, "_usdt_price_at", no_price)
    r = asyncio.run(bitget_api.fetch_net_investment("k", "s", "p", coins=["USDT"]))
    assert r["deposits"] == 10.0
    assert r["other_deposits"][0]["usd"] is None
