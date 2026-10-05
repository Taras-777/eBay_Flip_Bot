"""«📉 Знизили ціну»: перша ціна, знижка ≥ порогу, не дорожче за ринок, екран і дії."""
import asyncio
import time

import db
import markdowns
import panel
from conftest import listing
from test_sales import _screen

DAY = 86400


def _item(item_id, price, **extra):
    return {"item_id": item_id, "title": f"iPhone 15 Pro 256GB {item_id}", "url": f"https://www.ebay.de/itm/{item_id}",
            "cond_group": "used", "spec_group": "256GB", "created_at": int(time.time() - 20 * DAY),
            "total_price": price, "has_best_offer": False, **extra}


def _setup(sale=600):
    wid = db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "", "", 15)
    db.upsert_market_stats(wid, "used", "256GB", sale + 30, 30, sale_price=sale, sale_source="за проданими")
    return wid


def _age(wid, item_id, first_seen_days=10):
    with db.get_conn() as conn:
        conn.execute("UPDATE price_track SET first_seen = ? WHERE watch_id = ? AND item_id = ?",
                     (int(time.time() - first_seen_days * DAY), wid, item_id))


# ---------- ціни ----------

def test_first_price_kept_and_drop_recorded():
    wid = _setup()
    db.track_prices(wid, [_item("a", 700), _item("v", 500, is_variation=True)])
    db.mark_track_seen([(wid, "a")])
    db.track_prices(wid, [_item("a", 700.3)])              # копійки — не знижка
    row = db.get_track_row(wid, "a")
    assert row["price_changed_at"] is None and row["seen_at"] is not None
    db.track_prices(wid, [_item("a", 540)])
    row = db.get_track_row(wid, "a")
    assert row["first_price"] == 700 and row["price"] == 540 and row["min_price"] == 540
    assert row["price_changed_at"] and row["seen_at"] is None   # нова знижка — знову 🆕
    db.track_prices(wid, [_item("a", 560)])                     # трохи підняв
    assert db.get_track_row(wid, "a")["min_price"] == 540
    assert db.get_track_row(wid, "v") is None                   # варіантні не відстежуємо


def test_markdown_filters():
    wid = _setup(sale=600)
    db.track_prices(wid, [_item("good", 700), _item("small", 700), _item("pricey", 900),
                          _item("young", 700, created_at=int(time.time() - 2 * DAY)),
                          _item("rej", 700), _item("deal", 700), _item("cheap", 650)])
    db.track_prices(wid, [_item("good", 540), _item("small", 600), _item("pricey", 700),
                          _item("young", 540), _item("rej", 540), _item("deal", 450), _item("cheap", 420)])
    db.reject_item(wid, "rej", "x")
    db.add_deal(wid, "deal", "iPhone", 450, "EUR", 600, 25, "u", False)
    rows = markdowns.markdown_list(1)
    assert [r["item_id"] for r in rows] == ["cheap", "good"]    # від найвигіднішої
    # small: −14% < 20%; pricey: −22%, але 700€ > ринку 600€; young: висить 2 дні; rej, deal — прибрані
    assert round(rows[1]["drop_pct"]) == 23 and rows[0]["price"] <= rows[0]["buy_limit"]

    db.set_drop_pct(1, 10)
    assert {r["item_id"] for r in markdowns.markdown_list(1)} == {"cheap", "good", "small"}


def test_listing_without_date_uses_first_seen_and_stale_hidden():
    wid = _setup()
    db.track_prices(wid, [_item("a", 700, created_at=None)])
    db.track_prices(wid, [_item("a", 500, created_at=None)])
    assert markdowns.markdown_list(1) == []                     # бот бачить його лише сьогодні
    _age(wid, "a")
    assert len(markdowns.markdown_list(1)) == 1
    with db.get_conn() as conn:
        conn.execute("UPDATE price_track SET last_seen = last_seen - 9 * 3600")
    assert markdowns.markdown_list(1) == []                     # давно не бачили — може, вже продано


# ---------- сканування найдешевших ----------

def test_cheapest_scan_tracks_and_respects_interval(fake_ebay):
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    fake_ebay.listings = [listing(f"p{i}", "Sony PlayStation 5 Slim 1TB Konsole", 300 + i) for i in range(5)]
    assert markdowns.run_markdown_scan() == 1
    assert db.get_track_row(wid, "p0")["first_price"] > 0
    assert markdowns.run_markdown_scan() == 0                   # раз на кілька годин


# ---------- екран ----------

def test_menu_button_only_when_list_not_empty():
    wid = _setup()
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert not any(x.startswith("📉") for x in labels)
    db.track_prices(wid, [_item("a", 700)])
    db.track_prices(wid, [_item("a", 520)])
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert labels[1] == "📉 Знизили ціну · 🆕 1"          # одразу під «🔥»


def test_screen_menu_counter_hide_and_undo(monkeypatch):
    wid = _setup()
    db.track_prices(wid, [_item("a", 700, has_best_offer=True), _item("b", 650)])
    db.track_prices(wid, [_item("a", 520, has_best_offer=True), _item("b", 430)])
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert "📉 Знизили ціну · 🆕 2" in labels

    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("mkd:0")
    ctx.user_data = {}
    asyncio.run(handlers.markdowns_callback(upd, ctx))
    text, buttons = shown[-1]
    assert "Знизили ціну</b> (2)" in text and "було 700€ → зараз <b>520€</b> (−26%)" in text
    assert "🎯 Можна торгуватись" in text and "✅ Вже вигідно" in text
    assert "📉 Знизили ціну" in [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]

    upd, ctx2 = press(f"mact:hide:{wid}:a:0")
    ctx2.user_data = {}
    asyncio.run(handlers.markdown_action_callback(upd, ctx2))
    assert "(1)" in shown[-1][0] and "a" in db.get_rejected_ids(wid)
    undo_id = db.latest_undo(1, 0)["id"]
    upd, ctx3 = press(f"undo:{undo_id}")
    ctx3.user_data = {"panel_state": {}}
    asyncio.run(handlers.undo_callback(upd, ctx3))
    assert "(2)" in shown[-1][0] and "a" not in db.get_rejected_ids(wid)


def test_screen_rechecks_and_drops_sold(monkeypatch):
    import deal_check
    wid = _setup()
    db.track_prices(wid, [_item("a", 700), _item("b", 650)])
    db.track_prices(wid, [_item("a", 520), _item("b", 430)])
    with db.get_conn() as conn:
        conn.execute("UPDATE price_track SET last_seen = last_seen - 3600")
    monkeypatch.setattr(deal_check, "listing_available", lambda item_id: item_id != "b")
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("mkd:0")
    ctx.user_data = {}
    asyncio.run(handlers.markdowns_callback(upd, ctx))
    assert "Прибрано вже проданих чи знятих: 1" in shown[-1][0] and "(1)" in shown[-1][0]


def test_drop_pct_setting(monkeypatch):
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("mdpct:30")
    asyncio.run(handlers.drop_pct_callback(upd, ctx))
    assert db.get_drop_pct(1) == 30 and "✅ 30%" in shown[-1][1] and "◀️ До налаштувань" in shown[-1][1]
    import account
    labels = [b.text for r in account._settings_keyboard(1).inline_keyboard for b in r]
    assert "📉 Поріг знижки: 30%" in labels


def test_deal_card_mentions_earlier_price_cut():
    import screen_deals
    wid = _setup()
    db.track_prices(wid, [_item("a", 700)])
    db.track_prices(wid, [_item("a", 450)])
    deal_id = db.add_deal(wid, "a", "iPhone", 450, "EUR", 600, 25, "u", False)
    d = {**db.get_deal(deal_id), "watch_label": "iPhone"}
    assert "📉 Продавець уже знизив ціну на 36% (було 700€)" in screen_deals._deal_card(1, d)


def test_card_shows_where_sale_price_comes_from():
    import screen_markdowns
    wid = _setup(sale=600)
    db.upsert_market_stats(wid, "used", "*", 650, 40, sale_price=620, sale_source="за 12 проданими")
    db.track_prices(wid, [_item("a", 700), _item("b", 700, spec_group="512GB")])
    db.track_prices(wid, [_item("a", 540), _item("b", 540, spec_group="512GB")])
    rows = {r["item_id"]: r for r in markdowns.markdown_list(1)}
    own = screen_markdowns._card(1, rows["a"])
    assert "📊 Ціна продажу за проданими · 👤 приватні, 256GB" in own and "⚠️" not in own
    wide = screen_markdowns._card(2, rows["b"])                 # 512GB немає — ціна за всіма
    assert "📊 Ціна продажу за 12 проданими · 👤 приватні, усі конфігурації" in wide and "⚠️" in wide


def test_clear_list_returns_only_after_new_drop(monkeypatch):
    wid = _setup()
    db.track_prices(wid, [_item("a", 700), _item("b", 650)])
    db.track_prices(wid, [_item("a", 520), _item("b", 430)])
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("mkd:0")
    ctx.user_data = {}
    asyncio.run(handlers.markdowns_callback(upd, ctx))
    assert "🧹 Очистити список" in shown[-1][1]
    upd.callback_query.data = "mkdclr"
    asyncio.run(handlers.markdowns_clear_callback(upd, ctx))
    assert "Очистити «📉 Знизили ціну»?" in shown[-1][0] and markdowns.markdown_list(1)   # спершу питає
    upd.callback_query.data = "mkdclr:yes"
    asyncio.run(handlers.markdowns_clear_callback(upd, ctx))
    assert "Список очищено (2)" in shown[-1][0] and markdowns.markdown_list(1) == []
    assert not any(b.text.startswith("📉") for r in panel.build_main_menu(1).inline_keyboard for b in r)
    db.track_prices(wid, [_item("a", 480), _item("b", 430)])          # «a» подешевшало ще — повертається
    assert [r["item_id"] for r in markdowns.markdown_list(1)] == ["a"]
