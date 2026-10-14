"""«🔨 Аукціони»: окремий пошук, порівняння ставки з «купувати до», два сповіщення, вкл/викл."""
import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import auctions
import db
import ebay_api
import market
from conftest import listing

HOUR = 3600


def _end(hours):
    return datetime.fromtimestamp(time.time() + hours * HOUR, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _auction(item_id, bid, hours=8, ship=5, **extra):
    return listing(item_id, f"iPhone 15 Pro 256GB {item_id}", bid, buyingOptions=["AUCTION"],
                   currentBidPrice={"value": str(bid), "currency": "EUR"}, bidCount=3, itemEndDate=_end(hours),
                   shippingOptions=[{"shippingCost": {"value": str(ship), "currency": "EUR"}}], **extra)


def _setup(sale=600):
    wid = db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "", "", 15)
    db.upsert_market_stats(wid, "used", "256GB", sale + 30, 30, sale_price=sale, sale_source="за проданими")
    return db.get_watch(wid, 1)


def _search_params(fake_ebay, monkeypatch):
    seen = []
    real = ebay_api._request_with_retries

    def spy(method, url, **kwargs):
        if url == ebay_api.SEARCH_URL:
            seen.append(kwargs.get("params") or {})
        return real(method, url, **kwargs)
    monkeypatch.setattr(ebay_api, "_request_with_retries", spy)
    return seen


def test_auction_search_filters_and_price(fake_ebay, monkeypatch):
    params = _search_params(fake_ebay, monkeypatch)
    fake_ebay.listings = [_auction("a", 300), listing("f", "iPhone 15 Pro 256GB f", 400)]
    now = time.time()
    found = ebay_api.search_active_items("iPhone 15 Pro", auctions=True, end_between=(now + 6 * HOUR, now + 12 * HOUR),
                                         sort="endingSoonest")
    assert [it["item_id"] for it in found] == ["a"]                 # «купити зараз» — не аукціон
    it = found[0]
    assert it["total_price"] == 305 and it["bid_count"] == 3 and it["bid_is_price"] is False
    assert it["has_best_offer"] is False and it["end_at"]
    flt = params[-1]["filter"]
    assert "buyingOptions:{AUCTION}" in flt and "itemEndDate:[" in flt and params[-1]["sort"] == "endingSoonest"

    ebay_api.search_active_items("iPhone 15 Pro")                     # звичайний пошук — як раніше
    assert "buyingOptions:{FIXED_PRICE}" in params[-1]["filter"] and "itemEndDate" not in params[-1]["filter"]


def test_scan_alerts_only_profitable_once(fake_ebay):
    w = _setup(sale=600)
    limit = market.max_buy_price(600, db.get_min_profit(1))
    fake_ebay.listings = [_auction("cheap", 200), _auction("pricey", limit), _auction("late", 200, hours=20)]
    alerts = auctions.scan_watch_auctions(w)
    assert [a["item_id"] for a, _ in alerts] == ["cheap"]
    text = alerts[0][1]
    assert "Ставка зараз: <b>200€</b>" in text and f"ставити не більше {limit - 5:.0f}€" in text
    assert "Нагадаю за 45 хв" in text
    assert auctions.scan_watch_auctions(w) == []                      # вдруге про той самий — ні


def test_run_scan_respects_toggle_and_interval(fake_ebay):
    _setup()
    fake_ebay.listings = [_auction("a", 200)]
    assert auctions.run_auction_scan() == [] and not fake_ebay.calls   # вимкнено — жодного запиту
    db.set_auctions_enabled(1, True)
    assert db.auction_chats() == {1}
    assert len(auctions.run_auction_scan()) == 1
    calls = len(fake_ebay.calls)
    fake_ebay.listings = [_auction("b", 200)]
    assert auctions.run_auction_scan() == [] and len(fake_ebay.calls) == calls   # раз на 3 год


def _alert(**extra):
    a = {"chat_id": 1, "item_id": "x", "end_at": time.time() + 30 * 60, "buy_limit": 400.0, "shipping": 5.0,
         "message_id": 10, "text": "🔨 перше", "url": "https://www.ebay.de/itm/x"}
    a.update(extra)
    return a


def test_final_decision():
    action, text = auctions.final_decision(_alert(), "active", 350, 7)
    assert action == "send" and "Ще вигідно" in text and "395€" in text and "ставок: 7" in text
    action, text = auctions.final_decision(_alert(), "active", 399, 7)    # 399 + 5 доставка > 400
    assert action == "note" and "дорожче" in text
    assert auctions.final_decision(_alert(), "gone", None, None)[0] == "note"
    action, text = auctions.final_decision(_alert(), "unknown", None, None)
    assert action == "send" and "Не вдалося перевірити" in text


def _app():
    app = MagicMock()
    app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=55))
    app.bot.edit_message_text = AsyncMock()
    return app


def test_final_reminder_sent_once(monkeypatch):
    import panel
    monkeypatch.setattr(panel, "repost_panel", AsyncMock())
    db.set_auctions_enabled(1, True)
    db.add_auction_alert(1, "x", 7, time.time() + 30 * 60, 400, 5, 200, "🔨 перше", "https://www.ebay.de/itm/x")
    db.add_auction_alert(1, "y", 7, time.time() + 5 * HOUR, 400, 5, 200, "🔨 інше", None)   # ще рано
    db.set_auction_message(1, "x", 10)
    monkeypatch.setattr(auctions, "current_bid", lambda item_id: ("active", 250.0, 4))
    app = _app()
    asyncio.run(auctions.check_finals(app))
    assert app.bot.send_message.await_count == 1
    kwargs = app.bot.send_message.await_args.kwargs
    assert kwargs["reply_to_message_id"] == 10 and "250€" in kwargs["text"]
    asyncio.run(auctions.check_finals(app))
    assert app.bot.send_message.await_count == 1                      # друге — лише раз


def test_final_too_expensive_edits_silently(monkeypatch):
    db.set_auctions_enabled(1, True)
    db.add_auction_alert(1, "x", 7, time.time() + 30 * 60, 400, 5, 200, "🔨 перше", None)
    db.set_auction_message(1, "x", 10)
    monkeypatch.setattr(auctions, "current_bid", lambda item_id: ("active", 500.0, 9))
    app = _app()
    asyncio.run(auctions.check_finals(app))
    assert app.bot.send_message.await_count == 0
    assert "дорожче" in app.bot.edit_message_text.await_args.args[0]


def test_disabled_chat_gets_no_final(monkeypatch):
    db.add_auction_alert(1, "x", 7, time.time() + 30 * 60, 400, 5, 200, "🔨", None)
    monkeypatch.setattr(auctions, "current_bid", lambda item_id: (_ for _ in ()).throw(AssertionError("запит")))
    app = _app()
    asyncio.run(auctions.check_finals(app))
    assert app.bot.send_message.await_count == 0 and db.due_auction_finals(45) == []


def test_current_bid_parsing(monkeypatch):
    monkeypatch.setattr(auctions, "browse_budget_left", lambda: 5000)
    monkeypatch.setattr(auctions, "fetch_item_by_legacy_id", lambda legacy: {
        "currentBidPrice": {"value": "123.50", "currency": "EUR"}, "bidCount": 6, "itemEndDate": _end(0.5)})
    assert auctions.current_bid("v1|123|0") == ("active", 123.5, 6)
    monkeypatch.setattr(auctions, "fetch_item_by_legacy_id", lambda legacy: None)
    assert auctions.current_bid("v1|123|0")[0] == "gone"
    monkeypatch.setattr(auctions, "browse_budget_left", lambda: 0)
    assert auctions.current_bid("v1|123|0")[0] == "unknown"


def test_cleanup_old_alerts():
    db.add_auction_alert(1, "old", 7, time.time() - 5 * 86400, 400, 5, 200, "", None)
    db.add_auction_alert(1, "new", 7, time.time() + HOUR, 400, 5, 200, "", None)
    assert db.cleanup_auction_alerts() == 1
    assert db.get_alerted_auctions(1, ["old", "new"]) == {"new"}


def test_settings_toggle(monkeypatch):
    import account
    from test_deal_reprice import _record
    from test_sales import _screen
    _, shown, press = _screen(monkeypatch)
    monkeypatch.setattr(account, "show_panel", lambda u, c, text, reply_markup=None, parse_mode=None:
                        _record(shown, text, reply_markup))
    upd, ctx = press("auctions")
    asyncio.run(account.auctions_callback(upd, ctx))
    assert db.get_auctions_enabled(1) is True and "увімкнено" in shown[-1][0]
    assert "🔨 Аукціони: увімкнено ✅" in shown[-1][1]
    asyncio.run(account.auctions_callback(upd, ctx))
    assert db.get_auctions_enabled(1) is False and "🔨 Аукціони: вимкнено" in shown[-1][1]
