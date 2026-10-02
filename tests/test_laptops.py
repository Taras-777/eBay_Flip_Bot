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


# ---------- батьківська й вкладена категорії ----------

COMPUTER = {"id": "58058", "name": "Computer, Tablets & Netzwerk"}
NOTEBOOKS = {"id": "175672", "name": "Notebooks & Netbooks"}


def test_collapse_keeps_only_parent(no_category_tree):
    import ebay_api
    no_category_tree["58058"] = {"175672", "177"}
    assert ebay_api.collapse_categories([COMPUTER, NOTEBOOKS]) == [COMPUTER]
    assert ebay_api.collapse_categories([NOTEBOOKS, {"id": "9355", "name": "Handys"}]) == \
        [NOTEBOOKS, {"id": "9355", "name": "Handys"}]                    # не вкладені — обидві лишаються


def test_search_skips_nested_category(no_category_tree, monkeypatch):
    import ebay_api
    no_category_tree["58058"] = {"175672"}
    searched = []
    monkeypatch.setattr(ebay_api, "search_active_items",
                        lambda category_id=None, stats=None, **kw: searched.append(category_id) or [])
    ebay_api.search_in_categories(["58058", "175672"], query="x")
    assert searched == ["58058"]                                          # один запит замість двох


def test_existing_watch_categories_collapsed_without_losing_history(no_category_tree):
    no_category_tree["58058"] = {"175672"}
    from test_sold_check import add_sale
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15, categories=[COMPUTER, NOTEBOOKS])
    add_sale(wid, "s1", 900, "RTX 4060", confirmed=True)
    assert market.collapse_watch_categories() == 1
    assert db.get_watch_categories(db.get_watch(wid, 1)) == [COMPUTER]
    assert [r["item_id"] for r in db.get_sold_listings(wid)] == ["s1"]  # історія на місці
    assert market.collapse_watch_categories() == 0


def test_removing_nested_category_in_editor_keeps_history(no_category_tree, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import access
    import handlers
    from conftest import patch_ui
    from test_sold_check import add_sale, make_update
    no_category_tree["58058"] = {"175672"}
    wid = db.add_watch(1, "MSI Katana", "MSI Katana", "", "", 15, categories=[COMPUTER, NOTEBOOKS])
    add_sale(wid, "s1", 900, "RTX 4060", confirmed=True)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    patch_ui(monkeypatch, "_show_watch_details", AsyncMock())
    ctx = MagicMock()
    ctx.user_data = {f"cat_options_{wid}": [COMPUTER, NOTEBOOKS], f"cat_selected_{wid}": {"58058"}}
    asyncio.run(handlers.set_category_callback(make_update(f"setcat:{wid}:done"), ctx))
    assert [r["item_id"] for r in db.get_sold_listings(wid)] == ["s1"]


def test_category_descendants_parses_subtree(no_category_tree, monkeypatch):
    import ebay_api
    from conftest import FakeResponse
    payload = {"categorySubtreeNode": {"category": {"categoryId": "58058"}, "childCategoryTreeNodes": [
        {"category": {"categoryId": "175672"}, "childCategoryTreeNodes": [{"category": {"categoryId": "177"}}]},
        {"category": {"categoryId": "171485"}}]}}
    monkeypatch.setattr(ebay_api, "_get_access_token", lambda: "T")
    monkeypatch.setattr(ebay_api, "get_category_tree_id", lambda: "77")
    calls = []
    monkeypatch.setattr(ebay_api, "_request_with_retries", lambda *a, **k: calls.append(1) or FakeResponse(payload))
    assert no_category_tree.real("58058") == {"175672", "177", "171485"}
    assert no_category_tree.real("58058") == {"175672", "177", "171485"} and len(calls) == 1   # з кешу


def test_category_picker_shows_parent_and_nested(no_category_tree):
    import ebay_api
    import screen_common
    no_category_tree["58058"] = {"175672", "175673", "171485"}
    no_category_tree["175673"] = {"171485"}
    options = [{"id": "175673", "name": "Computer-Komponenten & -Teile", "count": 6943},
               {"id": "58058", "name": "Computer, Tablets & Netzwerk", "count": 9400},
               {"id": "175672", "name": "Notebooks & Netbooks", "count": 332},
               {"id": "9355", "name": "Handys & Smartphones", "count": 5}]
    ebay_api.mark_parent_categories(options)
    assert options[2]["parent"] == "58058" and options[0]["parent"] == "58058" and "parent" not in options[3]
    rows = [r[0].text for r in screen_common._category_keyboard(options, "cat:", {"58058"}).inline_keyboard]
    assert rows[0].startswith("☑️ 📂 Комп'ютери, планшети й мережа")
    assert rows[1].startswith("🔹 ↳ Комплектуючі") and rows[2].startswith("🔹 ↳ Ноутбуки й нетбуки")
    assert rows[3].startswith("⬜ Мобільні")                       # не вкладена — як і раніше


# ---------- запчастини «для ноутбука» ----------

@pytest.mark.parametrize("title, part", [
    ("Samsung 32GB DDR4 3200MHz SO-DIMM RAM für ASUS ROG Strix G15 G513", True),
    ("32GB 16GB RAM Speicher passend für Asus G513RM-HF222X ROG Strix G15 (2022)", True),
    ("Netzteil für Lenovo Legion 5 230W", True),
    ("Akku für Laptop MSI Katana GF66", True),
    ("ASUS ROG Strix G15 Tastatur DE beleuchtet", True),
    ("ASUS ROG Strix G15 G513RM Ryzen 7 6800H RTX 3060 16GB 1TB", False),
    ("ASUS ROG Strix G15 Gaming Laptop beleuchtete Tastatur 16GB", False),
    ("Lenovo Legion 5 Notebook mit Netzteil 16GB", False),
    ("ROG Strix G15 Laptop für Gamer", False),
    ("MacBook Pro 14 M3 Pro 18GB 512GB", False),
])
def test_laptop_part_detection(title, part):
    assert laptops.looks_like_laptop_part(title) == part


def test_part_category_without_cpu_is_part():
    assert laptops.looks_like_laptop_part("ASUS ROG Strix G15 Gaming", ["Arbeitsspeicher (RAM)"])
    # ноутбук не в тій категорії, але з процесором і відеокартою — лишається
    assert not laptops.looks_like_laptop_part("ROG Strix G18 Ryzen9 8940HX RTX5060 32GB", ["CPU-Lüfter & Kühlkörper"])


def test_parts_filtered_and_pruned_from_history():
    wid = db.add_watch(1, "rog strix g15", "rog strix g15", "", "", 15)
    w = db.get_watch(wid, 1)
    items = [{"item_id": "ram", "title": "32GB RAM Speicher passend für Asus ROG Strix G15", "spec_group": "x"},
             {"item_id": "lap", "title": "ROG Strix G15 Ryzen 7 RTX 3060 16GB", "spec_group": "x"}]
    assert [it["item_id"] for it in market._apply_item_filters(w, items)] == ["lap"]
    phone = db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "", "", 15)       # не ноутбук — правило не діє
    assert market._apply_item_filters(db.get_watch(phone, 1), [dict(items[0])])

    db.update_listing_observations(wid, [
        {"item_id": "ram", "title": items[0]["title"], "cond_group": "new", "total_price": 255},
        {"item_id": "lap", "title": items[1]["title"], "cond_group": "used", "total_price": 700}])
    assert market.prune_laptop_parts() == 1
    assert [r["item_id"] for r in db.get_all_listing_rows(wid)] == ["lap"]


def test_laptop_category_protects_short_titles():
    nb = ["PC Notebooks & Netbooks", "Computer, Tablets & Netzwerk"]
    assert not laptops.looks_like_laptop_part("ASUS ROG Strix G15 16GB 1TB Display 144Hz", nb)
    assert not laptops.looks_like_laptop_part("ASUS ROG Strix G15 gebraucht", nb)
    assert laptops.looks_like_laptop_part("RAM Speicher passend für ROG Strix G15", nb)   # явна запчастина
    assert laptops.looks_like_laptop_part("ASUS ROG Strix G15 16GB 1TB Display 144Hz", ["Displays & LCD-Panels"])
    assert not laptops.looks_like_laptop_part("ASUS ROG Strix G15 gebraucht", ["Sonstige"])


def test_laptop_search_excludes_ram_spam_and_needs_no_spec():
    import ebay_api
    lap = db.get_watch(db.add_watch(1, "MSI Katana", "MSI Katana", "broken", "", 15), 1)
    phone = db.get_watch(db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "broken", "", 15), 1)
    assert ebay_api._watch_search_kwargs(lap)["exclude_terms"] == "broken passend sodimm arbeitsspeicher speicherriegel"
    assert ebay_api._watch_search_kwargs(phone)["exclude_terms"] == "broken"
    assert not market.watch_requires_spec(lap)          # клас ноутбука бот визначить і без пам'яті в назві
    assert market.watch_requires_spec(phone)


def test_trademark_symbols_in_aspects():
    from laptops import laptop_spec
    aspects = {"grafikprozessor": "GeForce RTX™ 5050", "prozessor": "Intel® Core™ i5-13420H",
               "arbeitsspeichergröße": "16 GB"}
    assert laptop_spec("HP Victus Gaming 15-fa2357ng Gaming Notebook", aspects, query="HP Victus") \
        == "RTX 5050 · i5 13 gen · 16GB"


def test_old_laptop_history_reclassified():
    import db
    import market
    wid = db.add_watch(1, "HP Victus", "HP Victus", "", "", 15, categories=[{"id": "175672", "name": "Notebooks"}])
    db.update_listing_observations(wid, [
        {"item_id": "a", "title": "HP Victus 15 Ryzen 5 RTX 4060 16GB", "spec_group": "16GB+RYZEN5",
         "cond_group": "used", "total_price": 750},
        {"item_id": "b", "title": "HP Victus Gaming 15-fa2357ng Gaming Notebook", "spec_group": "GPU ? · i5 13 gen · 16GB",
         "cond_group": "used", "total_price": 849}])
    db.save_cached_spec("b", "GPU ? · i5 13 gen · 16GB", {"grafikprozessor": "GeForce RTX™ 5050",
                                                         "prozessor": "Intel® Core™ i5-13420H",
                                                         "arbeitsspeichergröße": "16 GB"})
    assert market.normalize_saved_specs() == 2
    with db.get_conn() as conn:
        specs = dict(conn.execute("SELECT item_id, spec_group FROM listing_obs").fetchall())
    assert specs["b"] == "RTX 5050 · i5 13 gen · 16GB" and specs["a"].startswith("RTX 4060")


def test_cpu_generation_from_aspects_when_title_vague():
    from laptops import laptop_spec
    aspects = {"grafikprozessor": "NVIDIA® GeForce RTX™ 3050", "prozessor": "Intel® Core™ i5-12450H",
               "arbeitsspeichergröße": "16 GB"}
    assert laptop_spec("MSI Katana 17 Gaming-Notebook (RTX-30-SERIE) i5", aspects, query="MSI Katana") \
        == "RTX 3050 · i5 12 gen · 16GB"
    assert laptop_spec("MSI Katana Laptop, 17,3\", Windows 11, Intel Core i7, NVIDIA GeForce RTX 5070",
                       query="MSI Katana") == "RTX 5070 · i7"


def test_aspects_read_when_class_incomplete():
    from laptops import wants_aspects, needs_aspects
    assert wants_aspects("RTX 3060")                              # ні процесора, ні пам'яті
    assert wants_aspects("RTX 4060 · i5 · 16GB")                  # процесор без покоління
    assert wants_aspects("RTX 4050 · 32GB+")
    assert not wants_aspects("RTX 4060 · i5 13 gen · 16GB")       # повний клас — запит не потрібен
    assert not wants_aspects("RTX 4060 · Ryzen 7 7000 · 32GB+")
    assert not wants_aspects("M2 · 13\" · 8GB · 256GB")           # MacBook
    assert wants_aspects("GPU ? · i5 13 gen · 16GB")
    assert not needs_aspects("RTX 3060")                          # але знахідки «RTX 3060» не відкладаються


def test_incomplete_laptop_class_reads_aspects(fake_ebay):
    import db
    import market
    w = {"id": db.add_watch(1, "Gigabyte G5", "Gigabyte G5", "", "", 15,
                            categories=[{"id": "175672", "name": "Notebooks"}]), "query": "Gigabyte G5"}
    fake_ebay.aspects = {"g5": {"Prozessor": "Intel® Core™ i5-12500H", "Arbeitsspeichergröße": "16 GB"}}
    items = [{"item_id": "g5", "title": "GIGABYTE G5 Gaming | RTX 3060 6GB | 300Hz", "category_names": []}]
    market._annotate_items(items, max_lookups=10, watch=db.get_watch(w["id"], 1))
    assert items[0]["spec_group"] == "RTX 3060 · i5 12 gen · 16GB"
