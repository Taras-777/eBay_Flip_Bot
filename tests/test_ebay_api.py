"""Запити до eBay: параметри пошуку, кілька категорій, ліміти запитів."""
import time

import pytest

import ebay_api
from conftest import FakeResponse, listing


@pytest.fixture
def captured(monkeypatch):
    """Перехоплює запити до eBay і повертає їхні параметри."""
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse({"itemSummaries": []})

    monkeypatch.setattr(ebay_api, "_get_access_token", lambda: "TOKEN")
    monkeypatch.setattr(ebay_api, "_request_with_retries", fake_request)
    return calls


def test_search_params(captured):
    ebay_api._browse_search("PlayStation 5", condition_ids="1000,3000", exclude_terms="defekt teile",
                            category_id="139971", min_price=195, sort="price", offset=100)
    params = captured[0]["params"]
    assert params["q"] == "PlayStation 5 -defekt -teile"  # слова за абеткою
    assert params["category_ids"] == "139971"
    assert params["sort"] == "price" and params["offset"] == "100"
    f = params["filter"]
    assert "conditionIds:{1000|3000}" in f          # eBay вимагає «|», а не кому
    assert "buyingOptions:{FIXED_PRICE}" in f       # без аукціонів
    assert "deliveryCountry:DE" in f
    assert "price:[195.00..]" in f and "priceCurrency:EUR" in f


def test_no_price_filter_without_min_price(captured):
    ebay_api._browse_search("PS5")
    assert "price:" not in captured[0]["params"]["filter"]


def test_search_skips_auctions_and_parts(fake_ebay):
    fake_ebay.listings = [
        listing("ok", "Sony PlayStation 5 Slim 1TB Konsole", 400),
        listing("auction", "Sony PlayStation 5 Slim 1TB Konsole", 100, buyingOptions=["AUCTION"]),
        listing("parts", "Sony PlayStation 5 Slim 1TB Konsole", 100, condition_id="7000"),
    ]
    ids = [it["item_id"] for it in ebay_api.search_active_items("PlayStation 5")]
    assert ids == ["ok"]


def test_search_counts_raw_results(fake_ebay):
    fake_ebay.listings = [listing("a", "Sony PlayStation 5 Konsole", 400),
                          listing("b", "Hülle für Handy", 10)]
    stats = {}
    found = ebay_api.search_active_items("PlayStation 5", stats=stats)
    assert stats["raw"] == 2 and len(found) == 1  # «сирих» два, підходить одне


def test_several_categories_are_merged_without_duplicates(monkeypatch):
    def fake_search(category_id=None, stats=None, **kw):
        stats["raw"] = stats.get("raw", 0) + 100
        common = {"item_id": "same", "total_price": 500}
        own = {"item_id": f"only-{category_id}", "total_price": 400 if category_id == "b" else 450}
        return [common, own]

    monkeypatch.setattr(ebay_api, "search_active_items", fake_search)
    stats = {}
    items = ebay_api.search_in_categories(["a", "b"], sort="price", query="x", stats=stats)
    assert [it["item_id"] for it in items] == ["only-b", "only-a", "same"]  # від найдешевших
    assert stats["raw"] == 200


def test_rate_limit_parsing_picks_daily_window(monkeypatch):
    payload = {"rateLimits": [{"resources": [{"rates": [
        {"timeWindow": 60, "limit": 100, "remaining": 90},
        {"timeWindow": 86400, "limit": 5000, "remaining": 4910, "count": 90},
    ]}]}]}
    monkeypatch.setattr(ebay_api, "_get_access_token", lambda: "TOKEN")
    monkeypatch.setattr(ebay_api, "_request_with_retries", lambda *a, **k: FakeResponse(payload))
    data = ebay_api.fetch_browse_rate_limit()
    assert (data["limit"], data["remaining"], data["count"]) == (5000, 4910, 90)
    line = ebay_api.api_usage_line()
    assert "<b>90</b> / 5000" in line
    assert "залишилось" not in line and "бот використовує" not in line


def test_budget_uses_fresh_ebay_data(monkeypatch):
    ebay_api._rate_limit_cache.update(data={"limit": 5000, "remaining": 1000, "count": 4000, "reset": None},
                                      fetched_at=time.time())
    # з 1000 залишку eBay боту доступно лише те, що вкладається в його власний бюджет
    assert ebay_api.browse_budget_left() == 1000 - (5000 - ebay_api.DAILY_BROWSE_BUDGET)


def test_search_limit_allows_200(captured):
    ebay_api._browse_search("PS5", limit=500)
    assert captured[0]["params"]["limit"] == "200"


def test_category_names_are_kept(fake_ebay):
    fake_ebay.listings = [listing("a", "Sony PlayStation 5 Konsole", 400,
                                  categories=[{"categoryId": "139971", "categoryName": "Konsolen"}])]
    found = ebay_api.search_active_items("PlayStation 5")
    assert found[0]["category_names"] == ["Konsolen"]


def test_budget_after_reset_does_not_wait_for_stale_data():
    """Ліміт eBay уже скинувся, а в кеші — вчорашні 5440/5000: бот не має стояти до наступного оновлення."""
    ebay_api._rate_limit_cache.update(
        data={"limit": 5000, "remaining": 0, "count": 5440, "reset": time.time() - 60}, fetched_at=time.time())
    assert ebay_api.browse_budget_left() > 0
    assert "Ліміт щойно скинувся" in ebay_api.api_usage_line()


def test_reset_line_says_today_or_tomorrow():
    ebay_api._rate_limit_cache.update(
        data={"limit": 5000, "remaining": 100, "count": 4900, "reset": time.time() + 3600}, fetched_at=time.time())
    line = ebay_api.api_usage_line()
    assert "Ліміт скинеться сьогодні о" in line or "Ліміт скинеться завтра о" in line
    assert 3500 < ebay_api.seconds_until_reset() <= 3600