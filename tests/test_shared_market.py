"""Спільний ринок: однакові товари різних користувачів сканують eBay один раз."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import db
import ebay_api
import scheduler
import shared_market
from conftest import listing
from test_scheduler import consoles, make_app

CATS = [{"id": "139971", "name": "Konsolen"}]


def search_calls(fake):
    return sum(1 for url in fake.calls if url == ebay_api.SEARCH_URL)


def test_same_product_two_users_one_scan(fake_ebay):
    fake_ebay.listings = consoles()
    w1 = db.add_watch(1, "PS5", "PlayStation 5 Slim", "", "", 15, categories=CATS)
    w2 = db.add_watch(2, "PS5", "slim playstation 5", "", "", 15, categories=CATS)   # інший порядок слів
    assert shared_market.market_key(db.get_watch(w1, 1)) == shared_market.market_key(db.get_watch(w2, 2))

    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    market_calls = search_calls(fake_ebay)
    assert db.get_market_stats(w1) and db.get_market_stats(w2)        # ціни в обох

    fake_ebay.calls.clear()
    asyncio.run(scheduler.check_all_watches(app))                      # пошук нових лотів
    assert search_calls(fake_ebay) == 1                                # один запит на двох
    assert market_calls == 2   # ринок: 2 сторінки (друга порожня) — теж один раз на двох


def test_different_categories_are_not_shared(fake_ebay):
    w1 = db.add_watch(1, "PS5", "PlayStation 5 Slim", "", "", 15, categories=CATS)
    w2 = db.add_watch(2, "PS5", "PlayStation 5 Slim", "", "", 15)
    assert shared_market.market_key(db.get_watch(w1, 1)) != shared_market.market_key(db.get_watch(w2, 2))


def test_shared_search_uses_softest_settings_and_own_filters():
    w1 = db.get_watch(db.add_watch(1, "PS5", "PS5", "defekt kaputt", "3000", 15, min_price=300), 1)
    w2 = db.get_watch(db.add_watch(2, "PS5", "PS5", "defekt", "1000,3000", 15, min_price=250), 2)
    kw = shared_market.shared_search_kwargs(w1)
    assert kw["condition_ids"] == "1000,3000" and kw["exclude_terms"] == "defekt" and kw["min_price"] == 250

    items = [
        {"title": "PS5 neu", "condition_id": "1000", "total_price": 400},
        {"title": "PS5 kaputt", "condition_id": "3000", "total_price": 400},
        {"title": "PS5", "condition_id": "3000", "total_price": 280},
        {"title": "PS5", "condition_id": "3000", "total_price": 350},
    ]
    assert [it["total_price"] for it in shared_market.own_filter(w1, items)] == [350]   # лише вживані від 300
    assert len(shared_market.own_filter(w2, items)) == 4                                 # «kaputt» і нові — для нього ок


def test_new_user_gets_history_and_hint(monkeypatch):
    import access
    import handlers
    w1 = db.add_watch(1, "iPhone", "iPhone 16 Pro", "", "", 15, categories=[{"id": "9355", "name": "Handys"}])
    now = int(time.time())
    with db.get_conn() as conn:
        for item, title, status in (("s1", "iPhone 16 Pro 256GB", "gone"), ("s2", "iPhone 16 Pro defekt", "gone"),
                                    ("a1", "iPhone 16 Pro 128GB", "active")):
            conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price, first_seen,
                            last_seen, status, gone_at, title) VALUES (?, ?, 'used', '256GB', 600, ?, ?, ?, ?, ?)""",
                         (w1, item, now, now, status, now if status == "gone" else None, title))

    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r] if reply_markup else []))

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(handlers, "_ebay_configured", lambda: True)
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 2
    upd.message.text = "iphone 16 pro"
    ctx = MagicMock()
    ctx.user_data = {}
    state = asyncio.run(handlers.addwatch_got_query(upd, ctx))
    assert state == handlers.ASK_CATEGORY
    assert "уже аналізується ботом" in shown[-1][0] and "✅ Підключитися" in shown[-1][1]
    assert "помічено продажів: 2" in shown[-1][0]

    went_to = []

    async def fake_aspects(update, context):
        went_to.append(context.user_data["new_watch_categories"])
        return handlers.ASK_ASPECT

    monkeypatch.setattr(handlers, "_propose_aspects", fake_aspects)
    upd.callback_query.data = "shr:yes"
    upd.callback_query.answer = AsyncMock()
    asyncio.run(handlers.addwatch_shared_choice(upd, ctx))
    assert went_to == [[{"id": "9355", "name": "Handys"}]]              # ті самі категорії

    # Після додавання — історія «сусіда» з урахуванням власних виключених слів
    w2 = db.get_watch(db.add_watch(2, "iPhone", "iPhone 16 Pro", "defekt", "", 15,
                                   categories=[{"id": "9355", "name": "Handys"}]), 2)
    assert shared_market.attach_shared_history(w2) == 2                 # «defekt» не скопійовано
    assert db.watch_obs_summary(w2["id"])["sold"] == 1


def test_sold_check_not_repeated_for_shared_item(monkeypatch):
    import trading_api
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, price, first_seen, last_seen,
                        status, gone_at, sold_check, checked_at) VALUES (1, 'x', 'used', 500, ?, ?, 'gone', ?, 'sold', ?)""",
                     (now, now, now, now))
        conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, price, first_seen, last_seen,
                        status, gone_at, sold_check) VALUES (2, 'x', 'used', 500, ?, ?, 'gone', ?, 'pending')""",
                     (now, now, now))
    monkeypatch.setattr(trading_api, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "get_item_status", lambda item_id: (_ for _ in ()).throw(AssertionError("зайвий запит")))
    assert trading_api.verify_disappeared() == 1
    assert db.get_pending_sold_checks(10) == []