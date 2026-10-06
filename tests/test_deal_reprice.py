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
    assert "📊 Ціна продажу за 30 поточними оголошеннями (продажів ще мало) · 👤 приватні, усі конфігурації" in text
    assert "Для цієї конфігурації даних замало" in text           # 256GB окремо ще не пораховано

    db.upsert_market_stats(wid, "used", "256GB", 720, 12, sale_price=700, sale_source="за 7 проданими")
    asyncio.run(handlers.deals_callback(upd, ctx))
    text = shown[-1][0]
    assert "продати ~700€" in text and "📊 Ціна продажу за 7 проданими · 👤 приватні, 256GB" in text
    assert "🧩 Конфігурація: 256GB" in text                       # характеристики самого оголошення
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


def test_sold_only_uses_wider_sold_group():
    import market
    stats = {("used", "RTX 4060 · i7 13 gen · 16GB"): {"sale_source": "за поточними оголошеннями", "spec_group": "x"},
             ("used", "RTX 4060"): {"sale_source": "за 12 проданими", "spec_group": "RTX 4060"},
             ("used", "*"): {"sale_source": "за поточними оголошеннями", "spec_group": "*"}}
    it = {"cond_group": "used", "spec_group": "RTX 4060 · i7 13 gen · 16GB"}
    assert market._stat_for_item(stats, it)["spec_group"] == "x"                       # як раніше — найточніша
    assert market._stat_for_item(stats, it, sold_only=True)["spec_group"] == "RTX 4060"   # ширша, але за продажами
    other = {"cond_group": "used", "spec_group": "RTX 3050"}
    assert market._stat_for_item(stats, other, sold_only=True) is None                  # продажів ніде — не пропонуємо


def test_sold_only_hides_listing_priced_deals(monkeypatch):
    import account
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    sold = db.add_deal(wid, "a", "PS5 A", 300, "EUR", 620, 40, "u", False, sale_source="за 9 проданими")
    db.add_deal(wid, "b", "PS5 B", 300, "EUR", 620, 40, "u", False, sale_source="за поточними оголошеннями")
    assert db.get_inbox_deals(1)[1] == 2 and db.count_unseen_deals(1) == 2
    db.set_sold_only(1, True)
    deals, total = db.get_inbox_deals(1)
    assert total == 1 and deals[0]["id"] == sold and db.count_unseen_deals(1) == 1
    assert [r["id"] for r in db.get_deals_to_recheck(10, -1)] == [sold]       # приховані не перевіряємо

    from test_sales import _screen
    handlers_mod, shown, press = _screen(monkeypatch)
    monkeypatch.setattr(account, "show_panel", lambda u, c, text, reply_markup=None, parse_mode=None:
                        _record(shown, text, reply_markup))
    upd, ctx = press("soldonly")
    asyncio.run(account.sold_only_callback(upd, ctx))
    assert db.get_sold_only(1) is False and "як раніше" in shown[-1][0]
    assert any("продажі або оголошення" in b for b in shown[-1][1])


async def _record(shown, text, reply_markup):
    shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r]))


def test_scheduler_skips_listing_priced_deals_when_sold_only(fake_ebay):
    from conftest import listing
    from test_scheduler import consoles, make_app
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    db.set_sold_only(1, True)
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    fake_ebay.listings = [listing("cheap", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM deals").fetchone()["c"] == 0   # продажів ще немає


def test_laptop_card_shows_specs_and_sources(monkeypatch):
    wid = _laptop_watch()
    db.save_cached_spec("v1|k|0", "RTX 3050 · i7 · 16GB", {"grafikkarte (aus beschreibung)": "RTX 3050",
                                                          "_beschreibung_geprueft": "2"})
    db.upsert_market_stats(wid, "used", "*", 700, 30, sale_price=700, sale_source="за 7 проданими")
    db.add_deal(wid, "v1|k|0", "MSI Katana Gaming Notebook Core i7 16GB 512GB SSD RTX Win11", 440, "EUR", 700, 37,
                "u", False, cond_group="used", spec_group="*", item_spec="RTX 3050 · i7 · 16GB")
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (True, None))
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    assert "🧩 відеокарта: RTX 3050 (опис) · процесор: i7 (назва) · RAM: 16GB (назва)" in shown[-1][0]
