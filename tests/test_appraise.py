"""«💶 Оцінити товар»: ціна продажу з усіх джерел, оцінка за оголошеннями, «👀 Спостерігати»."""
import asyncio
import time

from core import db
from analysis import appraise
from tests.test_sales import _sale, _screen

MAC = 'M3 Pro · 14" · 18GB'
TITLE = 'Apple MacBook Pro 14" M3 Pro 18GB 512GB Space Schwarz'


def _mac_watch(chat_id=2):
    return db.add_watch(chat_id, "MacBook Pro M3", "MacBook Pro M3", "", "", 25)


def test_estimate_from_sales_of_other_users_and_archive():
    wid = _mac_watch(chat_id=2)                                   # товар іншого користувача
    for i, p in enumerate((1400, 1450, 1500)):
        _sale(wid, f"a{i}", p, spec=MAC)
    arch = _mac_watch(chat_id=1)                                  # мій, в архіві
    for i, p in enumerate((1420, 1480)):
        _sale(arch, f"b{i}", p, spec=MAC)
    _sale(arch, "shop", 1900, cond="new", spec=MAC)               # магазин — не рахується
    db.archive_watch(arch, 1)
    spec, laptop = appraise.classify(TITLE, query="MacBook Pro M3")
    assert (spec, laptop) == (MAC, True)
    est = appraise.estimate(TITLE, spec, laptop, live=False)
    assert est["source"] == "sold" and est["sold_count"] == 5 and 1400 <= est["sale"] <= 1500


def test_estimate_falls_back_to_listings_with_few_sales(monkeypatch):
    wid = _mac_watch(chat_id=1)
    _sale(wid, "s1", 1450, spec=MAC)
    listings = [{"item_id": f"l{i}", "price": p, "spec_group": MAC, "cond_group": "used"}
                for i, p in enumerate((1500, 1550, 1600, 1650, 1700, 1750))]
    monkeypatch.setattr(appraise, "live_listings", lambda query, laptop: listings)
    est = appraise.estimate(TITLE, MAC, True, query="MacBook Pro M3")
    assert est["source"] == "listings" and est["listings_count"] == 6 and est["searched"]
    assert est["few_sold"][0] == 1 and 1500 <= est["sale"] < 1600            # нижня чверть цін


def test_appraise_link_profit_and_screen(monkeypatch):
    wid = _mac_watch(chat_id=2)
    for i, p in enumerate((1400, 1450, 1500, 1450, 1460)):
        _sale(wid, f"a{i}", p, spec=MAC)
    db.set_min_profit(1, 100)
    monkeypatch.setattr(appraise, "read_listing", lambda text: {
        "title": TITLE, "url": "https://www.ebay.de/itm/1", "total": 1250.0, "aspects": {}, "categories": [],
        "cond_group": "used"})
    res = appraise.appraise(1, "https://ebay.io/m/x")
    assert res["estimate"]["source"] == "sold" and res["query"] == "MacBook Pro M3"
    assert round(res["buy_limit"]) == round(res["estimate"]["sale"] - 7 - 100) and res["profit"] > 100

    from ui import screen_appraise
    text = screen_appraise.estimate_text(res)
    assert "за 5 продажами" in text and "✅ вигідно" in text and "Клас: M3 Pro" in text

    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("x")
    upd.callback_query = None
    upd.message.text = "https://ebay.io/m/x"
    ctx.user_data = {}
    state = asyncio.run(screen_appraise.appraise_text(upd, ctx))
    assert state == screen_appraise.APPRAISE_INPUT and "➕ Почати відстежувати" in shown[-1][1]
    assert "👀 Спостерігати 7 днів" not in shown[-1][1]                    # ціна за продажами — не треба


def test_name_too_short():
    import pytest
    with pytest.raises(appraise.AppraiseError):
        appraise.appraise(1, "PS5")


def test_observe_collects_then_reports_and_archives(monkeypatch):
    from ui import screen_appraise
    info = {"title": TITLE, "query": "MacBook Pro M3", "spec": MAC, "laptop": True}
    monkeypatch.setattr("analysis.shared_market.attach_shared_history", lambda w: 0)
    wid = screen_appraise._add_from_appraisal(1, info, observe=True)
    w = db.get_watch(wid, 1)
    assert w["observe_until"] > time.time() and db.observations_due() == []
    for i, p in enumerate((1400, 1450, 1500, 1450, 1460)):
        _sale(wid, f"o{i}", p, spec=MAC)
    assert [x["id"] for x in db.observations_due(time.time() + 8 * 86400)] == [wid]
    text, _ = screen_appraise.finish_observation(w)
    assert "Спостереження" in text and "за 5 продажами" in text and "архів" in text
    assert db.get_watch(wid, 1) is None and db.list_archived_watches(1)[0]["observe_until"] is None


def test_observed_watch_does_not_search_deals(monkeypatch):
    from core import scheduler
    wid = _mac_watch(chat_id=1)
    db.set_observe(wid, int(time.time()) + 86400)
    db.upsert_market_stats(wid, "used", "*", 1500, 20, sale_price=1450, sale_source="x")
    called = []
    monkeypatch.setattr(scheduler, "deal_scan", lambda w, limit: called.append(w) or [])
    asyncio.run(scheduler.check_one_watch(None, db.get_watch(wid, 1)))
    assert called == []
