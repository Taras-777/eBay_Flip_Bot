"""«💡 Що перепродавати»: аналіз популярних товарів і рейтинг."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import access
import db
import discovery
import handlers
from conftest import listing, patch_ui


def market(title, prices, prefix="x", age_s=3600):
    return [listing(f"{prefix}{i}", title, p, created_ago_s=age_s + i) for i, p in enumerate(prices)]


# 20 вживаних Switch OLED по 250–345€ і кілька дешевих — вигідних
SWITCH = market("Nintendo Switch OLED Konsole 64GB weiß", [250 + i * 5 for i in range(20)]) + \
    market("Nintendo Switch OLED 64GB", [160, 170], prefix="cheap")


@pytest.fixture
def one_candidate(monkeypatch):
    monkeypatch.setattr(discovery, "CANDIDATES",
                        [("🎮", "Nintendo Switch OLED", "Nintendo Switch OLED", 150)])
    monkeypatch.setattr(discovery, "browse_budget_left", lambda: 4000)


def test_candidate_metrics(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH
    r = discovery.analyze_candidate("🎮", "Nintendo Switch OLED", "Nintendo Switch OLED", 150)
    assert r["spec"] == "64GB"
    assert r["median"] > r["buy_limit"]
    assert r["deals_now"] == 2 and r["deal_profit"] >= 15
    assert r["deals_per_week"] > 0
    assert r["sold_per_day"] is None  # швидкість продажу — лише після кількох днів


def test_too_few_listings_gives_no_result(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH[:4]
    assert discovery.analyze_candidate("🎮", "X", "Nintendo Switch OLED", 150) is None


def test_run_discovery_respects_interval(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH
    assert discovery.run_discovery() == 1
    assert discovery.run_discovery() == 0            # ще не минуло DISCOVERY_INTERVAL_HOURS
    assert discovery.run_discovery(force=True) == 1  # примусово — можна


def test_tracked_products_are_not_recommended(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    assert [r["name"] for r in discovery.top_recommendations(1)] == ["Nintendo Switch OLED"]
    db.add_watch(1, "Switch", "nintendo switch oled", "", "", 25)
    assert discovery.top_recommendations(1) == []                       # уже відстежуєш
    assert [r["name"] for r in discovery.top_recommendations(2)] == ["Nintendo Switch OLED"]  # інший користувач


def test_disappeared_listings_count_as_sold(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    # наступна перевірка: двох нових оголошень уже немає — їх купили
    fake_ebay.listings = [it for it in SWITCH if it["itemId"] not in ("x10", "x11")]
    discovery.run_discovery(force=True)
    gone, _ = db.discovery_gone_stats("Nintendo Switch OLED")
    assert gone == 2


def test_score_prefers_more_profit_and_faster_sales():
    base = {"deals_per_week": 4, "deal_profit": 30, "sold_per_day": None}
    assert discovery.score(dict(base, deal_profit=60)) > discovery.score(base)
    assert discovery.score(dict(base, sold_per_day=5)) > discovery.score(dict(base, sold_per_day=0.2))
    assert discovery.score(dict(base, deals_per_week=0)) == 0


# ---------- екран у боті ----------

def _screen(monkeypatch):
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r] if reply_markup else []))

    patch_ui(monkeypatch, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    patch_ui(monkeypatch, "is_owner", lambda uid: True)
    return shown


def _update(data):
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    return upd


def test_discover_screen_and_add(fake_ebay, one_candidate, monkeypatch):
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    shown = _screen(monkeypatch)
    ctx = MagicMock()
    ctx.user_data = {}
    asyncio.run(handlers.discover_callback(_update("menu:discover"), ctx))
    text, buttons = shown[-1]
    assert "Що варто перепродавати" in text and "Nintendo Switch OLED" in text
    assert "➕ Nintendo Switch OLED" in buttons and "🔄 Оновити аналіз" in buttons

    asyncio.run(handlers.discover_add_callback(_update("dadd:0"), ctx))
    watch = db.list_watches(chat_id=1)[0]
    assert watch["query"] == "Nintendo Switch OLED" and watch["min_price"] == 150
    assert "Nintendo Switch OLED" in shown[-1][0]


def test_discover_screen_without_data(monkeypatch):
    shown = _screen(monkeypatch)
    ctx = MagicMock()
    ctx.user_data = {}
    asyncio.run(handlers.discover_callback(_update("menu:discover"), ctx))
    assert "Поки нічого показати" in shown[-1][0]


# ---------- продажі в «💡 Що перепродавати» ----------

def seed_discovery_sales(candidate, prices, spec="64GB", confirmed=0):
    now = int(time.time())
    with db.get_conn() as conn:
        for i, p in enumerate(prices):
            conn.execute(
                """INSERT INTO discovery_obs (candidate, item_id, price, spec_group, cond_group, created_at,
                                              first_seen, last_seen, status, gone_at, sold_check)
                   VALUES (?, ?, ?, ?, 'used', ?, ?, ?, 'gone', ?, ?)""",
                (candidate, f"sold{i}", p, spec, now - 3 * 86400, now - 3 * 86400, now - 86400, now - 86400,
                 "sold" if i < confirmed else None),
            )


def test_sold_items_drive_sale_price(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH
    before = discovery.analyze_candidate("🎮", "Nintendo Switch OLED", "Nintendo Switch OLED", 150)
    assert before["sale_source"] == "listings" and before["sold_week"] == 0

    seed_discovery_sales("Nintendo Switch OLED", [240, 245, 250, 255, 260], confirmed=3)
    r = discovery.analyze_candidate("🎮", "Nintendo Switch OLED", "Nintendo Switch OLED", 150)
    assert r["sale_source"] == "sold" and r["sold_week"] == 5 and r["sold_confirmed"] == 3
    assert r["sold_median"] == 250 and r["sold_days"] == 2.0
    assert r["buy_limit"] < before["buy_limit"]  # продають дешевше, ніж просять в оголошеннях


def test_unsold_listing_not_counted(fake_ebay, one_candidate):
    seed_discovery_sales("Nintendo Switch OLED", [240, 245])
    db.apply_discovery_check("Nintendo Switch OLED", "sold0", "unsold")  # pending не було — нічого не змінить
    with db.get_conn() as conn:
        conn.execute("UPDATE discovery_obs SET sold_check = 'pending' WHERE item_id = 'sold0'")
    db.apply_discovery_check("Nintendo Switch OLED", "sold0", "unsold")
    assert [r["item_id"] for r in db.get_discovery_sold("Nintendo Switch OLED")] == ["sold1"]


def test_discovery_listings_are_verified(fake_ebay, one_candidate, monkeypatch):
    import trading_api
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    fake_ebay.listings = [it for it in SWITCH if it["itemId"] not in ("x10", "x11")]
    discovery.run_discovery(force=True)
    assert len(db.get_pending_discovery_checks(10)) == 2
    monkeypatch.setattr(trading_api, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "get_item_status",
                        lambda item_id: {"result": "sold" if item_id == "x10" else "unsold"})
    assert trading_api.verify_disappeared() == 2
    sold = db.get_discovery_sold("Nintendo Switch OLED")
    assert [(r["item_id"], r["sold_check"]) for r in sold] == [("x10", "sold")]


def test_recommendation_text_shows_sales():
    r = {"emoji": "🎮", "name": "Switch", "spec": "64GB", "median": 280, "buy_limit": 190,
         "deals_per_week": 3, "deals_now": 1, "deal_profit": 40, "sold_per_day": 1.5,
         "sold_week": 11, "sold_confirmed": 8, "sold_median": 255, "sold_days": 2.4, "sale_source": "sold"}
    text = handlers._recommendation_text(1, r)
    assert "🛒 продано за тиждень: 11 (✅8) по ~255€, продаються за ~2 дні" in text
    assert "(за продажами)" in text
    r.update(sold_week=0, sold_per_day=None, sale_source="listings")
    assert "продажі ще рахую" in handlers._recommendation_text(1, r)


@pytest.mark.parametrize("title,query,ok", [
    ("Samsung Galaxy S24 Ultra 256GB", "Samsung Galaxy S24", False),
    ("Samsung Galaxy S24 128GB super Zustand", "Samsung Galaxy S24", True),
    ("MSI GeForce RTX 4070 Super 12GB", "RTX 4070", False),
    ("MSI RTX 4070 SUPER 12GB", "RTX 4070 Super", True),
    ("Gigabyte RTX 4070 Ti 12GB", "RTX 4070", False),
    ("Apple iPhone 15 Pro 256GB", "iPhone 15", False),
    ("Hülle für iPhone 15", "iPhone 15", False),
    ("Apple iPhone 15 128GB Schwarz", "iPhone 15", True),
])
def test_candidate_takes_only_its_own_model(title, query, ok):
    assert discovery._same_model(title, query) is ok


def test_candidate_list_is_big_and_unique():
    names = [name for _, name, _, _ in discovery.CANDIDATES]
    assert len(names) >= 80 and len(names) == len(set(names))


def test_hide_candidate_and_restore(fake_ebay, one_candidate, monkeypatch):
    import config
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    shown = _screen(monkeypatch)
    ctx = MagicMock()
    ctx.user_data = {}
    asyncio.run(handlers.discover_callback(_update("menu:discover"), ctx))
    assert "🙈" in shown[-1][1]
    asyncio.run(handlers.discover_hide_callback(_update("dhide:0"), ctx))
    assert db.get_hidden_candidates(1) == ["Nintendo Switch OLED"]
    assert "➕ Nintendo Switch OLED" not in shown[-1][1] and "🙈 Приховані (1)" in shown[-1][1]
    assert discovery.top_recommendations(1) == []

    monkeypatch.setattr(config, "OWNER_TELEGRAM_ID", 1, raising=False)   # власник приховав — не аналізуємо
    assert discovery.run_discovery(force=True) == 0

    asyncio.run(handlers.discover_hidden_callback(_update("dhidden"), ctx))
    assert "↩️ Nintendo Switch OLED" in shown[-1][1]
    asyncio.run(handlers.discover_hidden_callback(_update("dunhide:0"), ctx))
    assert db.get_hidden_candidates(1) == [] and "повернуто" in shown[-1][0]


def test_sold_only_hides_listing_priced_candidates(fake_ebay, one_candidate):
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    assert discovery.top_recommendations(1)                       # ціна за оголошеннями — видно
    db.set_sold_only(1, True)
    assert discovery.top_recommendations(1) == []                 # «лише за продажами» — ще ні


def test_variant_terms_split_models():
    assert not discovery._same_model("Apple iPhone 15 Pro 128GB", "iPhone 15")
    assert not discovery._same_model("Google Pixel 8 Pro 256GB", "Pixel 8")
    assert discovery._same_model("Apple iPhone 15 128GB Schwarz", "iPhone 15")
    assert not discovery._same_model("Steam Deck OLED 512GB", "Steam Deck 512GB")
