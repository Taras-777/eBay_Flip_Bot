"""Дата виставлення оголошень і перевірка, чи вигідні пропозиції ще продаються."""
import asyncio
import time

import db
import deal_check
import handlers
import screen_common
import scheduler
from test_sales import _screen
from test_scheduler import consoles, make_app
from test_ui import Screen, make_context, make_update, _state
from conftest import patch_ui

DAY = 86400


def _deals(n=3, listed_ago=2 * DAY):
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15)
    ids = [db.add_deal(wid, f"v1|{i}|0", f"iPhone 16 Pro #{i}", 300 + i, "EUR", 620, 20,
                       f"https://www.ebay.de/itm/{i}", False, listed_at=int(time.time() - listed_ago))
           for i in range(n)]
    return wid, ids


def _age_checks(seconds):
    with db.get_conn() as conn:
        conn.execute("UPDATE deals SET checked_at = checked_at - ?", (seconds,))


# ---------- 📅 коли виставлено ----------

def test_listed_text():
    now = time.time()
    assert screen_common._listed(None) == ""
    assert screen_common._listed(now).startswith("сьогодні о ")
    assert screen_common._listed(now - 3 * DAY).endswith("(3 дні тому)")
    assert screen_common._listed(now - 10 * DAY).endswith("(10 днів тому)")


def test_listings_show_listed_date(monkeypatch):
    screen = Screen()
    patch_ui(monkeypatch, "show_panel", screen)
    state = _state(3)
    state["items"][0]["created_at"] = int(time.time() - 3 * DAY)
    asyncio.run(handlers._render_listing_panel(make_update(), make_context(), 7, state))
    assert screen.text.count("📅 виставлено") == 1 and "(3 дні тому)" in screen.text


def test_new_deal_remembers_listing_date(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    from conftest import listing
    fake_ebay.listings = [listing("cheap", "Sony PlayStation 5 Slim 1TB Konsole", 250,
                                  created_ago_s=2 * DAY)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    deal = db.get_inbox_deals(1)[0][0]
    assert abs(deal["listed_at"] - (time.time() - 2 * DAY)) < 60


# ---------- продані/зняті зникають зі списку ----------

def test_background_recheck_removes_gone(monkeypatch):
    _, ids = _deals(3)
    status = {"v1|0|0": True, "v1|1|0": False, "v1|2|0": None}
    asked = []
    monkeypatch.setattr(deal_check, "listing_available", lambda item: asked.append(item) or status[item])

    assert deal_check.recheck_deals() == 0 and asked == []     # щойно знайдені — ще свіжі
    _age_checks(31 * 60)
    assert deal_check.recheck_deals() == 1
    assert db.get_deal(ids[1])["status"] == "gone" and db.get_inbox_deals(1)[1] == 2
    asked.clear()
    assert deal_check.recheck_deals() == 0
    assert asked == ["v1|2|0"]    # «не вдалося дізнатися» — перепитаємо наступного разу


def test_below_min_profit_not_checked(monkeypatch):
    wid = db.add_watch(1, "iPhone", "iPhone", "", "", 15)
    db.add_deal(wid, "v1|x|0", "iPhone", 480, "EUR", 580, 20, "u", False)   # прибуток 6€ — не видно
    _age_checks(DAY)
    monkeypatch.setattr(deal_check, "listing_available", lambda item: 1 / 0)
    assert deal_check.recheck_deals() == 0


def test_opening_inbox_rechecks_page(monkeypatch):
    _, ids = _deals(3)
    _age_checks(11 * 60)
    monkeypatch.setattr(deal_check, "listing_available", lambda item: item != "v1|0|0")
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    text = shown[-1][0]
    assert "Прибрано вже проданих чи знятих: 1" in text and "(2)" in text
    assert "📅 виставлено" in text and "(2 дні тому)" in text
    assert db.get_deal(ids[0])["status"] == "gone"


def test_listing_available_uses_trading_then_browse(monkeypatch):
    monkeypatch.setattr(deal_check, "is_connected", lambda: True)
    monkeypatch.setattr(deal_check, "trading_calls_today", lambda: (0, None))
    monkeypatch.setattr(deal_check, "get_item_status", lambda item: {"result": "sold"})
    assert deal_check.listing_available("v1|5|0") is False
    monkeypatch.setattr(deal_check, "get_item_status", lambda item: {"result": "active"})
    assert deal_check.listing_available("v1|5|0") is True

    monkeypatch.setattr(deal_check, "is_connected", lambda: False)
    monkeypatch.setattr(deal_check, "browse_budget_left", lambda: 4000)
    monkeypatch.setattr(deal_check, "fetch_item_by_legacy_id", lambda legacy: None)
    assert deal_check.listing_available("v1|5|0") is False
    monkeypatch.setattr(deal_check, "browse_budget_left", lambda: 10)   # бюджет — для пошуку
    assert deal_check.listing_available("v1|5|0") is None
