"""«🛠 Нерозпізнані»: оголошення з неповним класом і рішення користувача."""
import asyncio

import pytest

import db
import market
import unrecognized
from test_sales import _screen

FULL = "RTX 4060 · i7 13 gen · 16GB"


@pytest.fixture(autouse=True)
def available(monkeypatch):
    """Без мережі: усі оголошення «ще продаються», якщо тест не каже інакше."""
    import deal_check
    state = {"gone": set()}
    monkeypatch.setattr(deal_check, "listing_available", lambda item_id: item_id not in state["gone"])
    unrecognized._checked.clear()
    return state
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


def test_main_menu_button_and_products_list(monkeypatch):
    import panel
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert not any(x.startswith("🛠") for x in labels)                     # немає — немає кнопки
    wid, watch = _setup()
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert "🛠 Нерозпізнані (1)" in labels

    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("unkall:0")
    ctx.user_data = {}
    asyncio.run(handlers.unknown_callback(upd, ctx))
    text, buttons = shown[-1]
    assert "🛠 <b>Нерозпізнані оголошення</b> (1)" in text and "📦 MSI Katana (1)" in buttons
    upd.callback_query.data = f"unk:{wid}:0:m"                              # товар зі списку меню
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert "◀️ До всіх товарів" in shown[-1][1] and "📌 До товару" in shown[-1][1]
    upd.callback_query.data = f"unk:{wid}:0:w"                              # той самий список з екрана товару
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert "◀️ До товару" in shown[-1][1] and "◀️ До всіх товарів" not in shown[-1][1]


def test_sold_or_ended_not_shown(monkeypatch, available):
    wid, watch = _setup()
    available["gone"].add("u1")                                             # уже продано на eBay
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"unk:{wid}:0")
    ctx.user_data = {}
    asyncio.run(handlers.unknown_callback(upd, ctx))
    text = shown[-1][0]
    assert "Прибрано вже проданих чи завершених: 1" in text and "Усе розпізнано" in text
    assert unrecognized.count_unrecognized(watch) == 0


def test_options_only_same_kind_and_kind_by_majority():
    wid, watch = _setup()
    db.update_listing_observations(wid, [{"item_id": "x", "title": "Speicher 32GB 64GB", "total_price": 50,
                                          "cond_group": "used", "spec_group": "32GB+64GB"}])
    item = unrecognized.unrecognized_items(watch)[0]
    assert "32GB+64GB" not in unrecognized.class_options(watch, item)
    # Товар не позначено як ноутбук, але більшість його оголошень — ноутбуки
    other = db.add_watch(1, "Gigabyte g5", "Gigabyte g5", "", "", 15)
    db.update_listing_observations(other, [
        {"item_id": f"g{i}", "title": f"Gigabyte G5 Nr{i}", "total_price": 700, "cond_group": "used",
         "spec_group": "RTX 3060 · i5 12 gen · 16GB"} for i in range(3)])
    g5 = db.get_watch(other, 1)
    assert unrecognized.item_kind(g5, {"spec_group": "unspecified"}) == "win"
    assert unrecognized.builder_steps(g5, {"spec_group": "unspecified"})[0]["local"] == ["RTX 3060"]


def test_build_class_step_by_step(monkeypatch):
    wid, watch = _setup()
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"unk:{wid}:0")
    ctx.user_data = {}
    asyncio.run(handlers.unknown_callback(upd, ctx))
    upd.callback_query.data = f"unkm:{wid}:0"
    asyncio.run(handlers.unknown_callback(upd, ctx))
    assert "🧩 Немає потрібного — зібрати клас" in shown[-1][1]

    def press_again(data):
        upd.callback_query.data = data
        asyncio.run(handlers.unknown_callback(upd, ctx))
        return shown[-1]

    text, buttons = press_again(f"unkb:{wid}:0")
    assert "крок 1 з 3: <b>відеокарта</b>" in text and buttons[0] == "RTX 4060" and "⏭ Невідомо" not in buttons
    text, buttons = press_again(f"unkbl:{wid}")                               # усі відеокарти
    k = buttons.index("RTX 3060")
    text, buttons = press_again(f"unkbp:{wid}:a:{k}")
    assert "крок 2 з 3: <b>процесор</b>" in text and "Вибрано: RTX 3060" in text
    text, buttons = press_again(f"unkbs:{wid}")                                # процесор невідомий
    assert "крок 3 з 3: <b>RAM</b>" in text
    text, buttons = press_again(f"unkbp:{wid}:l:{buttons.index('16GB')}")
    assert "Клас «RTX 3060 · 16GB» збережено" in text
    assert db.get_manual_specs(["u1"]) == {"u1": "RTX 3060 · 16GB"}


def test_item_refreshed_when_bot_now_knows_full_class():
    wid, watch = _setup()
    # Опис уже перечитано новою версією — RAM знайдено, а в історії ще старий неповний клас
    db.save_cached_spec("u1", "GPU ? · i7 13 gen · 16GB",
                        {"_beschreibung_geprueft": "2", "grafikkarte (aus beschreibung)": "RTX 4060"})
    assert unrecognized.unrecognized_items(watch) == []
    assert {r["item_id"]: r["spec_group"] for r in db.get_current_listings(wid)}["u1"] == FULL
