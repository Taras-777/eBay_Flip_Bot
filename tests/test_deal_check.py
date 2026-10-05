"""Дата виставлення оголошень і перевірка, чи вигідні пропозиції ще продаються."""
import asyncio
import time

import db
import deal_check
import handlers
import screen_common
import settings
import scheduler
from test_sales import _screen
from test_scheduler import consoles, make_app
from test_ui import Screen, make_context, make_update, _state
from conftest import patch_ui

DAY = 86400


def _deals(n=3, listed_ago=2 * DAY):
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15)
    ids = [db.add_deal(wid, f"v1|{i}|0", f"iPhone 16 Pro #{i}", 300 + i, "EUR", 620, 20,
                       f"https://www.ebay.de/itm/{i}", False, listed_at=int(time.time() - listed_ago))
           for i in range(n)]
    return wid, ids


def _age_checks(seconds):
    with db.get_conn() as conn:
        conn.execute("UPDATE deals SET checked_at = checked_at - ?", (seconds,))


# ---------- 📅 коли виставлено ----------

def test_listed_text():
    now = time.time()
    assert screen_common._listed(None) == ""
    assert screen_common._listed(now).startswith("сьогодні о ")
    assert screen_common._listed(now - 3 * DAY).endswith("(3 дні тому)")
    assert screen_common._listed(now - 10 * DAY).endswith("(10 днів тому)")
    from datetime import datetime
    old = now - 800 * DAY
    assert screen_common._listed(old).startswith(datetime.fromtimestamp(old, settings.LOCAL_TZ).strftime("%d.%m.%Y"))


def test_listings_show_listed_date(monkeypatch):
    screen = Screen()
    patch_ui(monkeypatch, "show_panel", screen)
    state = _state(3)
    state["items"][0]["created_at"] = int(time.time() - 3 * DAY)
    asyncio.run(handlers._render_listing_panel(make_update(), make_context(), 7, state))
    assert screen.text.count("📅 виставлено") == 1 and "(3 дні тому)" in screen.text


def test_listings_screen_keeps_listing_date(monkeypatch):
    """Дата має дожити від відповіді eBay до екрана «🔎 Оголошення»."""
    import screen_listings
    listed = int(time.time() - 3 * DAY)
    found = [{"item_id": "a", "title": "Sony PlayStation 5", "total_price": 300, "currency": "EUR",
              "condition": "Gebraucht", "url": "u", "created_at": listed}]
    monkeypatch.setattr(screen_listings, "search_in_categories", lambda *a, **kw: [dict(x) for x in found])
    monkeypatch.setattr(screen_listings, "_annotate_items", lambda items, **kw: items)
    monkeypatch.setattr(screen_listings, "_apply_item_filters", lambda w, items: items)
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "1", "name": "K"}])
    items = screen_listings._fetch_cheapest(db.get_watch(wid, 1), screen_listings._new_fetch_state(), 10)
    assert items[0]["created_at"] == listed


def test_new_deal_remembers_listing_date(fake_ebay):
    fake_ebay.listings = consoles()
    db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    from conftest import listing
    fake_ebay.listings = [listing("cheap", "Sony PlayStation 5 Slim 1TB Konsole", 250,
                                  created_ago_s=2 * DAY)] + consoles()
    asyncio.run(scheduler.check_all_watches(app))
    deal = db.get_inbox_deals(1)[0][0]
    assert abs(deal["listed_at"] - (time.time() - 2 * DAY)) < 60


# ---------- продані/зняті зникають зі списку ----------

def test_background_recheck_removes_gone(monkeypatch):
    _, ids = _deals(3)
    status = {"v1|0|0": True, "v1|1|0": False, "v1|2|0": None}
    asked = []
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (asked.append(item) or status[item], None))

    assert deal_check.recheck_deals() == 0 and asked == []     # щойно знайдені — ще свіжі
    _age_checks(31 * 60)
    assert deal_check.recheck_deals() == 1
    assert db.get_deal(ids[1])["status"] == "gone" and db.get_inbox_deals(1)[1] == 2
    asked.clear()
    assert deal_check.recheck_deals() == 0
    assert asked == ["v1|2|0"]    # «не вдалося дізнатися» — перепитаємо наступного разу


def test_below_min_profit_not_checked(monkeypatch):
    wid = db.add_watch(1, "iPhone", "iPhone", "", "", 15)
    db.add_deal(wid, "v1|x|0", "iPhone", 480, "EUR", 580, 20, "u", False)   # прибуток 6€ — не видно
    _age_checks(DAY)
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (1 / 0, None))
    assert deal_check.recheck_deals() == 0


def test_opening_inbox_rechecks_page(monkeypatch):
    _, ids = _deals(3)
    _age_checks(11 * 60)
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (item != "v1|0|0", None))
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers.deals_callback(upd, ctx))
    text = shown[-1][0]
    assert "Прибрано вже проданих чи знятих: 1" in text and "(2)" in text
    assert "📅 виставлено" in text and "(2 дні тому)" in text
    assert db.get_deal(ids[0])["status"] == "gone"


def test_listing_available_uses_trading_then_browse(monkeypatch):
    monkeypatch.setattr(deal_check, "is_connected", lambda: True)
    monkeypatch.setattr(deal_check, "trading_calls_today", lambda: (0, None))
    monkeypatch.setattr(deal_check, "get_item_status", lambda item: {"result": "sold"})
    assert deal_check.listing_available("v1|5|0") is False
    monkeypatch.setattr(deal_check, "get_item_status", lambda item: {"result": "active"})
    assert deal_check.listing_available("v1|5|0") is True

    monkeypatch.setattr(deal_check, "is_connected", lambda: False)
    monkeypatch.setattr(deal_check, "browse_budget_left", lambda: 4000)
    monkeypatch.setattr(deal_check, "fetch_item_by_legacy_id", lambda legacy: None)
    assert deal_check.listing_available("v1|5|0") is False
    monkeypatch.setattr(deal_check, "browse_budget_left", lambda: 10)   # бюджет — для пошуку
    assert deal_check.listing_available("v1|5|0") is None


# ---------- розбивка запитів Trading API ----------

def test_trading_breakdown(monkeypatch):
    import account
    import trading_api
    for _ in range(3):
        db.record_api_call("trading")
    db.record_api_call("trading:watch")
    db.record_api_call("trading:deals")
    monkeypatch.setattr(deal_check, "trading_calls_today", lambda: (5, False))
    lines = deal_check.trading_breakdown_lines()
    assert "мої товари: 1 · 💡 Що перепродавати: 0 · 🔥 вигідні пропозиції: 1" in lines[0]
    assert lines[1].startswith("   інше: 3")

    # перевірка зниклих рахується окремо: твої товари і «💡 Що перепродавати»
    monkeypatch.setattr(trading_api, "get_item_status", lambda item_id: {"result": "sold"})
    trading_api._run_checks([(db.apply_sold_check, (1, "v1|a|0")),
                             (db.apply_discovery_check, ("PS5", "v1|b|0"))])
    assert db.get_api_calls_today("trading:watch") == 2
    assert db.get_api_calls_today("trading:discovery") == 1
    assert callable(account._connected_text)


# ---------- 👁 скільки людей стежать ----------

def test_watchers_shown_in_deal_card(monkeypatch):
    import trading_api
    xml = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item><ItemID>1</ItemID>'
           '<WatchCount>23</WatchCount><SellingStatus><QuantitySold>0</QuantitySold>'
           '<ListingStatus>Active</ListingStatus></SellingStatus></Item></GetItemResponse>')
    info = trading_api.parse_get_item(xml)
    assert info["result"] == "active" and info["details"]["watch_count"] == 23
    monkeypatch.setattr(deal_check, "is_connected", lambda: True)
    monkeypatch.setattr(deal_check, "trading_calls_today", lambda: (0, None))
    monkeypatch.setattr(deal_check, "get_item_status", lambda item: info)
    assert deal_check.listing_state("v1|0|0") == (True, 23)

    _, ids = _deals(1)
    _age_checks(11 * 60)
    handlers_mod, shown, press = _screen(monkeypatch)
    upd, ctx = press("deals:0")
    asyncio.run(handlers_mod.deals_callback(upd, ctx))
    assert "👁 стежать: 23" in shown[-1][0] and db.get_deal(ids[0])["watch_count"] == 23


def test_shop_listing_sold_quantity_saved(fake_ebay, monkeypatch):
    import ebay_api
    from conftest import FakeResponse
    monkeypatch.setattr(ebay_api, "_request_with_retries", lambda *a, **k: FakeResponse(
        {"localizedAspects": [{"name": "Speicherkapazität", "value": "256 GB"}],
         "estimatedAvailabilities": [{"estimatedSoldQuantity": 14, "estimatedAvailableQuantity": 6}]}))
    assert ebay_api.fetch_item_aspects("v1|shop|0") == {"speicherkapazität": "256 GB", "_beschreibung_geprueft": "2"}
    db.save_cached_spec("v1|shop|0", "256GB", {"speicherkapazität": "256 GB"})   # як після читання характеристик
    with db.get_conn() as conn:
        row = dict(conn.execute("SELECT spec_group, est_sold, est_available FROM item_specs").fetchone())
    assert row == {"spec_group": "256GB", "est_sold": 14, "est_available": 6}


# ---------- 🔨 «аукціон + купити зараз» ----------

def test_auction_with_buy_now_marked(fake_ebay, monkeypatch):
    import ebay_api
    from conftest import listing
    fake_ebay.listings = [
        listing("v1|bin|0", "iPhone 16 Pro 128GB", 810, buyingOptions=["AUCTION", "FIXED_PRICE"],
                currentBidPrice={"value": "556.00", "currency": "EUR"}, bidCount=36),
        listing("v1|bid|0", "iPhone 16 Pro 128GB", 300, buyingOptions=["AUCTION", "FIXED_PRICE"],
                currentBidPrice={"value": "300.00", "currency": "EUR"}, bidCount=2),
        listing("v1|plain|0", "iPhone 16 Pro 128GB", 600)]
    items = {it["item_id"]: it for it in ebay_api.search_active_items("iPhone 16 Pro", limit=10)}
    assert items["v1|bin|0"]["auction"] and items["v1|bin|0"]["current_bid"] == 556 and items["v1|bin|0"]["bid_count"] == 36
    assert not items["v1|bin|0"]["bid_is_price"] and items["v1|bid|0"]["bid_is_price"]
    assert items["v1|plain|0"]["auction"] is False

    import market
    w = {"id": db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15), "query": "iPhone 16 Pro"}
    kept = [it["item_id"] for it in market._apply_item_filters(w, list(items.values()))]
    assert "v1|bid|0" not in kept and "v1|bin|0" in kept          # ціна-ставка ще зросте — не порівнюємо

    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    db.add_deal(wid, "v1|a|0", "PS5 Slim", 300, "EUR", 620, 40, "u", False,
                auction={"current_bid": 210, "bid_count": 5, "end_at": int(time.time() + 5 * 3600)})
    handlers_mod, shown, press = _screen(monkeypatch)
    monkeypatch.setattr(deal_check, "listing_state", lambda item: (True, None))
    upd, ctx = press("deals:0")
    asyncio.run(handlers_mod.deals_callback(upd, ctx))
    assert "🔨 Ще й аукціон: ставка 210€ · 5 ставок · до кінця 5 год (на момент знахідки)" in shown[-1][0]


def test_buy_it_now_price_used_for_buying(fake_ebay, monkeypatch):
    import ebay_api
    import ebay_user
    import market
    import trading_api
    from conftest import listing
    fake_ebay.listings = [listing("v1|bid|0", "iPhone 16 Pro 128GB", 300, buyingOptions=["AUCTION", "FIXED_PRICE"],
                                  currentBidPrice={"value": "300.00", "currency": "EUR"}, bidCount=2)]
    items = ebay_api.search_active_items("iPhone 16 Pro", limit=10)
    w = {"id": db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 15), "query": "iPhone 16 Pro"}
    asked = []
    monkeypatch.setattr(ebay_user, "is_connected", lambda: True)
    monkeypatch.setattr(trading_api, "fetch_buy_it_now", lambda item_id: asked.append(item_id) or 810.0)
    kept = market._apply_item_filters(w, items)
    assert [(it["price"], it["total_price"], it["auction"]) for it in kept] == [(810.0, 810.0, True)]
    market._apply_item_filters(w, ebay_api.search_active_items("iPhone 16 Pro", limit=10))
    assert asked == ["v1|bid|0"]                                   # друге звернення — з кешу
    xml = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item><ItemID>1</ItemID>'
           '<BuyItNowPrice currencyID="EUR">810.0</BuyItNowPrice></Item></GetItemResponse>')
    assert trading_api.parse_buy_it_now(xml) == 810.0


def test_auctions_listed_but_not_in_stats(fake_ebay):
    import market
    from conftest import listing
    from test_scheduler import consoles
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    auction = listing("auc", "Sony PlayStation 5 Slim 1TB", 200, buyingOptions=["AUCTION", "FIXED_PRICE"],
                      currentBidPrice={"value": "120.00", "currency": "EUR"}, bidCount=3)
    fake_ebay.listings = consoles() + [auction]
    items, window, present = market._fetch_market_items(db.get_watch(wid, 1))
    assert "auc" in [it["item_id"] for it in items]                         # у списках для купівлі є
    stats = market._compute_group_stats(wid, items)
    plain = market._compute_group_stats(wid, [it for it in items if it["item_id"] != "auc"])
    assert stats == plain                                                   # у статистиці — ні
    db.update_listing_observations(wid, items, window, present)
    row = next(r for r in db.get_current_listings(wid) if r["item_id"] == "auc")
    assert (row["current_bid"], row["bid_count"], "AUCTION" in row["buying_options"]) == (120.0, 3, True)

    # аукціон зник — це не продаж і не черга перевірки «продано?»
    import settings
    for _ in range(settings.GONE_MISS_THRESHOLD):
        db.update_listing_observations(wid, items[:-1], window, {it["item_id"] for it in items[:-1]})
    with db.get_conn() as conn:
        r = dict(conn.execute("SELECT status, sold_check FROM listing_obs WHERE item_id = 'auc'").fetchone())
    assert r == {"status": "ended", "sold_check": None}
    assert "auc" not in [x["item_id"] for x in db.get_sold_listings(wid)]


def test_listings_newest_first_toggle(monkeypatch):
    import screen_listings
    sorts = []
    found = [{"item_id": i, "title": f"PS5 #{i}", "total_price": p, "currency": "EUR", "condition": "Gebraucht",
              "url": "u", "created_at": int(time.time() - ago * DAY)} for i, p, ago in
             [("old_cheap", 200, 30), ("new_dear", 400, 0), ("mid", 300, 5)]]

    def search(*a, **kw):
        sorts.append(kw.get("sort"))
        return [dict(x) for x in found]

    monkeypatch.setattr(screen_listings, "search_in_categories", search)
    monkeypatch.setattr(screen_listings, "_annotate_items", lambda items, **kw: items)
    monkeypatch.setattr(screen_listings, "_apply_item_filters", lambda w, items: items)
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "1", "name": "K"}])
    handlers_mod, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"view_listings:{wid}")
    ctx.user_data = {}
    asyncio.run(handlers_mod.view_listings_callback(upd, ctx))
    assert sorts == ["price"] and "Від найдешевших" in shown[-1][0] and "🆕 Спершу найновіші" in shown[-1][1]
    assert shown[-1][0].index("old_cheap") < shown[-1][0].index("new_dear") if "old_cheap" in shown[-1][0] else True

    upd.callback_query.data = f"view_listings:{wid}:new"
    asyncio.run(handlers_mod.view_listings_callback(upd, ctx))
    assert sorts[-1] == "newlyListed" and "Спершу найновіші" in shown[-1][0]
    items = ctx.user_data[screen_listings._listing_state_key(wid)]["items"]
    assert [it["item_id"] for it in items] == ["new_dear", "mid", "old_cheap"]
    assert "💶 Спершу найдешевші" in shown[-1][1]

    upd.callback_query.data = f"view_listings:{wid}"            # з картки товару — пам'ятає вибір
    asyncio.run(handlers_mod.view_listings_callback(upd, ctx))
    assert len(sorts) == 2                                       # з пам'яті, без нового запиту


def test_hide_old_listings_toggle(monkeypatch):
    import screen_listings
    found = [{"item_id": i, "title": f"PS5 #{i}", "total_price": p, "currency": "EUR", "condition": "Gebraucht",
              "url": "u", "created_at": int(time.time() - ago * DAY)} for i, p, ago in
             [("ancient", 150, 244), ("fresh", 300, 5), ("month", 250, 40)]]
    calls = []
    monkeypatch.setattr(screen_listings, "search_in_categories",
                        lambda *a, **kw: calls.append(1) or [dict(x) for x in found])
    monkeypatch.setattr(screen_listings, "_annotate_items", lambda items, **kw: items)
    monkeypatch.setattr(screen_listings, "_apply_item_filters", lambda w, items: items)
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "1", "name": "K"}])
    handlers_mod, shown, press = _screen(monkeypatch)
    upd, ctx = press(f"view_listings:{wid}")
    ctx.user_data = {}
    key = screen_listings._listing_state_key(wid)

    asyncio.run(handlers_mod.view_listings_callback(upd, ctx))
    assert [it["item_id"] for it in ctx.user_data[key]["items"]] == ["ancient", "month", "fresh"]
    assert "🕰 Без старих (>100 дн.)" in shown[-1][1] and "🕰 виставлено" in shown[-1][0]   # стару позначено

    upd.callback_query.data = f"lold:{wid}:on"
    asyncio.run(handlers_mod.listing_old_callback(upd, ctx))
    assert [it["item_id"] for it in ctx.user_data[key]["items"]] == ["month", "fresh"]
    assert "без старших за 100 дн." in shown[-1][0] and "⏱ Межа: 100 дн." in shown[-1][1]

    upd.callback_query.data = f"lold:{wid}:days"                 # 100 → 180
    asyncio.run(handlers_mod.listing_old_callback(upd, ctx))
    assert db.get_old_listing_filter(1) == (True, 180) and "⏱ Межа: 180 дн." in shown[-1][1]
    upd.callback_query.data = f"lold:{wid}:days"                 # 180 → 30
    asyncio.run(handlers_mod.listing_old_callback(upd, ctx))
    assert [it["item_id"] for it in ctx.user_data[key]["items"]] == ["fresh"]

    upd.callback_query.data = f"lold:{wid}:off"
    asyncio.run(handlers_mod.listing_old_callback(upd, ctx))
    assert len(ctx.user_data[key]["items"]) == 3 and "🕰 Без старих (>30 дн.)" in shown[-1][1]
