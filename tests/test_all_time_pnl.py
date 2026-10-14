import main


def _summary(**kw):
    base = {"total_balance": 334.51, "total_investment": 665.88, "trade_pnl": -4.37,
            "daily_pnl": 0.0, "open_positions": 0, "open_positions_pnl": 0.0,
            "pushed_at": None}
    return {**base, **kw}


def test_all_time_is_balance_minus_net_invested(monkeypatch):
    # Live figures from 2026-10-13: copy 334.51 + Earn 270.41 − invested 665.88
    monkeypatch.setitem(main._earn, "data", {"total": 270.41})
    monkeypatch.setitem(main._elite, "data", {"balance": 0.0})
    monkeypatch.setitem(main._elite_overview, "data", {"active": False, "equity": 99})
    s = main._with_balance_pnl(_summary())
    assert s["grand_total_balance"] == 604.92
    assert s["all_time_pnl"] == -60.96
    assert s["all_time_pnl_basis"] == "balance"


def test_falls_back_to_trade_sum_without_investment(monkeypatch):
    monkeypatch.setitem(main._earn, "data", None)
    monkeypatch.setitem(main._elite, "data", None)
    monkeypatch.setitem(main._elite_overview, "data", None)
    s = main._with_balance_pnl(_summary(total_investment=0.0))
    assert s["all_time_pnl"] == -4.37 and s["all_time_pnl_basis"] == "trades"


def test_widget_uses_same_figures(client, monkeypatch):
    monkeypatch.setitem(main._earn, "data", {"total": 270.41})
    monkeypatch.setitem(main._elite, "data", None)
    monkeypatch.setitem(main._elite_overview, "data", None)
    monkeypatch.setitem(main._mt5, "summary", _summary())
    w = client.get("/api/widget").json()
    assert w["total_balance"] == 604.92 and w["all_time_pnl"] == -60.96
    assert client.get("/api/mt5").json()["all_time_pnl"] == -60.96
