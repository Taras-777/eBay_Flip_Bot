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
