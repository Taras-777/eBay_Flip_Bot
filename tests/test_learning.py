"""«🚫 Не той товар»: відхилений лот більше не враховується, бот вивчає слова."""
import asyncio
import time

import db
import learning
import market
import scheduler
from conftest import listing
from test_scheduler import make_app


def _watch_with_listings(titles):
    wid = db.add_watch(1, "PlayStation 5", "PlayStation 5", "broken", "", 15)
    now = int(time.time())
    with db.get_conn() as conn:
        for i, title in enumerate(titles):
            conn.execute(
                """INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price,
                   first_seen, last_seen, status, title, url)
                   VALUES (?, ?, 'used', '825GB', ?, ?, ?, 'active', ?, '')""",
                (wid, f"ok{i}", 450 + i, now, now, title),
            )
    return db.get_watch(wid, 1)


GOOD = [f"Sony PlayStation 5 Disc Edition 825GB Konsole Nr {i}" for i in range(10)]


def test_rejected_item_is_filtered_out():
    w = _watch_with_listings(GOOD)
    items = [{"item_id": "x", "title": "Sony PlayStation 5", "spec_group": "825GB", "aspects": {}},
             {"item_id": "y", "title": "Sony PlayStation 5", "spec_group": "825GB", "aspects": {}}]
    learning.reject_and_learn(w, "x", "Sony PlayStation 3 Slim")
    assert [i["item_id"] for i in market._apply_item_filters(w, items)] == ["y"]


def test_word_learned_after_two_rejections_and_can_be_undone():
    w = _watch_with_listings(GOOD)
    assert learning.reject_and_learn(w, "r1", "Sony Playstation 3 PS3 Slim HEN 3.5.0") == []
    words = learning.reject_and_learn(db.get_watch(w["id"], 1), "r2", "PS3 Super Slim 500GB Sony")
    assert "ps3" in words
    assert "sony" not in words  # «sony» є в правильних лотах
    assert "slim" not in words  # варіант моделі — захищене слово
    assert "ps3" in db.get_watch(w["id"], 1)["exclude"].split()
    assert "broken" in db.get_watch(w["id"], 1)["exclude"].split()  # старі слова лишились

    assert learning.unlearn_word(w["id"], 1, "ps3")
    assert "ps3" not in db.get_watch(w["id"], 1)["exclude"].split()
    # Скасоване слово більше не пропонується
    learning.reject_and_learn(db.get_watch(w["id"], 1), "r3", "PS3 Konsole Sony")
    assert "ps3" not in db.get_watch(w["id"], 1)["exclude"].split()


def test_no_learning_without_enough_good_listings():
    w = _watch_with_listings(GOOD[:3])
    learning.reject_and_learn(w, "r1", "PS3 Slim")
    assert learning.reject_and_learn(db.get_watch(w["id"], 1), "r2", "PS3 Slim") == []


def test_rejected_item_never_becomes_a_deal(fake_ebay):
    consoles = [listing(f"c{i}", "Sony PlayStation 5 Slim 1TB Konsole", 450 + i * 5, created_ago_s=600 + i)
                for i in range(20)]
    fake_ebay.listings = consoles
    wid = db.add_watch(1, "PS5", "PS5", "", "", 15, categories=[{"id": "139971", "name": "Konsolen"}])
    app = make_app()
    asyncio.run(scheduler.check_all_watches(app))

    learning.reject_and_learn(db.get_watch(wid, 1), "cheap", "Sony PlayStation 5 Slim 1TB Konsole")
    fake_ebay.listings = [listing("cheap", "Sony PlayStation 5 Slim 1TB Konsole", 250)] + consoles
    app.bot.messages.clear()
    asyncio.run(scheduler.check_all_watches(app))
    assert not [m for m in app.bot.messages if "Вигідна пропозиція" in m]


def test_hidden_item_is_filtered_but_words_not_learned():
    w = _watch_with_listings(GOOD)
    learning.hide_item(w, "h1", "Sony PlayStation 5 Displaybruch Riss")
    learning.hide_item(w, "h2", "Sony PlayStation 5 Riss am Gehäuse")
    items = [{"item_id": "h1", "title": "x", "spec_group": "825GB", "aspects": {}},
             {"item_id": "ok", "title": "x", "spec_group": "825GB", "aspects": {}}]
    assert [i["item_id"] for i in market._apply_item_filters(w, items)] == ["ok"]
    # «Сховані» назви не використовуються для навчання слів
    assert db.get_rejected_titles(w["id"]) == []
    assert "riss" not in db.get_watch(w["id"], 1)["exclude"].split()


def test_old_rejected_items_table_gets_reason_column(tmp_path, monkeypatch):
    import sqlite3
    import settings
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE rejected_items (watch_id INTEGER NOT NULL, item_id TEXT NOT NULL, "
                 "title TEXT, created_at INTEGER NOT NULL, PRIMARY KEY (watch_id, item_id))")
    conn.execute("INSERT INTO rejected_items VALUES (1, 'a', 'PS3 Slim', 0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "DB_PATH", path)
    db.init_db()
    assert db.get_rejected_titles(1) == ["PS3 Slim"]  # старі записи — «не той товар»