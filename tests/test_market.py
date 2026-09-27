"""Аналіз ринку: формули купівлі/продажу, фільтри лотів, групи статистики."""
import pytest

import market
import settings


def test_percentile():
    assert market.percentile([100, 200, 300, 400, 500], 25) == 200
    assert market.percentile([], 25) is None


def test_max_buy_price_leaves_min_profit():
    for sale in (100, 500, 1200):
        _, profit = market.estimate_resale_profit(sale, market.max_buy_price(sale))
        assert profit == pytest.approx(settings.MIN_PROFIT_EUR)


def test_compat_aspects_mark_accessory():
    w = {"query": "iphone 15 pro", "require_spec": None, "required_aspect": None}
    items = [
        {"item_id": "a", "title": "Hülle für iPhone 15 Pro 128GB", "spec_group": "128GB",
         "aspects": {"kompatibles modell": "iPhone 15 Pro"}},
        {"item_id": "b", "title": "Apple iPhone 15 Pro 128GB", "spec_group": "128GB", "aspects": {}},
    ]
    assert [i["item_id"] for i in market._apply_item_filters(w, items)] == ["b"]


def test_required_aspects_all_must_be_present():
    w = {"query": "iphone 15 pro", "require_spec": 0,
         "required_aspect": '["Speicherkapazität", "Netzwerk"]'}
    items = [
        {"item_id": "title+net", "title": "iPhone 15 Pro 128GB", "spec_group": "128GB",
         "aspects": {"netzwerk": "Ohne Vertrag"}},
        {"item_id": "no_net", "title": "iPhone 15 Pro 256GB", "spec_group": "256GB", "aspects": {}},
        {"item_id": "unchecked", "title": "iPhone 15 Pro", "spec_group": "unspecified", "aspects": None},
    ]
    assert [i["item_id"] for i in market._apply_item_filters(w, items)] == ["title+net"]


def test_group_stats_only_for_large_groups():
    items = ([{"cond_group": "used", "spec_group": "825GB", "total_price": 500 + i} for i in range(10)]
             + [{"cond_group": "used", "spec_group": "1TB", "total_price": 450 + i} for i in range(3)])
    stats = market._compute_group_stats(1, items)
    assert ("used", "*") in stats and ("used", "825GB") in stats
    assert ("used", "1TB") not in stats  # 3 оголошення — замало для окремої групи


def test_estimate_resale_profit():
    # 500€ продаж − 15% комісії − 7€ доставки = 418€; купівля 300€ → 118€
    _, profit = market.estimate_resale_profit(500, 300)
    assert profit == pytest.approx(118)


def _no_budget_limits(monkeypatch):
    monkeypatch.setattr(market, "browse_budget_left", lambda: 4000)


def test_annotate_items_uses_cache_on_second_call(monkeypatch):
    _no_budget_limits(monkeypatch)
    fetched = []

    def fake_fetch(item_id):
        fetched.append(item_id)
        return {"speicherkapazität": "825 GB"}

    monkeypatch.setattr(market, "fetch_item_aspects", fake_fetch)
    watch = {"required_aspect": None, "require_spec": None}
    items = [{"item_id": f"x{i}", "title": "Sony PlayStation 5"} for i in range(5)]
    market._annotate_items(items, max_lookups=10, watch=watch)
    assert sorted(fetched) == [f"x{i}" for i in range(5)]
    assert all(it["aspects"] for it in items)

    again = [{"item_id": f"x{i}", "title": "Sony PlayStation 5"} for i in range(5)]
    market._annotate_items(again, max_lookups=10, watch=watch)
    assert len(fetched) == 5  # другий раз — з кешу, без запитів
    assert all(it["aspects"] for it in again)


def test_annotate_items_respects_lookup_limit(monkeypatch):
    _no_budget_limits(monkeypatch)
    monkeypatch.setattr(market, "fetch_item_aspects", lambda item_id: {"a": "b"})
    items = [{"item_id": f"y{i}", "title": "Sony PlayStation 5"} for i in range(10)]
    market._annotate_items(items, max_lookups=3, watch={"required_aspect": None, "require_spec": None})
    assert sum(1 for it in items if it["aspects"] is not None) == 3


def test_annotate_items_runs_lookups_in_parallel(monkeypatch):
    import time
    _no_budget_limits(monkeypatch)

    def slow_fetch(item_id):
        time.sleep(0.2)
        return {"a": "b"}

    monkeypatch.setattr(market, "fetch_item_aspects", slow_fetch)
    items = [{"item_id": f"z{i}", "title": "Sony PlayStation 5"} for i in range(16)]
    start = time.time()
    market._annotate_items(items, max_lookups=16, watch={"required_aspect": None, "require_spec": None})
    assert time.time() - start < 1.5  # по черзі було б 3,2 с


def test_failed_lookup_does_not_break_others(monkeypatch):
    _no_budget_limits(monkeypatch)

    def flaky(item_id):
        if item_id == "bad":
            raise RuntimeError("eBay недоступний")
        return {"a": "b"}

    monkeypatch.setattr(market, "fetch_item_aspects", flaky)
    items = [{"item_id": "bad", "title": "PS5"}, {"item_id": "good", "title": "PS5"}]
    market._annotate_items(items, max_lookups=5, watch={"required_aspect": None, "require_spec": None})
    assert items[0]["aspects"] is None and items[1]["aspects"] == {"a": "b"}


def test_rejected_item_filtered_only_for_its_watch():
    import db
    import learning
    w1 = db.get_watch(db.add_watch(1, "PS5", "PS5", "", "", 25), 1)
    w2 = db.get_watch(db.add_watch(1, "PS5 Digital", "PS5 Digital", "", "", 25), 1)
    learning.hide_item(w1, "x", "Sony PlayStation 5")
    item = [{"item_id": "x", "title": "Sony PlayStation 5 825GB", "spec_group": "825GB", "aspects": {}}]
    assert market._apply_item_filters(w1, item) == []
    assert len(market._apply_item_filters(w2, item)) == 1


def test_auto_min_price_from_market_stats():
    import db
    import ebay_api
    wid = db.add_watch(1, "PS5", "PS5", "", "", 25)
    w = db.get_watch(wid, 1)
    assert ebay_api.effective_min_price(w) is None          # ціни ще не пораховані
    db.upsert_market_stats(wid, "used", "*", 480, 20, sale_price=460)
    db.upsert_market_stats(wid, "new", "*", 600, 10, sale_price=580)
    assert ebay_api.effective_min_price(w) == 190           # 40% від 480, округлено до 5
    with db.get_conn() as conn:
        conn.execute("UPDATE watches SET min_price = 300 WHERE id = ?", (wid,))
    assert ebay_api.effective_min_price(db.get_watch(wid, 1)) == 300  # своя ціна важливіша


def test_items_from_accessory_categories_are_dropped():
    import db
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 25,
                       categories=[{"id": "15032", "name": "Handys & Kommunikation"}])
    w = db.get_watch(wid, 1)
    items = [
        {"item_id": "phone", "title": "iPhone 16 Pro 128GB", "spec_group": "128GB", "aspects": {},
         "category_names": ["Handys & Smartphones"]},
        {"item_id": "case", "title": "iPhone 16 Pro 128GB", "spec_group": "128GB", "aspects": {},
         "category_names": ["Handyhüllen & -taschen"]},
    ]
    assert [i["item_id"] for i in market._apply_item_filters(w, items)] == ["phone"]


def test_accessory_categories_kept_when_user_chose_them():
    import db
    wid = db.add_watch(1, "AirPods Case", "AirPods Case", "", "", 25,
                       categories=[{"id": "1", "name": "Handyhüllen & -taschen"}])
    item = [{"item_id": "case", "title": "AirPods Case", "spec_group": "unspecified", "aspects": {},
             "category_names": ["Handyhüllen & -taschen"]}]
    w = dict(db.get_watch(wid, 1), require_spec=0)
    assert len(market._apply_item_filters(w, item)) == 1


def test_auto_min_price_before_stats_uses_seen_listings():
    import time
    import db
    import ebay_api
    wid = db.add_watch(1, "iPhone 16 Pro", "iPhone 16 Pro", "", "", 25)
    now = int(time.time())
    with db.get_conn() as conn:
        for i, price in enumerate([500, 550, 600]):
            conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price,
                            first_seen, last_seen, status) VALUES (?, ?, 'used', '128GB', ?, ?, ?, 'active')""",
                         (wid, f"p{i}", price, now, now))
    # статистики ще немає, але 3 справжні оголошення вже є → 40% від медіани 550
    assert ebay_api.effective_min_price(db.get_watch(wid, 1)) == 220