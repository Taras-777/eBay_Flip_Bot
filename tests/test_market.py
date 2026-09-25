"""Аналіз ринку: формули купівлі/продажу, фільтри лотів, групи статистики."""
import pytest

import market
import settings


def test_percentile():
    assert market.percentile([100, 200, 300, 400, 500], 25) == 200
    assert market.percentile([], 25) is None


def test_max_buy_price_gives_target_profit():
    sale = 500
    buy = market.max_buy_price(sale, 20)
    _, profit = market.estimate_resale_profit(sale, buy)
    assert profit == pytest.approx(sale * 0.20)


def test_min_profit_floor():
    # 10% від 100€ = 10€ < мінімальних 15€ → діє мінімум
    _, profit = market.estimate_resale_profit(100, market.max_buy_price(100, 10))
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