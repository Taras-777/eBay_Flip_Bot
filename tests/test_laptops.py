"""Ноутбуки: клас замість точної конфігурації, ширші класи, попередження."""
import time

import pytest

import db
import laptops
import market


@pytest.mark.parametrize("title, spec", [
    ("ASUS ROG Strix G16 i7-13650HX RTX 4060 16GB 1TB", "RTX 4060 · i7 13 gen · 16GB"),
    ("MSI Katana 15 i7-13620H RTX 4060 8GB 16GB RAM 1TB", "RTX 4060 · i7 13 gen · 16GB"),   # 8GB — відеопам'ять
    ("Lenovo Legion 5 Ryzen 7 5800H RTX 3060 6GB 16GB 512GB", "RTX 3060 · Ryzen 7 5000 · 16GB"),
    ("Acer Nitro V 15 i5-13420H GeForce RTX4050 16 GB 512 GB", "RTX 4050 · i5 13 gen · 16GB"),
    ("ASUS TUF i5-11400H GTX 1650 8GB 512GB", "GTX 1650 · i5 11 gen · 8GB"),
    ("MSI RTX 4070 8GB GDDR6 32GB DDR5 1TB", "RTX 4070 · 32GB+"),
    ("Lenovo ThinkPad X1 Carbon Gen 10 i5-1240P 16GB 256GB", "iGPU · i5 12 gen · 16GB"),
    ("Dell XPS 13 Core Ultra 7 155H 16GB 1TB", "iGPU · Core Ultra 7 (S1) · 16GB"),
    ("Apple MacBook Pro 14 M3 Pro 18GB 512GB", 'M3 Pro · 14" · 18GB · 512GB'),
    ("Apple MacBook Pro 14,2 Zoll M3 8GB RAM 512GB SSD", 'M3 · 14" · 8GB · 512GB'),
    ('MacBook Pro 16" M3 Max 36GB 1TB', 'M3 Max · 16" · 36GB · 1TB'),
    ("HP Victus 15 Gaming Laptop", "unspecified"),
])
def test_laptop_class(title, spec):
    assert laptops.laptop_spec(title) == spec


def test_class_from_aspects_when_title_is_short():
    aspects = {"grafikprozessor": "NVIDIA GeForce RTX 4050", "arbeitsspeichergröße": "16 GB",
               "prozessor": "Intel Core i5-13420H"}
    assert laptops.laptop_spec("HP Victus 15 Gaming Laptop", aspects) == "RTX 4050 · i5 13 gen · 16GB"
    assert laptops.needs_aspects("GPU ? · i5 13 gen") and not laptops.needs_aspects("RTX 4050 · i5 13 gen")


def test_parents_and_matching():
    assert laptops.spec_parents("RTX 4060 · i7 13 gen · 16GB") == ["RTX 4060 · i7 13 gen", "RTX 4060"]
    assert laptops.spec_parents("256GB") == []
    assert laptops.spec_matches("RTX 4060 · i7 13 gen · 16GB", "RTX 4060")
    assert not laptops.spec_matches("RTX 4060 Ti · i7", "RTX 4060")      # інша відеокарта
    assert laptops.spec_matches("256GB", "*") and not laptops.spec_matches("512GB", "256GB")


def test_is_laptop():
    assert laptops.is_laptop("Gigabyte G5", ["PC Notebooks & Netbooks"])
    assert laptops.is_laptop(query="HP Victus 15")
    assert laptops.is_laptop("Apple MacBook Air M2")
    assert not laptops.is_laptop("iPhone 15 Pro 256GB", ["Handys & Smartphones"], "iPhone 15 Pro")


def test_warnings():
    w = laptops.laptop_warnings("Laptop US Layout ohne Netzteil BIOS Passwort")
    assert len(w) == 3 and "US/UK" in w[0]
    assert laptops.laptop_warnings("ASUS USB-C 16GB QWERTZ") == []
    assert laptops.laptop_warnings("x", {"tastaturlayout": "QWERTY (US)"})
    assert laptops.laptop_warnings("x", {"tastaturlayout": "Deutsch (QWERTZ)"}) == []
    assert "iCloud" in laptops.laptop_warnings("MacBook iCloud gesperrt")[0]


# ---------- ринок: класи й ширші групи ----------

def _items(spec, price, n, cond="used"):
    return [{"item_id": f"{spec}-{price}-{i}", "title": "x", "cond_group": cond, "spec_group": spec,
             "total_price": price + i} for i in range(n)]


def test_group_stats_fall_back_to_wider_class():
    items = (_items("RTX 4060 · i7 13 gen · 16GB", 900, 8) + _items("RTX 4060 · i5 12 gen · 16GB", 800, 3)
             + _items("RTX 3050 · Ryzen 5 5000 · 16GB", 500, 8))
    stats = market._compute_group_stats(1, items)
    keys = {spec for _, spec in stats}
    assert {"RTX 4060 · i7 13 gen · 16GB", "RTX 4060", "RTX 3050 · Ryzen 5 5000 · 16GB", "*"} <= keys
    assert "RTX 3050" not in keys and "RTX 3050 · Ryzen 5 5000" not in keys   # ті самі лоти — не дублюємо
    # i5 12 gen — лише 3 оголошення: ціну беремо з «RTX 4060», а не з усього ринку разом з RTX 3050
    stat = market._stat_for_item(stats, {"cond_group": "used", "spec_group": "RTX 4060 · i5 12 gen · 16GB"})
    assert stat["spec_group"] == "RTX 4060" and stat["median_price"] > 800


def test_gone_prices_include_narrower_classes():
    now = int(time.time())
    with db.get_conn() as conn:
        for item, spec, price in [("a", "RTX 4060 · i7 13 gen · 16GB", 900), ("b", "RTX 4060", 850),
                                  ("c", "RTX 4060 Ti · i7", 1100), ("d", "RTX 3050", 500)]:
            conn.execute("""INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price, first_seen,
                            last_seen, status, gone_at) VALUES (1, ?, 'used', ?, ?, ?, ?, 'gone', ?)""",
                         (item, spec, price, now, now, now))
    assert sorted(db.get_gone_prices(1, "used", "RTX 4060")) == [850, 900]


def test_annotate_uses_classes_only_for_laptops(monkeypatch):
    monkeypatch.setattr(market, "ASPECT_LOOKUP_ENABLED", False)
    lap = db.add_watch(1, "HP Victus 15", "HP Victus 15", "", "", 15,
                       categories=[{"id": "175672", "name": "PC Notebooks & Netbooks"}])
    phone = db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "", "", 15)
    items = [{"item_id": "1", "title": "HP Victus 15 Ryzen 5 5600H RTX 3050 16GB 512GB"}]
    market._annotate_items(items, watch=db.get_watch(lap, 1))
    assert items[0]["spec_group"] == "RTX 3050 · Ryzen 5 5000 · 16GB"
    items = [{"item_id": "2", "title": "iPhone 15 Pro 256GB"}]
    market._annotate_items(items, watch=db.get_watch(phone, 1))
    assert items[0]["spec_group"] == "256GB"


def test_laptop_sales_window_is_longer():
    import sales
    day = 86400
    row = {"price": 900, "cond_group": "used", "spec_group": "RTX 4060 · i7 13 gen · 16GB",
           "gone_at": time.time() - 20 * day, "created_at": time.time() - 25 * day}
    assert "1 продаж за 30 днів" in sales.sales_note([row], "used", "RTX 4060 · i7 13 gen · 16GB")
    phone = {**row, "spec_group": "256GB"}
    assert sales.sales_note([phone], "used", "256GB") == ""        # у телефонів — як і було, 14 днів


# ---------- невідома відеокарта ----------

def test_unknown_gpu_not_grouped_and_not_a_deal(fake_ebay, monkeypatch):
    import asyncio
    import scheduler
    from conftest import listing
    from test_scheduler import make_app
    items = _items("GPU ? · i7 14 gen · 16GB", 900, 10) + _items("RTX 4060 · i7 13 gen · 16GB", 900, 8)
    keys = {spec for _, spec in market._compute_group_stats(1, items)}
    assert not any(k.startswith("GPU ?") for k in keys) and "*" in keys

    monkeypatch.setattr(market, "ASPECT_LOOKUP_ENABLED", False)   # характеристики ще не прочитані
    fake_ebay.listings = [listing(f"n{i}", f"MSI Katana 15 i7-13620H RTX 4060 16GB Nr{i}", 900 + i)
                          for i in range(12)]
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15)
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))
    fake_ebay.listings = [listing("cheap", "MSI Katana 15 B13VFK Gaming", 300)] + fake_ebay.listings
    asyncio.run(scheduler.check_all_watches(app))
    assert db.get_inbox_deals(1)[1] == 0                         # клас невідомий — не пропонуємо
    assert db.get_seen_items(wid, ["cheap"]) == {}               # і не забуваємо: оцінимо пізніше


def test_unknown_gpu_items_read_first(monkeypatch):
    fetched = []
    monkeypatch.setattr(market, "fetch_item_aspects", lambda item_id: fetched.append(item_id) or {})
    monkeypatch.setattr(market, "browse_budget_left", lambda: 5000)
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15)
    items = [{"item_id": "known", "title": "MSI Katana i7-13620H RTX 4060 16GB"},
             {"item_id": "unknown", "title": "MSI Katana 15 B13VFK"}]
    market._annotate_items(items, max_lookups=1, watch=db.get_watch(wid, 1))
    assert fetched == ["unknown"]
