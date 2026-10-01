"""iPhone ділиться лише за об'ємом пам'яті."""
import db
import market
from textparse import extract_spec_key, normalize_spec, spec_key_from_aspects


def test_iphone_only_storage():
    assert extract_spec_key("Apple iPhone 15 Pro 128GB 8GB RAM Titan") == "128GB"
    assert extract_spec_key("iPhone 15 Pro 1 TB Schwarz") == "1TB"
    assert extract_spec_key("iPhone 15 Pro 128GB 256GB 512GB 1TB Alle Farben") == "unspecified"   # варіанти
    assert extract_spec_key("iPhone 15 Pro 8GB RAM") == "unspecified"                           # лише ОЗП
    assert spec_key_from_aspects("iPhone 15 Pro", {"speicherkapazität": "256 GB", "arbeitsspeicher": "8 GB"}) == "256GB"
    assert extract_spec_key("Samsung Galaxy S24 8GB 256GB") == "256GB+8GB"                     # інші — як було
    assert normalize_spec("Samsung", "256GB+8GB") == "256GB+8GB"


def test_saved_history_normalized():
    wid = db.add_watch(1, "iPhone 15 Pro", "iPhone 15 Pro", "", "", 15)
    rows = [("a", "iPhone 15 Pro 256GB 8GB", "256GB+8GB"), ("b", "iPhone 15 Pro 128/256/512GB", "128GB+256GB+512GB"),
            ("c", "iPhone 15 Pro 128GB", "128GB"), ("d", "Galaxy S24 8GB 256GB", "256GB+8GB")]
    db.update_listing_observations(wid, [{"item_id": i, "title": t, "spec_group": s, "cond_group": "used",
                                          "total_price": 500} for i, t, s in rows])
    db.upsert_market_stats(wid, "used", "*", 500, 10, sale_price=480, sale_source="x")
    assert market.normalize_saved_specs() == 2
    with db.get_conn() as conn:
        specs = dict(conn.execute("SELECT item_id, spec_group FROM listing_obs").fetchall())
    assert specs == {"a": "256GB", "b": "unspecified", "c": "128GB", "d": "256GB+8GB"}
    assert db.get_market_stats(wid)[0]["updated_at"] == 0          # перерахується найближчим циклом
    assert market.normalize_saved_specs() == 0


def test_console_storage_only():
    assert extract_spec_key("Sony PlayStation 4 Slim 500GB + 2 Controller + FIFA (50GB)") == "500GB"
    assert extract_spec_key("PS4 Pro 1000GB schwarz") == "1TB"
    assert extract_spec_key("PS4 Pro 1TB (500GB auf 1TB aufgerüstet)") == "unspecified"   # два об'єми
    assert extract_spec_key("PS4 Konsole + 5GB Spiel") == "unspecified"
    assert extract_spec_key("PlayStation 5 Slim 1TB") == "1TB"
    assert extract_spec_key("Xbox One 500GB") == "500GB"                                  # інші — як було


def test_other_console_model_dropped():
    from textparse import console_foreign
    assert console_foreign("Sony PlayStation 5 825GB Disc", "PlayStation 4")
    assert console_foreign("PS3 Slim 320GB", "PlayStation 4")
    assert not console_foreign("PS4 Slim 500GB", "PlayStation 4")
    assert not console_foreign("PS5 825GB", "PlayStation 5")
    w = {"id": db.add_watch(1, "PlayStation 4", "PlayStation 4", "", "", 15), "query": "PlayStation 4"}
    items = [{"item_id": "a", "title": "PS4 Slim 500GB", "category_names": []},
             {"item_id": "b", "title": "PlayStation 5 825GB", "category_names": []}]
    assert [it["item_id"] for it in market._apply_item_filters(w, items)] == ["a"]

    db.update_listing_observations(w["id"], [
        {"item_id": "x", "title": "PS4 Pro 1000GB", "spec_group": "1000GB", "cond_group": "used", "total_price": 200},
        {"item_id": "y", "title": "PS5 Disc 825GB", "spec_group": "825GB", "cond_group": "used", "total_price": 400}])
    assert market.normalize_saved_specs() == 2
    with db.get_conn() as conn:
        rows = dict(conn.execute("SELECT item_id, spec_group FROM listing_obs").fetchall())
    assert rows == {"x": "1TB"}                                        # PS5 прибрано з історії PS4


def test_console_unknown_storage_kept():
    from textparse import spec_key_from_aspects
    assert spec_key_from_aspects("Sony PlayStation 4 Heimkonsole Schwarz", {"speicherkapazität": "64 GB"}) == "unspecified"
    w = {"id": db.add_watch(1, "PlayStation 4", "PlayStation 4", "", "", 15), "query": "PlayStation 4"}
    assert market.watch_requires_spec(w) is False
    items = [{"item_id": "a", "title": "Sony PlayStation 4 Heimkonsole Schwarz", "spec_group": "unspecified",
              "category_names": [], "aspects": {"speicherkapazität": "64 GB"}}]
    assert [it["item_id"] for it in market._apply_item_filters(w, items)] == ["a"]    # не випадає
    w5 = {"id": db.add_watch(1, "PS5", "PlayStation 5", "", "", 15), "query": "PlayStation 5"}
    assert market.watch_requires_spec(w5) is False
    wx = {"id": db.add_watch(1, "Xbox", "Xbox Series X", "", "", 15), "query": "Xbox Series X"}
    assert market.watch_requires_spec(wx) is True                                     # інші консолі — як було
