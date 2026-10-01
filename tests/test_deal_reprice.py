"""«🔥 Вигідні пропозиції»: «продати» — за теперішньою статистикою ринку, з поясненням, звідки ціна."""
import asyncio

import db
import deal_check
import deal_reprice
import scheduler
from test_deal_check import DAY
from test_sales import _screen
from test_scheduler import consoles, make_app

LAPTOPS = [{"id": "175672", "name": "Notebooks & Netbooks"}]


def _laptop_watch():
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15, categories=LAPTOPS)
    db.upsert_market_stats(wid, "used", "*", 1500, 40, sale_price=1477, sale_source="за поточними оголошеннями")
    db.upsert_market_stats(wid, "used", "RTX 3050 Ti", 900, 9, sale_price=850, sale_source="за 6 проданими")
    return wid


def _old_deal(wid, title, price, sale=1477):
    """Знахідка до класів ноутбуків: ціна з групи «усі конфігурації», власної конфігурації немає."""
    return db.add_deal(wid, f"v1|{title}|0", title, price, "EUR", sale, 40, "u", False,
                       cond_group="used", spec_group="*")


def test_old_laptop_deal_repriced_by_its_class():
    wid = _laptop_watch()
    bad = _old_deal(wid, "MSI Katana GF76 i7-11800H RTX 3050 Ti 16GB", 750)
    good = _old_deal(wid, "MSI Katana GF66 i5-11400H RTX 3050 Ti 8GB", 600)
    assert db.get_inbox_deals(1)[1] == 2
    assert deal_reprice.reprice_deals(1) == 1                    # 750€ при ринку 850€ — уже невигідно
    deals = {d["id"]: d for d in deal_reprice.get_open_deals(1)}
    assert deals[bad]["median_price"] == 850 and deals[bad]["spec_group"] == "RTX 3050 Ti"
    assert deals[bad]["item_spec"] == "RTX 3050 Ti · i7 11 gen · 16GB"
    assert [d["id"] for d in db.get_inbox_deals(1)[0]] == [good]
    assert deal_reprice.reprice_deals(1) == 0                    # повторно не рахується
    assert db.get_deal(bad)["status"] == "new"                   # не видалено: ринок може підрости


def test_laptop_without_gpu_removed():
    wid = _laptop_watch()
    did = _old_deal(wid, "MSI Katana 15 i7-12650H 16GB", 700)
    assert deal_reprice.reprice_deals(1) == 1 and db.get_deal(did)["status"] == "stale"


def test_no_market_stats_keeps_deal():
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15)
    did = db.add_deal(wid, "v1|1|0", "iPhone 16 Pro 256GB", 400, "EUR", 620, 20, "u", False, cond_group="used",
                      spec_group="*")
    assert deal_reprice.reprice_deals(1) == 0 and db.get_deal(did)["median_price"] == 620


def test_card_explains_price(monkeypatch):
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15)
    db.upsert_market_stats(wid, "used", "*", 650, 30, sale_price=600, sale_source="за поточними оголошеннями")
    db.add_deal(wid, "v1|a|0", "iPhone 16 Pro 256GB Schwarz", 400, "EUR", 600, 30, "u", False,
                cond_group="used", spec_group="*")
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (True, None))
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    text = shown[-1][0]
    assert "📊 Ціна продажу за 30 поточними оголошеннями (продажів ще мало) · вживані, усі конфігурації" in text
    assert "Для цієї конфігурації даних замало" in text           # 256GB окремо ще не пораховано

    db.upsert_market_stats(wid, "used", "256GB", 720, 12, sale_price=700, sale_source="за 7 проданими")
    asyncio.run(handlers.deals_callback(upd, ctx))
    text = shown[-1][0]
    assert "продати ~700€" in text and "📊 Ціна продажу за 7 проданими · вживані, 256GB" in text
    assert "даних замало" not in text and "перераховано" not in text   # нічого не прибрано


def test_reprice_note_on_screen(monkeypatch):
    wid = _laptop_watch()
    _old_deal(wid, "MSI Katana GF76 i7-11800H RTX 3050 Ti 16GB", 750)
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (1 / 0, None))   # невигідні не перевіряємо
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    assert "прибрано невигідних: 1" in shown[-1][0] and "(0)" in shown[-1][0]


def test_new_deal_stores_price_basis(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    from conftest import listing
    fake_ebay.listings = [listing("cheap", "Sony PlayStation 5 Slim 1TB Konsole", 250,
                                  created_ago_s=2 * DAY)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    deal = db.get_inbox_deals(1)[0][0]
    assert deal["sale_source"] and deal["sale_sample"] and deal["item_spec"]
    assert deal_reprice.basis_line(deal).startswith("📊 Ціна продажу за ")
