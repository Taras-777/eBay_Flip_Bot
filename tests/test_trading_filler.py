"""Trading API як друга квота: характеристики через Trading, сміливіше читання, «заповнювач» до 90%."""
import time

from analysis import market
from core import db
from ebay import ebay_api, trading_api, trading_filler

ITEM = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
        '<ItemID>1</ItemID><SellingStatus><ListingStatus>Active</ListingStatus><QuantitySold>0</QuantitySold>'
        '<ConvertedCurrentPrice currencyID="EUR">520.0</ConvertedCurrentPrice></SellingStatus>'
        '<ShippingDetails><ShippingServiceOptions><ShippingServiceCost currencyID="EUR">6.99</ShippingServiceCost>'
        '</ShippingServiceOptions><ShippingServiceOptions><ShippingServiceCost currencyID="EUR">4.99'
        '</ShippingServiceCost></ShippingServiceOptions></ShippingDetails>'
        '<ItemSpecifics><NameValueList><Name>Prozessor</Name><Value>Intel Core i7-13620H</Value></NameValueList>'
        '</ItemSpecifics>'
        '<Description>&lt;p&gt;Grafikkarte: NVIDIA GeForce RTX 4060, 16 GB RAM&lt;/p&gt;</Description>'
        '</Item></GetItemResponse>')
NOT_FOUND = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
             '<Errors><ErrorCode>17</ErrorCode></Errors></GetItemResponse>')


# ---------- 1. характеристики через Trading ----------

def test_parse_item_details_with_description_and_shipping():
    aspects = trading_api.parse_item_details(ITEM)
    assert aspects["prozessor"] == "Intel Core i7-13620H"
    assert aspects["grafikkarte (aus beschreibung)"] == "RTX 4060"
    assert aspects["_beschreibung_geprueft"]
    assert trading_api.parse_item_details(NOT_FOUND) == {}                   # оголошення вже немає
    assert trading_api.parse_get_item(ITEM)["details"]["shipping_cost"] == 4.99  # найдешевша доставка


def test_aspects_via_trading_with_browse_fallback(monkeypatch):
    browse = []
    monkeypatch.setattr(ebay_api, "_browse_item_aspects", lambda item_id: browse.append(item_id) or {"b": "1"})
    monkeypatch.setattr(trading_api, "trading_room", lambda: 0)               # акаунт не підключено
    assert ebay_api.fetch_item_aspects("x") == {"b": "1"}
    monkeypatch.setattr(trading_api, "trading_room", lambda: 500)
    monkeypatch.setattr(trading_api, "fetch_item_details", lambda item_id: {"t": "1"})
    assert ebay_api.fetch_item_aspects("y") == {"t": "1"} and browse == ["x"]   # Browse не чіпаємо

    def broken(item_id):
        raise RuntimeError("Trading API: тимчасова помилка")
    monkeypatch.setattr(trading_api, "fetch_item_details", broken)
    assert ebay_api.fetch_item_aspects("z") == {"b": "1"} and browse == ["x", "z"]   # збій — Browse


# ---------- 2. сміливіше читання ----------

def test_more_lookups_when_trading_available(monkeypatch):
    fetched = []
    monkeypatch.setattr(market, "fetch_item_aspects", lambda item_id: fetched.append(item_id) or {"marke": "x"})
    items = [{"item_id": f"i{n}", "title": f"Gadget {n}"} for n in range(80)]
    monkeypatch.setattr(market, "trading_room", lambda: 0)
    monkeypatch.setattr(market, "browse_budget_left", lambda: 4000)
    market._annotate_items([dict(it) for it in items[:20]], max_lookups=5)
    assert len(fetched) == 5                                                   # Browse — економно
    fetched.clear()
    monkeypatch.setattr(market, "trading_room", lambda: 1000)
    market._annotate_items([dict(it) for it in items[20:]], max_lookups=5)
    assert len(fetched) == 60                                                  # Trading — TRADING_SPEC_LOOKUPS
    fetched.clear()
    monkeypatch.setattr(market, "trading_room", lambda: 7)
    market._annotate_items([{"item_id": f"j{n}", "title": "G"} for n in range(30)], max_lookups=5)
    assert len(fetched) == 7                                                   # не більше вільного запасу


# ---------- 3. «заповнювач» ----------

def test_spare_paces_to_ninety_percent(monkeypatch):
    from datetime import datetime, timezone
    reset = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc).timestamp()      # 09:00 Берлін = північ у Лос-Анджелесі
    assert trading_filler.day_fraction(reset) == 0
    assert trading_filler.day_fraction(reset + 12 * 3600) == 0.5
    monkeypatch.setattr(trading_filler, "trading_calls_today", lambda: (1000, True))
    assert trading_filler.trading_spare(reset + 12 * 3600) == 4000 * 0.9 * 0.5 - 1000   # 800 — відстаємо
    monkeypatch.setattr(trading_filler, "trading_calls_today", lambda: (2500, True))
    assert trading_filler.trading_spare(reset + 12 * 3600) < 0                 # випереджаємо — нічого


def test_filler_runs_tasks_in_order_within_spare(monkeypatch):
    calls = []
    monkeypatch.setattr(trading_filler, "is_connected", lambda: True)
    monkeypatch.setattr(trading_filler, "trading_spare", lambda now=None: 100)

    def task(name, used):
        def run(limit):
            calls.append((name, limit))
            return min(used, limit)
        run.__name__ = name
        return run
    monkeypatch.setattr(trading_filler, "refresh_deals", task("deals", 30))
    monkeypatch.setattr(trading_filler, "refresh_tracks", task("tracks", 60))
    monkeypatch.setattr(trading_filler, "backfill_seller_types", task("sellers", 50))
    monkeypatch.setattr(trading_filler, "backfill_laptop_specs", task("laptops", 50))
    assert trading_filler.run_filler(limit=150) == 100
    assert calls == [("deals", 100), ("tracks", 70), ("sellers", 10)]          # до ноутбуків черга не дійшла
    monkeypatch.setattr(trading_filler, "is_connected", lambda: False)
    calls.clear()
    assert trading_filler.run_filler() == 0 and calls == []                    # без акаунта eBay — нічого


def test_refresh_tracks_updates_price_and_gone(monkeypatch):
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15)
    old = int(time.time()) - 20 * 86400
    db.track_prices(wid, [{"item_id": "a", "title": "PS5 A", "total_price": 600, "created_at": old},
                          {"item_id": "b", "title": "PS5 B", "total_price": 500, "created_at": old},
                          {"item_id": "new", "title": "PS5 N", "total_price": 500, "created_at": int(time.time())}])
    with db.get_conn() as conn:
        conn.execute("UPDATE price_track SET last_seen = ?", (int(time.time()) - 5 * 3600,))
    answers = {"a": {"result": "active", "details": {"sold_price": 480.0, "shipping_cost": 4.99}},
               "b": {"result": "unsold", "details": {}}}
    monkeypatch.setattr(trading_filler, "get_item_status", lambda item_id: answers[item_id])
    assert trading_filler.refresh_tracks(10) == 2                              # «new» — ще молоде, не чіпаємо
    row = db.get_track_row(wid, "a")
    assert row["price"] == 484.99 and row["first_price"] == 600 and row["price_changed_at"]
    assert db.get_track_row(wid, "b")["last_seen"] == 0                        # знято — зі «📉» зникає
