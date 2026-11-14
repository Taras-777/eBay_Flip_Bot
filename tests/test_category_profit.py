"""Мін. прибуток категорії (€ чи % від ціни продажу) і персональний «💡 Що варто перепродавати»."""
import asyncio

from analysis import discovery
from core import db
from tests.test_sales import _screen


def test_rule_parsing_and_needed():
    assert db.parse_profit_rule("60") == ("eur", 60.0) and db.parse_profit_rule("12%") == ("pct", 12.0)
    assert db.parse_profit_rule("") is None and db.parse_profit_rule("x%") is None
    assert db.profit_needed(("pct", 12), 868) == 868 * 0.12 and db.profit_needed(("eur", 60), 868) == 60
    assert db.profit_rule_label(("pct", 12.0)) == "12%" and db.profit_rule_label(("eur", 60.0)) == "60€"


def test_order_own_category_general():
    db.set_min_profit(1, 100)
    ps5 = db.get_watch(db.add_watch(1, "PS5", "PlayStation 5", "", "", 15), 1)
    mac = db.get_watch(db.add_watch(1, "MacBook", "MacBook Pro M1 Pro", "", "", 15), 1)
    iphone = db.get_watch(db.add_watch(1, "iPhone", "iPhone 15 Pro", "", "", 15), 1)
    db.set_category_profit(1, "consoles", "60")
    db.set_category_profit(1, "laptops", "12%")
    assert db.min_profit_rule(ps5) == (("eur", 60.0), "category")
    assert db.watch_min_profit(mac, 868) == 868 * 0.12
    assert db.watch_min_profit(mac) == 100                          # без ціни продажу — загальний
    assert db.min_profit_rule(iphone) == (("eur", 100.0), "general")
    db.set_watch_min_profit(ps5, 80)
    assert db.min_profit_rule(db.get_watch(ps5["id"], 1)) == (("eur", 80.0), "own")   # свій — найголовніший
    db.set_category_profit(1, "consoles", None)
    assert db.get_category_profits(1) == {"laptops": ("pct", 12.0)}


def test_inbox_uses_category_percent():
    db.set_min_profit(1, 100)
    wid = db.add_watch(1, "MacBook", "MacBook Pro M1 Pro", "", "", 15)
    # продати 868€ → чистими 861€: прибуток 161 / 111 / 61
    for price in (700, 750, 800):
        db.add_deal(wid, f"v1|{price}|0", f"MacBook {price}", price, "EUR", 868, 20, "u", False)
    assert db.get_inbox_deals(1)[1] == 2                             # загальний 100€
    db.set_category_profit(1, "laptops", "15%")                      # 15% від 868€ = ~130€
    assert db.get_inbox_deals(1)[1] == 1 and db.count_unseen_deals(1) == 1
    db.set_category_profit(1, "laptops", "5%")                       # ~43€
    assert db.get_inbox_deals(1)[1] == 3
    assert len(db.get_deals_to_recheck(10, -60)) == 3


def test_discovery_personal_buy_limit(monkeypatch):
    base = {"emoji": "🎮", "name": "PlayStation 5 Slim", "query": "PlayStation 5 Slim", "sale": 560,
            "buy_limit": 503, "deals_now": 11, "deal_profit": 81, "deals_per_week": 11, "new_per_day": 10,
            "raw_prices": [440, 470, 500, 520, 560, 600], "sold_per_day": 2, "sale_source": "sold"}
    r = discovery.personalize(base, ("eur", 100))
    assert r["buy_limit"] == 453 and r["min_profit"] == 100 and r["deals_now"] == 1   # лише 440 ≤ 453
    assert r["deal_profit"] == 553 - 440
    assert discovery.candidate_group(base) == "consoles"

    monkeypatch.setattr(discovery, "CANDIDATES", [("🎮", "PlayStation 5 Slim", "PlayStation 5 Slim", 200)])
    monkeypatch.setattr(discovery, "get_discovery_results", lambda: [dict(base)])
    db.set_min_profit(1, 100)
    assert discovery.top_recommendations(1)[0]["buy_limit"] == 453
    db.set_category_profit(1, "consoles", "60")
    assert discovery.top_recommendations(1)[0]["buy_limit"] == 493
    db.set_category_profit(1, "consoles", "50%")                     # нічого не лишається — товару немає
    assert discovery.top_recommendations(1) == []


def test_settings_screens(monkeypatch):
    handlers, shown, press = _screen(monkeypatch)
    asyncio.run(handlers.min_profit_callback(*press("mprof:show")))
    assert "🎮 Консолі: як загальний" in shown[-1][1] and "Звідки береться поріг" in shown[-1][0]
    asyncio.run(handlers.category_profit_callback(*press("cprof:consoles")))
    assert "12%" in shown[-1][1] and "60€" in shown[-1][1]
    asyncio.run(handlers.category_profit_callback(*press("cprof:consoles:12p")))
    assert db.get_category_profit(1, "consoles") == ("pct", 12.0) and "🎮 Консолі: 12%" in shown[-1][1]
    asyncio.run(handlers.category_profit_callback(*press("cprof:consoles:def")))
    assert db.get_category_profit(1, "consoles") is None
    asyncio.run(handlers.category_profit_callback(*press("cprof:c999")))
    assert "Такої категорії вже немає" in shown[-1][0]
