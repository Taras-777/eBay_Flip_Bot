"""База даних: пакетна робота з "вже бачили", очищення, збереження налаштувань."""
import time

import db


def test_bulk_seen_items_keeps_notified_price():
    db.bulk_upsert_seen_items(1, [("x", 300, 300)])       # сповіщено за 300
    db.bulk_upsert_seen_items(1, [("x", 320, None)])      # ціна зросла, сповіщення не було
    row = db.get_seen_items(1, ["x"])["x"]
    assert row["last_price"] == 320 and row["last_notified_price"] == 300


def test_cleanup_uses_last_seen():
    old = int(time.time()) - 60 * 86400
    with db.get_conn() as conn:
        conn.execute("INSERT INTO seen_items (item_id, watch_id, first_seen_at, last_seen_at) VALUES ('old', 1, ?, ?)", (old, old))
        conn.execute("INSERT INTO seen_items (item_id, watch_id, first_seen_at, last_seen_at) VALUES ('active', 1, ?, ?)", (old, int(time.time())))
    db.cleanup_old_seen_items()
    assert set(db.get_seen_items(1, ["old", "active"])) == {"active"}


def test_bought_deals_survive_cleanup():
    wid = db.add_watch(1, "PS5", "PS5", "", "", 20)
    bought = db.add_deal(wid, "a", "PS5", 300, "EUR", 450, 30, "u", False)
    db.set_deal_status(bought, "bought")
    old = db.add_deal(wid, "b", "PS5", 300, "EUR", 450, 30, "u", False)
    with db.get_conn() as conn:
        conn.execute("UPDATE deals SET created_at = ?", (int(time.time()) - 400 * 86400,))
    db.cleanup_old_seen_items()
    assert db.get_deal_stats(1) == {"bought": 1}
    assert db.get_deal_owner_chat_id(bought) == 1 and db.get_deal_owner_chat_id(old) is None


def test_multiple_categories_roundtrip():
    wid = db.add_watch(1, "PS5", "PS5", "", "", 20, categories=[{"id": "A", "name": "Konsolen"}, {"id": "B", "name": "PS5"}])
    w = db.get_watch(wid, 1)
    assert db.watch_category_ids(w) == ["A", "B"] and w["category_id"] == "A"


def test_legacy_single_required_aspect():
    assert db.get_required_aspects({"required_aspect": "Netzwerk"}) == ["Netzwerk"]
    assert db.get_required_aspects({"required_aspect": '["A", "B"]'}) == ["A", "B"]


def test_threshold_suggestion_from_seed_hints():
    pct, _ = db.find_threshold_suggestion("PS5 Slim")
    assert pct == 20