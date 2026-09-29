"""Аналітика продажів: швидкість, «не продається», падіння цін — і як це видно в сповіщеннях."""
import asyncio
import time
from datetime import date, timedelta

import db
import sales
import market
import scheduler
from conftest import listing
from test_scheduler import consoles, make_app

DAY = 86400


def sold_row(price, spec="256GB", cond="used", days_ago=1, listed_days=3):
    gone = time.time() - days_ago * DAY
    return {"price": price, "cond_group": cond, "spec_group": spec,
            "gone_at": gone, "created_at": gone - listed_days * DAY}


# ---------- швидкість і підсумок ----------

def test_summary_and_note():
    rows = [sold_row(600, listed_days=2), sold_row(640, listed_days=4, days_ago=9), sold_row(620, listed_days=3)]
    s = sales.summarize(rows)
    assert s["count"] == 3 and s["week"] == 2 and s["median_price"] == 620 and round(s["median_days"]) == 3
    note = sales.sales_note(rows, "used", "256GB")
    assert note == "🛒 Такі ж: 3 продажі за 14 днів, типова ціна 620€, продаються за ~3 дні"
    assert sales.sales_note(rows, "used", "512GB") == ""
    assert "3 продажі" in sales.sales_note(rows, "used", "*")  # «усі конфігурації» стану


def test_speed_text():
    assert sales.speed_text(None) == ""
    assert sales.speed_text(0.4) == "менше ніж за добу"
    assert sales.speed_text(1.2) == "за ~1 день"
    assert sales.speed_text(5) == "за ~5 днів"


def test_old_sales_outside_window_ignored():
    rows = [sold_row(600, days_ago=20)]
    assert sales.sales_note(rows, "used", "256GB") == ""


# ---------- «не продається» ----------

def test_slow_seller():
    busy = [sold_row(600, spec="256GB") for _ in range(10)]
    assert sales.is_slow_seller(busy, "used", "1TB")          # ринок живий, а 1TB не йде
    assert not sales.is_slow_seller(busy, "used", "256GB")
    assert not sales.is_slow_seller(busy, "used", "unspecified")
    assert not sales.is_slow_seller(busy[:5], "used", "1TB")  # даних замало — не фільтруємо


# ---------- падіння цін ----------

def history(watch_id, prices_by_days_ago, cond="used", spec="256GB"):
    with db.get_conn() as conn:
        for days_ago, price in prices_by_days_ago.items():
            day = (date.today() - timedelta(days=days_ago)).isoformat()
            conn.execute("INSERT OR REPLACE INTO price_history VALUES (?, ?, ?, ?, ?, NULL)",
                         (watch_id, cond, spec, day, price))


def test_weekly_change():
    today = date(2026, 9, 28)
    points = [("2026-09-21", 650.0), ("2026-09-25", 630.0), ("2026-09-28", 598.0)]
    pct, old, new = sales.weekly_change(points, today)
    assert (old, new) == (650.0, 598.0) and round(pct) == -8
    assert sales.weekly_change([("2026-09-28", 598.0)], today) is None           # історії ще немає
    assert sales.weekly_change([("2026-09-21", 650.0), ("2026-09-27", 600.0)], today) is None  # сьогодні не рахували


def test_price_drop_alert_once_per_week():
    history(1, {7: 650, 0: 598})
    today = date.today()
    drops = sales.price_drops(1, today)
    assert [(c, s, round(p)) for c, s, _, _, p in drops] == [("used", "256GB", -8)]
    assert sales.price_drops(1, today) == []  # повторно того ж тижня — ні


def test_small_change_no_alert_and_star_group_skipped():
    history(1, {7: 650, 0: 630})                       # −3% — нормальні коливання
    history(1, {7: 700, 0: 600}, spec="*")              # «усі» — є окремі конфігурації
    assert sales.price_drops(1, date.today()) == []


# ---------- у фоновій перевірці ----------

def seed_sales(watch_id, spec, n):
    now = int(time.time())
    with db.get_conn() as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price, created_at,
                                            first_seen, last_seen, status, gone_at)
                   VALUES (?, ?, 'used', ?, ?, ?, ?, ?, 'gone', ?)""",
                (watch_id, f"s{spec}{i}", spec, 480, now - 3 * DAY, now - 3 * DAY, now - DAY, now - DAY),
            )


def watch_with_market(fake_ebay):
    fake_ebay.listings = consoles()
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    return wid, app


def deal_messages(app):
    return [m for m in app.bot.messages if "вигідн" in m.lower()]


def test_deal_notification_shows_sales(fake_ebay):
    wid, app = watch_with_market(fake_ebay)
    seed_sales(wid, "1TB", 4)
    fake_ebay.listings = [listing("deal", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    assert len(deal_messages(app)) == 1
    import handlers
    deals, _ = db.get_inbox_deals(1)
    card = handlers._deal_card(1, deals[0], db.get_sold_listings(wid, 14))
    assert "🛒 Такі ж: 4 продажі за 14 днів" in card and "за ~2 дні" in card


def test_slow_configuration_is_not_notified(fake_ebay):
    wid, app = watch_with_market(fake_ebay)
    seed_sales(wid, "825GB", 12)   # товар продається, але не 1TB
    fake_ebay.listings = [listing("deal", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles()
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    assert deal_messages(app) == []


def test_market_scan_records_price_history(fake_ebay):
    wid, _ = watch_with_market(fake_ebay)
    history_rows = db.get_price_history(wid)
    assert history_rows and all(len(points) == 1 for points in history_rows.values())


# ---------- «🔄 Оновити запити» оновлює і ціни всіх товарів ----------

def test_refresh_button_recalculates_all_watches(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import handlers

    w1 = db.add_watch(1, "PS5", "PS5", "", "", 15)
    w2 = db.add_watch(1, "iPhone", "iPhone 16", "", "", 15)
    db.add_watch(2, "Чужий", "Xbox", "", "", 15)  # чужий товар не чіпаємо
    recalculated, fetched, shown = [], [], []

    async def fake_recalc(watch, replace_existing=False):
        recalculated.append(watch["id"])
        return [], ({("used", "*"): {}} if watch["id"] == w1 else {}), []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append(text)

    monkeypatch.setattr(handlers, "is_owner", lambda uid: True)
    monkeypatch.setattr(handlers, "_recalculate_watch_medians", fake_recalc)
    monkeypatch.setattr(handlers, "fetch_browse_rate_limit", lambda: fetched.append(1))
    monkeypatch.setattr(handlers, "browse_budget_left", lambda: 4000)
    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(handlers, "main_menu_text", lambda uid: "MENU")
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.answer = AsyncMock()
    ctx = MagicMock()
    ctx.bot_data = {}

    asyncio.run(handlers.refresh_all_callback(upd, ctx))
    assert sorted(recalculated) == sorted([w1, w2]) and fetched == [1]
    assert any("(1/2)" in t for t in shown)
    assert "✅ Ціни оновлено: 1 товар" in shown[-1] and "Не вдалося порахувати: iPhone" in shown[-1]
    assert ctx.bot_data["refresh_all_running"] is False


def test_main_menu_shows_when_prices_were_updated(monkeypatch):
    import panel
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    assert "Ціни товарів оновлено" not in panel.main_menu_text(1)   # ще не рахувались
    db.upsert_market_stats(wid, "used", "*", 450, 20, sale_price=420, sale_source="x")
    assert "💰 Ціни товарів оновлено: сьогодні о " in panel.main_menu_text(1)
    assert "Ціни товарів оновлено" not in panel.main_menu_text(2)   # чужі товари не рахуються
    with db.get_conn() as conn:
        conn.execute("UPDATE market_stats SET updated_at = updated_at - 86400")
    assert "💰 Ціни товарів оновлено: вчора о " in panel.main_menu_text(1)


# ---------- «🔥 Вигідні пропозиції» ----------

def _inbox_setup(n=3):
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15)
    ids = [db.add_deal(wid, f"v1|{i}|0", f"iPhone 16 Pro 256GB #{i}", 400 + i, "EUR", 620, 20,
                       f"https://www.ebay.de/itm/{i}", False, cond_group="used", spec_group="256GB")
           for i in range(n)]
    return wid, ids


def _screen(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import access
    import handlers
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r]))

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)

    def press(data):
        upd = MagicMock()
        upd.effective_chat.id = upd.effective_user.id = 1
        upd.callback_query.data = data
        upd.callback_query.answer = AsyncMock()
        ctx = MagicMock()
        ctx.bot_data = {}
        return upd, ctx
    return handlers, shown, press


def test_menu_badge_and_inbox_marks_seen(monkeypatch):
    import panel
    _inbox_setup(3)
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert "🔥 Вигідні пропозиції · 🆕 3" in labels

    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    text, buttons = shown[-1]
    assert "Вигідні пропозиції</b> (3)" in text and text.count("🆕") == 3
    assert "✅ 1 Куплено" in buttons and "🧹 Очистити список" in buttons
    assert db.count_unseen_deals(1) == 0
    labels = [b.text for r in panel.build_main_menu(1).inline_keyboard for b in r]
    assert "🔥 Вигідні пропозиції" in labels   # лічильник зник


def test_inbox_actions(monkeypatch):
    wid, ids = _inbox_setup(3)
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"dact:buy:{ids[0]}:0")
    asyncio.run(handlers.deal_inbox_action_callback(upd, ctx))
    assert db.get_deal(ids[0])["status"] == "bought" and "(2)" in shown[-1][0]

    upd, ctx = press(f"dact:hide:{ids[1]}:0")
    asyncio.run(handlers.deal_inbox_action_callback(upd, ctx))
    assert db.get_deal(ids[1])["status"] == "skipped"
    assert "v1|1|0" in db.get_rejected_ids(wid)

    upd, ctx = press("dclear")
    asyncio.run(handlers.deals_clear_callback(upd, ctx))
    assert db.get_inbox_deals(1)[1] == 0 and "Поки нових немає" in shown[-1][0]


def test_inbox_pagination(monkeypatch):
    _inbox_setup(7)
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    assert "Старіші ▶️" in shown[-1][1] and "5. " in shown[-1][0]
    upd, ctx = press("deals:1")
    asyncio.run(handlers.deals_callback(upd, ctx))
    assert "◀️ Новіші" in shown[-1][1] and "7. " in shown[-1][0]


def test_menu_shows_last_scan_summary(fake_ebay):
    import panel
    fake_ebay.listings = consoles()
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))          # перше сканування — усе нове
    summary = db.get_scan_summary(1)
    assert summary["fetched"] == 20 and summary["kept"] == 20 and summary["new_count"] == 20
    text = panel.main_menu_text(1)
    assert "🔎 Останнє сканування: переглянуто <b>20</b> оголошень · підійшло <b>20</b> · нових у базі <b>20</b>" in text

    # Наступне сканування: 2 нові оголошення, решта вже в базі
    fake_ebay.listings = [listing("n1", "Sony PlayStation 5 Slim 1TB Konsole", 470),
                          listing("n2", "Sony PlayStation 5 Slim 1TB Konsole", 480)] + consoles()
    asyncio.run(market._recalculate_watch_medians(db.get_watch(wid, 1), replace_existing=True))
    assert db.get_scan_summary(1)["new_count"] == 2 and db.get_scan_summary(1)["fetched"] == 22


# ---------- ⚙️ мінімальний прибуток ----------

def test_min_profit_setting_filters_inbox(monkeypatch):
    wid = db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "", "", 15)
    # продати ~580€ → чистими 580*0.85-7 = 486€
    for price in (480, 426, 400, 300):                 # прибуток: 6, 60, 86, 186
        db.add_deal(wid, f"v1|{price}|0", f"iPhone {price}", price, "EUR", 580, 20, "u", False)
    assert db.get_inbox_deals(1)[1] == 3               # за замовчуванням ≥50€ — без «6€»
    db.set_min_profit(1, 80)
    assert db.get_inbox_deals(1)[1] == 2 and db.count_unseen_deals(1) == 2
    db.set_min_profit(1, 15)
    assert db.get_inbox_deals(1)[1] == 3               # 6€ і нижче однаково не показуємо

    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("mprof:60")
    asyncio.run(handlers.min_profit_callback(upd, ctx))
    assert db.get_min_profit(1) == 60 and "✅ 60€" in shown[-1][1] and "Збережено" in shown[-1][0]


def test_best_offer_does_not_make_loss_a_deal(fake_ebay):
    """«Можна торгуватись» більше не робить пропозицію вигідною — рахується ціна оголошення."""
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    sale = db.get_market_stats(1)[0]["sale_price"]
    limit = market.max_buy_price(sale, 50)
    fake_ebay.listings = [listing("bo", "Sony PlayStation 5 Slim 1TB Konsole", round(limit + 20),
                                  buyingOptions=["FIXED_PRICE", "BEST_OFFER"])] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    assert db.get_inbox_deals(1)[1] == 0