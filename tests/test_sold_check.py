"""Перевірка «справді продано?» через Trading API і вхід в акаунт eBay."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import account
import db
import ebay_user
import settings
import trading_api
from conftest import FakeResponse


def xml_item(status="Completed", sold="1"):
    sold_tag = f"<QuantitySold>{sold}</QuantitySold>" if sold is not None else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>'
            f'<Item><ItemID>298706741553</ItemID><SellingStatus>{sold_tag}'
            f'<ListingStatus>{status}</ListingStatus></SellingStatus></Item></GetItemResponse>')


def xml_error(code):
    return ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
            f'<Errors><ErrorCode>{code}</ErrorCode></Errors></GetItemResponse>')


# ---------- розбір відповіді eBay ----------

@pytest.mark.parametrize("status,sold,result", [
    ("Completed", "1", "sold"),
    ("Ended", "2", "sold"),
    ("Completed", "0", "unsold"),
    ("Active", "0", "active"),
    ("Completed", None, "unknown"),  # eBay не показав кількість — не вгадуємо
])
def test_parse_get_item(status, sold, result):
    assert trading_api.parse_get_item(xml_item(status, sold))["result"] == result


def test_parse_errors():
    assert trading_api.parse_get_item(xml_error("17"))["result"] == "not_found"
    with pytest.raises(ebay_user.UserAuthError):
        trading_api.parse_get_item(xml_error("931"))
    with pytest.raises(RuntimeError):
        trading_api.parse_get_item(xml_error("10007"))   # тимчасовий збій eBay — повторимо пізніше
    info = trading_api.parse_get_item(
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack><Errors>'
        '<ErrorCode>21920397</ErrorCode><LongMessage>Item is not accessible.</LongMessage>'
        '</Errors></GetItemResponse>')
    assert info == {"result": "unknown", "error": "21920397: Item is not accessible."}


def test_legacy_item_id():
    assert trading_api.legacy_item_id("v1|298706741553|0") == "298706741553"
    assert trading_api.legacy_item_id("298706741553") == "298706741553"


# ---------- черга перевірок у базі ----------

def obs_item(item_id, price, created_ago=3600):
    return {"item_id": item_id, "cond_group": "used", "spec_group": "unspecified",
            "total_price": price, "created_at": int(time.time()) - created_ago}


def make_gone(watch_id, item_id, price):
    """Лот бачили, потім він двічі не з'явився у видачі → зник."""
    db.update_listing_observations(watch_id, [obs_item(item_id, price, 7200), obs_item("keep", 500, 9000)])
    window = int(time.time()) - 9000
    for _ in range(settings.GONE_MISS_THRESHOLD):
        db.update_listing_observations(watch_id, [obs_item("keep", 500, 9000)], window, {"keep"})


def test_disappeared_listing_is_queued_and_verified():
    make_gone(1, "sold-1", 400)
    make_gone(1, "pulled-1", 300)
    pending = {r["item_id"] for r in db.get_pending_sold_checks(10)}
    assert pending == {"sold-1", "pulled-1"}
    # До перевірки обидва рахуються як «зниклі»
    assert sorted(db.get_gone_prices(1, "used")) == [300, 400]

    db.apply_sold_check(1, "sold-1", "sold")
    db.apply_sold_check(1, "pulled-1", "unsold")
    assert db.get_gone_prices(1, "used") == [400]  # знятий без продажу — не ціна продажу
    assert db.get_pending_sold_checks(10) == []
    assert db.sold_check_stats() == {"sold": 1, "unsold": 1}


def test_old_listing_can_be_confirmed_as_sold():
    """Лот, старший за GONE_MAX_LISTING_DAYS, раніше ігнорувався; тепер його теж перевіряємо."""
    old = settings.GONE_MAX_LISTING_DAYS * 86400 + 3600
    db.update_listing_observations(1, [obs_item("old", 350, old), obs_item("keep", 500, old + 100)])
    for _ in range(settings.GONE_MISS_THRESHOLD):
        db.update_listing_observations(1, [obs_item("keep", 500, old + 100)], int(time.time()) - old - 100, {"keep"})
    assert db.get_gone_prices(1, "used") == []
    db.apply_sold_check(1, "old", "sold")
    assert db.get_gone_prices(1, "used") == [350]


def test_reappeared_listing_leaves_queue():
    make_gone(1, "back", 400)
    db.update_listing_observations(1, [obs_item("back", 400, 7200)])
    assert db.get_pending_sold_checks(10) == []


def test_unknown_answer_keeps_old_estimate():
    make_gone(1, "x", 420)
    db.apply_sold_check(1, "x", "unknown")
    assert db.get_gone_prices(1, "used") == [420]


# ---------- фонова перевірка ----------

def test_verify_disappeared(monkeypatch):
    make_gone(1, "v1|111|0", 400)
    make_gone(1, "v1|222|0", 300)
    answers = {"111": "sold", "222": "unsold"}
    monkeypatch.setattr(trading_api, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "get_item_status",
                        lambda item_id: {"result": answers[trading_api.legacy_item_id(item_id)]})
    assert trading_api.verify_disappeared() == 2
    assert db.get_gone_prices(1, "used") == [400]


def test_verify_does_nothing_without_account(monkeypatch):
    make_gone(1, "a", 400)
    called = []
    monkeypatch.setattr(trading_api, "get_item_status", lambda item_id: called.append(item_id))
    assert trading_api.verify_disappeared() == 0 and called == []


def test_verify_stops_on_auth_error(monkeypatch):
    make_gone(1, "a", 400)
    make_gone(1, "b", 300)
    calls = []

    def fail(item_id):
        calls.append(item_id)
        raise ebay_user.UserAuthError("expired")

    monkeypatch.setattr(trading_api, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "get_item_status", fail)
    assert trading_api.verify_disappeared() == 0 and len(calls) == 1
    assert len(db.get_pending_sold_checks(10)) == 2  # лишаються в черзі


def test_verify_respects_daily_budget(monkeypatch):
    make_gone(1, "a", 400)
    monkeypatch.setattr(trading_api, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "trading_calls_today", lambda: (settings.TRADING_DAILY_BUDGET, True))
    monkeypatch.setattr(trading_api, "get_item_status", lambda item_id: pytest.fail("ліміт вичерпано"))
    assert trading_api.verify_disappeared() == 0


def test_get_item_status_request(monkeypatch):
    sent = {}

    class Resp:
        status_code, text = 200, xml_item("Completed", "1")

    def fake_request(method, url, **kwargs):
        sent.update(url=url, **kwargs)
        return Resp()

    monkeypatch.setattr(trading_api, "_request_with_retries", fake_request)
    monkeypatch.setattr(trading_api, "get_user_access_token", lambda: "USER-TOKEN")
    assert trading_api.get_item_status("v1|298706741553|0")["result"] == "sold"
    assert sent["url"] == trading_api.TRADING_URL
    assert sent["headers"]["X-EBAY-API-IAF-TOKEN"] == "USER-TOKEN"
    assert sent["headers"]["X-EBAY-API-SITEID"] == "77"
    assert b"<ItemID>298706741553</ItemID>" in sent["data"]
    assert db.get_api_calls_today("trading") == 1


# ---------- вхід в акаунт eBay ----------

def test_extract_code():
    url = ("https://auth2.ebay.com/oauth2/ThirdPartyAuthSucessFailure?isAuthSuccessful=true"
           "&code=v%5E1.1%23i%5E1%23abc&expires_in=299")
    assert ebay_user.extract_code(url) == "v^1.1#i^1#abc"
    assert ebay_user.extract_code("v^1.1#i^1#abc") == "v^1.1#i^1#abc"
    assert ebay_user.extract_code("привіт") is None
    assert ebay_user.extract_code("https://www.ebay.de/?isAuthSuccessful=false") is None


def test_exchange_and_refresh(monkeypatch):
    requests_made = []

    def fake_request(method, url, **kwargs):
        requests_made.append(kwargs["data"])
        if kwargs["data"]["grant_type"] == "authorization_code":
            return FakeResponse({"access_token": "A1", "expires_in": 7200,
                                 "refresh_token": "R1", "refresh_token_expires_in": 47304000})
        return FakeResponse({"access_token": "A2", "expires_in": 7200})

    monkeypatch.setattr(ebay_user, "_request_with_retries", fake_request)
    assert not ebay_user.is_connected()
    ebay_user.exchange_code("CODE")
    assert ebay_user.is_connected()
    assert ebay_user.get_user_access_token() == "A1"  # з кешу, без запиту
    ebay_user._token_cache.update(token=None, expires_at=0)
    assert ebay_user.get_user_access_token() == "A2"
    assert requests_made[-1]["refresh_token"] == "R1"
    ebay_user.disconnect()
    assert not ebay_user.is_connected()


def test_expired_login_disconnects(monkeypatch):
    db.set_meta("ebay_user_refresh_token", "R-old")
    ebay_user._token_cache.update(token=None, expires_at=0)
    monkeypatch.setattr(ebay_user, "_request_with_retries",
                        lambda *a, **k: FakeResponse({"error": "invalid_grant"}, status=400))
    with pytest.raises(ebay_user.UserAuthError):
        ebay_user.get_user_access_token()
    assert not ebay_user.is_connected()


def test_bad_code_is_reported(monkeypatch):
    monkeypatch.setattr(ebay_user, "_request_with_retries",
                        lambda *a, **k: FakeResponse({"error": "invalid_grant",
                                                      "error_description": "code expired"}, status=400))
    with pytest.raises(ebay_user.UserAuthError, match="code expired"):
        ebay_user.exchange_code("OLD")
    assert not ebay_user.is_connected()


# ---------- екран у боті ----------

@pytest.fixture
def screen(monkeypatch):
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        rows = [[b.text for b in r] for r in reply_markup.inline_keyboard] if reply_markup else []
        shown.append((text, rows))

    monkeypatch.setattr(account, "show_panel", fake_show)
    monkeypatch.setattr(account, "is_owner", lambda uid: True)
    return shown


def make_update(data="menu:ebay_account"):
    upd = MagicMock()
    upd.effective_chat.id = upd.effective_user.id = 1
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    return upd


def test_account_screen_without_runame(monkeypatch, screen):
    monkeypatch.setattr(account, "is_configured", lambda: False)
    state = asyncio.run(account.ebay_account_start(make_update(), MagicMock()))
    assert state == account.ConversationHandler.END
    assert "EBAY_RUNAME" in screen[-1][0]


def test_account_login_flow(monkeypatch, screen):
    monkeypatch.setattr(account, "is_configured", lambda: True)
    monkeypatch.setattr(ebay_user, "EBAY_RUNAME", "Taras-RuName")
    state = asyncio.run(account.ebay_account_start(make_update(), MagicMock()))
    assert state == account.EBAY_CODE and ["🔗 Увійти в eBay"] in screen[-1][1]

    got = []
    monkeypatch.setattr(account, "exchange_code", lambda code: got.append(code))
    upd = make_update()
    upd.message.text = "https://auth2.ebay.com/x?isAuthSuccessful=true&code=v%5E1.1%23abc&expires_in=299"
    state = asyncio.run(account.ebay_account_code(upd, MagicMock()))
    assert state == account.ConversationHandler.END
    assert got == ["v^1.1#abc"] and "підключено" in screen[-1][0]


def test_account_connected_screen(monkeypatch, screen):
    monkeypatch.setattr(account, "is_configured", lambda: True)
    db.set_meta("ebay_user_refresh_token", "R")
    make_gone(1, "a", 400)
    db.apply_sold_check(1, "a", "sold")
    asyncio.run(account.ebay_account_start(make_update(), MagicMock()))
    text, rows = screen[-1]
    assert "✅ підключено" in text and "продано: 1" in text
    assert ["🔌 Відключити"] in rows


# ---------- «📈 Продажі» у картці товару ----------

def sales_watch():
    wid = db.add_watch(1, "Iphone 16 pro", "Iphone 16 pro", "", "3000", 25)
    return db.get_watch(wid, 1)


def add_sale(watch_id, item_id, price, spec, confirmed=False, days_ago=1):
    db.update_listing_observations(watch_id, [{
        "item_id": item_id, "cond_group": "used", "spec_group": spec, "total_price": price,
        "created_at": int(time.time()) - 3600, "title": f"iPhone 16 Pro {spec}", "url": f"https://www.ebay.de/itm/{item_id}",
    }])
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET status='gone', gone_at=?, sold_check=? WHERE item_id=?",
                     (int(time.time()) - days_ago * 86400, "sold" if confirmed else None, item_id))


def test_sales_screen_groups_by_configuration():
    w = sales_watch()
    add_sale(w["id"], "a", 600, "256GB", confirmed=True)
    add_sale(w["id"], "b", 640, "256GB", days_ago=3)
    add_sale(w["id"], "c", 520, "128GB", days_ago=10)
    import handlers
    text = handlers._sales_text(w, db.get_sold_listings(w["id"]))
    assert "Продано: <b>3</b>" in text and "підтверджено eBay: 1" in text
    assert "За останні 7 днів: <b>2</b>" in text
    assert "<b>256GB</b> — 2 продажі (✅1)" in text and "620€" in text  # медіана 600 і 640
    assert "<b>128GB</b> — 1 продаж" in text
    assert text.index("256GB</b>") < text.index("128GB</b>")  # спершу найпопулярніша
    assert 'href="https://www.ebay.de/itm/a"' in text


def test_sales_screen_empty_and_hint():
    w = sales_watch()
    import handlers
    text = handlers._sales_text(w, [], show_account_hint=True)
    assert "Продажів ще не помічено" in text and "Акаунт eBay" in text


def test_unsold_listing_not_in_sales():
    w = sales_watch()
    add_sale(w["id"], "a", 600, "256GB")
    make_gone(w["id"], "pulled", 300)
    db.apply_sold_check(w["id"], "pulled", "unsold")
    assert [r["item_id"] for r in db.get_sold_listings(w["id"])] == ["a"]


def test_sales_button_in_watch_card(monkeypatch):
    import handlers
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r]))

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    w = sales_watch()
    asyncio.run(handlers._show_watch_details(MagicMock(), MagicMock(), w))
    assert "📈 Продажі" in shown[-1][1]

    add_sale(w["id"], "a", 600, "256GB")
    upd = make_update(f"sales:{w['id']}")
    import access
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    monkeypatch.setattr(handlers, "is_connected", lambda: True)
    asyncio.run(handlers.sales_callback(upd, MagicMock()))
    assert "Продано: <b>1</b>" in shown[-1][0] and "◀️ До товару" in shown[-1][1]


def test_main_menu_shows_sold_checks(monkeypatch):
    import ebay_api
    import panel
    monkeypatch.setattr(panel, "is_owner", lambda uid: True)
    ebay_api._rate_limit_cache.update(
        data={"limit": 5000, "remaining": 3480, "count": 1520, "reset": time.time() + 3600},
        fetched_at=time.time())
    assert "Перевірки продажів" not in panel.main_menu_text(1)  # акаунт не підключено
    db.set_meta("ebay_user_refresh_token", "R")
    db.record_api_call("trading")
    lines = panel.main_menu_text(1).splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("📡"))
    assert lines[i] == "📡 Запити до eBay сьогодні: <b>1520</b> / 5000"
    assert lines[i + 1] == "🧾 Перевірки продажів сьогодні: <b>1</b> / 4000 (✅ продано: <b>0</b>)"
    assert lines[i + 2].startswith("🔄 Ліміт скинеться сьогодні о") or lines[i + 2].startswith("🔄 Ліміт скинеться завтра о")


def test_login_script(monkeypatch, capsys):
    import ebay_login
    monkeypatch.setattr(ebay_login, "is_configured", lambda: True)
    monkeypatch.setattr(ebay_user, "EBAY_RUNAME", "RU")
    got = []
    monkeypatch.setattr(ebay_login, "exchange_code", lambda code: got.append(code))
    answers = iter(["не те", "https://auth2.ebay.com/x?isAuthSuccessful=true&code=v%5E1.1%23abc&expires_in=299"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(ebay_login.sys, "argv", ["ebay_login.py"])
    ebay_login.main()
    out = capsys.readouterr().out
    assert got == ["v^1.1#abc"] and "auth.ebay.com/oauth2/authorize" in out and "✅ Акаунт eBay підключено" in out


def test_trading_count_from_ebay(monkeypatch):
    import ebay_api
    payload = {"rateLimits": [{"apiContext": "tradingapi", "resources": [
        {"name": "TradingAPI", "rates": [{"timeWindow": 86400, "limit": 5000, "remaining": 4700, "count": 300}]},
        {"name": "GetItem", "rates": [{"timeWindow": 86400, "limit": 5000, "remaining": 4850, "count": 150}]},
    ]}]}
    monkeypatch.setattr(ebay_api, "_request_with_retries", lambda *a, **k: FakeResponse(payload))
    assert ebay_api.fetch_trading_rate_limit("TOKEN") == 300
    db.record_api_call("trading")                      # на цій копії бота — лише 1
    assert ebay_api.trading_calls_today() == (300, True)  # а eBay бачить запити з ПК і сервера разом


def test_old_gone_listings_are_queued_on_start():
    """Лоти, що зникли до появи перевірки продажів, стають у чергу після перезапуску."""
    with db.get_conn() as conn:
        conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price,
                        first_seen, last_seen, status, gone_at) VALUES (1, 'old', 'used', '500GB', 90, 1, 1, 'gone', ?)""",
                     (int(time.time()) - 86400,))
    db.init_db()
    assert [r["item_id"] for r in db.get_pending_sold_checks(10)] == ["old"]


def test_sales_screen_marks_pending():
    import handlers
    wid = db.add_watch(1, "PS4", "PS4", "", "", 15)
    now = int(time.time())
    with db.get_conn() as conn:
        for item, check in (("a", "sold"), ("b", "pending"), ("c", "unknown")):
            conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price, first_seen,
                            last_seen, status, gone_at, sold_check, title) VALUES (?, ?, 'used', '500GB', 90, 1, 1,
                            'gone', ?, ?, ?)""", (wid, item, now, check, f"PS4 {item}"))
    text = handlers._sales_text(db.get_watch(wid, 1), db.get_sold_listings(wid))
    assert "✅ " in text and "⏳ " in text and "ще в черзі" in text


# ---------- прибрати чужий товар зі статистики продажів ----------

def test_remove_wrong_item_from_sales(monkeypatch):
    import access
    import handlers
    w = sales_watch()
    for i, price in enumerate([600, 610, 620, 630, 640]):
        add_sale(w["id"], f"ok{i}", price, "256GB")
    add_sale(w["id"], "case", 25, "256GB")          # чохол затесався в продажі
    db.upsert_market_stats(w["id"], "used", "256GB", 650, 20, sale_price=300, sale_source="за 6 проданими")
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.callback_data for r in reply_markup.inline_keyboard for b in r]))

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    monkeypatch.setattr(handlers, "is_connected", lambda: True)

    upd = make_update(f"sales:{w['id']}")
    asyncio.run(handlers.sales_callback(upd, MagicMock()))
    assert f"srej:{w['id']}:0:case" in shown[-1][1] and "Продано: <b>6</b>" in shown[-1][0]

    upd = make_update(f"srej:{w['id']}:0:case")
    asyncio.run(handlers.sales_reject_callback(upd, MagicMock()))
    text = shown[-1][0]
    assert "❌ Прибрано" in text and "Продано: <b>5</b>" in text
    assert "case" in db.get_rejected_ids(w["id"])                     # і в пошуку більше не з'явиться
    stat = next(s for s in db.get_market_stats(w["id"]) if s["spec_group"] == "256GB")
    assert stat["sale_price"] == 620 and stat["sale_source"] == "за 5 проданими"  # ціну перераховано одразу


def test_sales_pagination():
    import handlers
    w = sales_watch()
    for i in range(10):
        add_sale(w["id"], f"s{i}", 600 + i, "256GB")
    sold = db.get_sold_listings(w["id"])
    text = handlers._sales_text(w, sold, page=1)
    assert "(стор. 2/2)" in text and "9. " in text and "\n1. " not in text
    kb = handlers._sales_keyboard(w["id"], sold, 1)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert "❌ 9" in labels and "◀️ Новіші" in labels and "❌ 1" not in labels


def test_watch_card_shows_tracking_summary(monkeypatch):
    import handlers
    w = sales_watch()
    add_sale(w["id"], "s1", 600, "256GB", confirmed=True)
    add_sale(w["id"], "s2", 610, "256GB")
    make_gone(w["id"], "p1", 590)                      # чекає перевірки
    make_gone(w["id"], "u1", 580)
    db.apply_sold_check(w["id"], "u1", "unsold")        # знято без продажу
    db.update_listing_observations(w["id"], [obs_item(f"a{i}", 500) for i in range(3)])  # 3 активні
    summary = db.watch_obs_summary(w["id"])
    assert summary["sold"] == 3 and summary["confirmed"] == 1 and summary["pending"] == 1
    assert summary["withdrawn"] == 1 and summary["active"] >= 3
    text = handlers._watch_details_text(db.get_watch(w["id"], 1))
    assert "📋 Відстежую оголошень зараз" in text and "🛒 Продано за 60 днів: <b>3</b>" in text
    assert "⏳ У черзі на перевірку «продано?»: <b>1</b>" in text and "🚫 Зникли без продажу" in text


def test_menu_counts_sold_today():
    import panel
    make_gone(1, "x1", 500)
    make_gone(1, "x2", 510)
    db.apply_sold_check(1, "x1", "sold")
    db.apply_sold_check(1, "x2", "unsold")
    db.record_api_call("trading")
    db.record_api_call("trading")
    assert db.sold_confirmed_today() == 1
    db.set_meta("ebay_user_refresh_token", "R")
    assert panel.sold_checks_line() == "🧾 Перевірки продажів сьогодні: <b>2</b> / 4000 (✅ продано: <b>1</b>)"


def test_pending_not_counted_when_verification_on():
    """Лот з ⏳ (ще не перевірений eBay) не впливає на ціни, доки eBay не підтвердить продаж."""
    make_gone(1, "p", 606)                           # зник, чекає перевірки
    assert db.get_gone_prices(1, "used") == [606]    # без входу в eBay — рахуємо за зникненням
    db.set_meta("ebay_user_refresh_token", "R")      # перевірка увімкнена
    assert db.get_gone_prices(1, "used") == []
    assert db.get_sold_listings(1) == [] and db.watch_obs_summary(1)["sold"] == 0
    assert db.watch_obs_summary(1)["pending"] == 1
    db.apply_sold_check(1, "p", "active")            # eBay: ще продається
    assert db.get_sold_listings(1) == [] and db.watch_obs_summary(1)["pending"] == 0


def test_check_now_button(monkeypatch):
    import access
    import handlers
    import trading_api
    w = sales_watch()
    db.set_meta("ebay_user_refresh_token", "R")
    make_gone(w["id"], "v1|1|0", 536)
    make_gone(w["id"], "v1|2|0", 606)
    make_gone(99, "v1|3|0", 700)                        # чужий товар — не чіпаємо
    shown = []

    async def fake_show(update, context, text, reply_markup=None, parse_mode=None):
        shown.append((text, [b.text for r in reply_markup.inline_keyboard for b in r] if reply_markup else []))

    monkeypatch.setattr(handlers, "show_panel", fake_show)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    monkeypatch.setattr(trading_api, "get_item_status",
                        lambda item_id: {"result": "sold" if item_id == "v1|1|0" else "active"})

    upd = make_update(f"sales:{w['id']}")
    asyncio.run(handlers.sales_callback(upd, MagicMock()))
    assert "⏳ Перевірити зараз (2)" in shown[-1][1]

    upd = make_update(f"schk:{w['id']}:0")
    asyncio.run(handlers.sales_check_now_callback(upd, MagicMock()))
    text, buttons = shown[-1]
    assert "✅ продано — 1" in text and "↩️ ще продається — 1" in text
    assert "Продано: <b>1</b>" in text and not any(b.startswith("⏳ Перевірити") for b in buttons)
    assert [r["item_id"] for r in db.get_pending_sold_checks(10)] == ["v1|3|0"]


def test_sales_screen_shows_tracked_listings():
    import handlers
    w = sales_watch()
    db.update_listing_observations(w["id"], [
        {"item_id": f"a{i}", "cond_group": "used", "spec_group": "256GB" if i < 3 else "128GB",
         "total_price": 500, "created_at": int(time.time())} for i in range(5)])
    add_sale(w["id"], "s1", 600, "256GB")
    active = db.active_listing_counts(w["id"])
    assert active == {("used", "256GB"): 3, ("used", "128GB"): 2}
    text = handlers._sales_text(w, db.get_sold_listings(w["id"]), active=active)
    assert "📋 Зараз у продажу (бот відстежує): <b>5</b> оголошень" in text
    assert "📋 зараз у продажу: 3" in text          # біля 256GB
    empty = handlers._sales_text(w, [], active=active)
    assert "<b>5</b> оголошень" in empty and "Продажів ще не помічено" in empty