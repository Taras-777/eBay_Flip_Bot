"""«🛠 Нерозпізнані»: оголошення з неповним класом і рішення користувача."""
import asyncio

import db
import market
import unrecognized
from test_sales import _screen

FULL = "RTX 4060 · i7 13 gen · 16GB"
CHECKED = {"marke": "MSI", "_beschreibung_geprueft": "1"}


def _setup():
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15)
    obs = [{"item_id": f"f{i}", "title": f"MSI Katana i7-13620H RTX 4060 16GB Nr{i}", "total_price": 900 + i,
            "cond_group": "used", "spec_group": FULL, "url": f"https://www.ebay.de/itm/f{i}"} for i in range(3)]
    obs += [{"item_id": "u1", "title": "MSI Katana 15 B13VFK i7-13620H 16GB", "total_price": 600,
             "cond_group": "used", "spec_group": "GPU ? · i7 13 gen · 16GB", "url": "https://www.ebay.de/itm/u1"},
            {"item_id": "u2", "title": "MSI Katana 15 i7-13620H 16GB", "total_price": 650,
             "cond_group": "used", "spec_group": "GPU ? · i7 13 gen · 16GB", "url": "https://www.ebay.de/itm/u2"}]
    db.update_listing_observations(wid, obs)
    db.save_cached_spec("u1", "GPU ? · i7 13 gen · 16GB", CHECKED)          # опис прочитано — нічого не знайшов
    db.save_cached_spec("u2", "GPU ? · i7 13 gen · 16GB", {"marke": "MSI"})  # опис ще не читав — не показуємо
    db.upsert_market_stats(wid, "used", "*", 900, 10, sale_price=880, sale_source="x")
    return wid, db.get_watch(wid, 1)


def test_list_options_and_manual_class():
    wid, watch = _setup()
    items = unrecognized.unrecognized_items(watch)
    assert [it["item_id"] for it in items] == ["u1"]
    assert "відеокарта: ❓" in items[0]["info"] and "процесор: i7 13 gen (назва)" in items[0]["info"]
    assert round(items[0]["ratio"], 2) == round(600 / 880, 2)
    assert unrecognized.class_options(watch, items[0]) == [FULL]

    unrecognized.apply_manual_class(watch, "u1", FULL)
    assert unrecognized.unrecognized_items(watch) == []
    assert {r["item_id"]: r["spec_group"] for r in db.get_current_listings(wid)}["u1"] == FULL
    # Наступні сканування й щоденна переоцінка вибір користувача не перезаписують
    found = [{"item_id": "u1", "title": "MSI Katana 15 B13VFK i7-13620H 16GB", "total_price": 600}]
    market._annotate_items(found, watch=watch)
    assert found[0]["spec_group"] == FULL
    market.normalize_saved_specs()
    assert {r["item_id"]: r["spec_group"] for r in db.get_current_listings(wid)}["u1"] == FULL


def test_screen_keep_flag_and_report(monkeypatch):
    wid, watch = _setup()
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"unk:{wid}:0")
    ctx.user_data = {}
    asyncio.run(handlers.unknown_callback(upd, ctx))
    text, buttons = shown[-1]
    assert "🛠 <b>Нерозпізнані — MSI Katana</b> (1)" in text and "-32% від ціни групи" in text
    assert ["✏️ 1", "👌 1", "❌ 1", "🚩 1"] == buttons[:4]

    upd.callback_query.data = f"unkf:{wid}:0"
    asyncio.run(handlers.unknown_callback(upd, ctx))
    text, buttons = shown[-1]
    assert "Усе розпізнано" in text and "📋 Для розробника (1)" in buttons
    upd.callback_query.data = f"unkrep:{wid}"
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert "MSI Katana 15 B13VFK" in shown[-1][0] and "відеокарта: ❓" in shown[-1][0]
    upd.callback_query.data = f"unkrepc:{wid}"
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert db.get_flagged(wid) == []


def test_watch_screen_button_only_when_needed(monkeypatch):
    wid, watch = _setup()
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"watch_details:{wid}")
    asyncio.run(handlers.watch_details_callback(upd, ctx))
    assert "🛠 Нерозпізнані (1)" in shown[-1][1]
    db.set_review(wid, "u1", "keep")
    asyncio.run(handlers.watch_details_callback(upd, ctx))
    assert not any(b.startswith("🛠") for b in shown[-1][1])


def test_screen_manual_class(monkeypatch):
    wid, watch = _setup()
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"unk:{wid}:0")
    ctx.user_data = {}
    asyncio.run(handlers.unknown_callback(upd, ctx))
    upd.callback_query.data = f"unkm:{wid}:0"
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert FULL in shown[-1][1]
    upd.callback_query.data = f"unks:{wid}:0:0"
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert "збережено" in shown[-1][0] and db.get_manual_specs(["u1"]) == {"u1": FULL}
