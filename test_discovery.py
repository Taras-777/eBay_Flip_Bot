"""«💡 Що перепродавати»: аналіз популярних товарів і рейтинг."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import access
import db
import discovery
import handlers
from conftest import listing


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
    assert discovery.run_discovery() == 0            # ще не минуло 12 годин
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

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    monkeypatch.setattr(handlers, "is_owner", lambda uid: True)
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
    assert "Аналіз ще не готовий" in shown[-1][0]