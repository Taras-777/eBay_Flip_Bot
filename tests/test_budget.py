"""Ліміт Browse API: жорстка межа, точний залишок, рівномірний розподіл на добу."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import db
import ebay_api
import panel
import scheduler
import settings


def _ebay_data(limit=5000, remaining=1000, reset_in=6 * 3600):
    ebay_api._rate_limit_cache.update(
        data={"limit": limit, "remaining": remaining, "count": limit - remaining,
              "reset": int(time.time() + reset_in)},
        fetched_at=time.time(), own_at_fetch=db.get_api_calls_today("browse"))


def test_budget_counts_calls_made_after_ebay_data():
    _ebay_data(remaining=1000)
    assert ebay_api.browse_budget_left() == 1000 - 500          # 500 — запас між 5000 і бюджетом 4500
    for _ in range(30):
        db.record_api_call("browse")
    assert ebay_api.browse_budget_left() == 470                 # дані eBay старі, але бот рахує свої запити


def test_hard_stop_blocks_browse_requests(monkeypatch):
    sent = []
    monkeypatch.setattr(ebay_api, "_http_session",
                        lambda: SimpleNamespace(request=lambda *a, **k: sent.append(1)))
    _ebay_data(remaining=20)
    with pytest.raises(ebay_api.BudgetExhausted):
        ebay_api._request_with_retries("GET", ebay_api.SEARCH_URL)
    assert sent == []                                           # запит до eBay не пішов


def test_out_of_budget_skips_watches_without_error_notices(monkeypatch):
    from test_scheduler import make_app
    db.add_watch(1, "PS5", "PS5", "", "", 15)
    called, notices = [], []

    async def fake_check(app, w):
        called.append(w["id"])
        raise ebay_api.BudgetExhausted("ліміт")

    async def fake_notice(app, w, e):
        notices.append(e)

    monkeypatch.setattr(scheduler, "check_one_watch", fake_check)
    monkeypatch.setattr(scheduler, "_notify_median_error", fake_notice)
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    assert called and notices == []                    # ліміт — не збій: користувача не сповіщаємо
    monkeypatch.setattr(scheduler, "browse_budget_left", lambda: 0)
    called.clear()
    asyncio.run(scheduler.check_all_watches(app))
    assert called == []                                # ліміт уже вичерпано — товари не чіпаємо


def _pace(app, used, minutes_ago, left, monkeypatch, reset_hours=10):
    monkeypatch.setattr(scheduler, "seconds_until_reset", lambda: reset_hours * 3600)
    monkeypatch.setattr(scheduler, "get_api_calls_today", lambda api="browse": used)
    return scheduler.pace_interval(app, left, now=1_000_000 + minutes_ago * 60)


def test_pacing_slows_down_and_speeds_up(monkeypatch):
    app = SimpleNamespace(bot_data={})
    assert _pace(app, 0, 0, 2000, monkeypatch) == settings.CHECK_INTERVAL_MINUTES
    # за 30 хв — 300 запитів (600/год), а до скидання можна 2000/10 = 200/год → рідше
    assert _pace(app, 300, 30, 2000, monkeypatch) == settings.CHECK_INTERVAL_MINUTES * 1.5
    assert db.get_meta("check_interval") == "8"
    assert "раз на 8 хв" in panel.pace_line()
    # запас великий (4000 на 10 год = 400/год, а витрачаємо мало) → знову частіше
    app.bot_data["pace"]["samples"] = [(1_000_000 + 60 * 60, 300)]
    assert _pace(app, 320, 90, 4000, monkeypatch) == settings.CHECK_INTERVAL_MINUTES
    assert panel.pace_line() == ""


def test_pacing_never_exceeds_max(monkeypatch):
    app = SimpleNamespace(bot_data={"pace": {"interval": 50.0, "samples": [(1_000_000, 0)]}})
    assert _pace(app, 1000, 30, 100, monkeypatch) == settings.MAX_CHECK_INTERVAL_MINUTES


def test_trading_count_resets_with_ebay_not_utc_day():
    for _ in range(379):
        db.record_api_call("trading")                  # власний лічильник: доба за UTC, ще «вчорашні»
    ebay_api._trading_limit_cache.update(count=3, fetched_at=time.time())   # eBay щойно скинув ліміт
    assert ebay_api.trading_calls_today() == (3, True)
    ebay_api._trading_limit_cache.update(fetched_at=0)                      # даних eBay немає — свій лічильник
    assert ebay_api.trading_calls_today() == (379, False)


def test_429_pauses_browse_without_error(monkeypatch):
    responses = []

    def request(*a, **k):
        responses.append(1)
        return SimpleNamespace(status_code=429, headers={"Retry-After": "0"})

    monkeypatch.setattr(ebay_api, "_http_session", lambda: SimpleNamespace(request=request))
    monkeypatch.setattr(ebay_api.time, "sleep", lambda s: None)
    with pytest.raises(ebay_api.RateLimited):
        ebay_api._request_with_retries("GET", ebay_api.SEARCH_URL)
    assert len(responses) == ebay_api.NETWORK_MAX_ATTEMPTS
    with pytest.raises(ebay_api.BudgetExhausted):                  # під час паузи — без запитів до eBay
        ebay_api._request_with_retries("GET", ebay_api.SEARCH_URL)
    assert len(responses) == ebay_api.NETWORK_MAX_ATTEMPTS
    ebay_api._rate_pause["until"] = 0


def test_sold_count_matches_check_counter_window():
    import time as _time
    from datetime import datetime
    from settings import LOCAL_TZ
    now = datetime(2026, 10, 3, 9, 58, tzinfo=LOCAL_TZ)
    assert datetime.fromtimestamp(panel.last_limit_reset(now), LOCAL_TZ).hour == 9
    early = datetime(2026, 10, 3, 3, 0, tzinfo=LOCAL_TZ)
    assert datetime.fromtimestamp(panel.last_limit_reset(early), LOCAL_TZ).day == 2   # ще вчорашня доба eBay
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    db.update_listing_observations(wid, [{"item_id": i, "cond_group": "used", "spec_group": "1TB", "total_price": 300}
                                         for i in ("night", "morning")])
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET sold_check = 'sold', checked_at = ? WHERE item_id = 'night'",
                     (int(_time.time()) - 10 * 3600,))
        conn.execute("UPDATE listing_obs SET sold_check = 'sold', checked_at = ? WHERE item_id = 'morning'",
                     (int(_time.time()),))
    assert db.sold_confirmed_today(since=int(_time.time()) - 3600) == 1   # нічні — до скидання ліміту
