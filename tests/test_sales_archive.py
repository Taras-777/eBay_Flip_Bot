"""«🗃 Архів продажів»: старі продажі не губляться, «💶 Оцінити» бере їх як запасний варіант;
🙈 приховані й оцінені товари теж збирають продажі."""
import time

from core import db
from analysis import appraise, discovery
from tests.test_appraise import MAC, TITLE, _mac_watch
from tests.test_discovery import SWITCH, one_candidate  # noqa: F401  (фікстура)
from tests.test_sales import _sale

DAY = 86400


def _age(watch_id, item_id, days):
    t = int(time.time()) - days * DAY
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET gone_at = ?, last_seen = ?, created_at = ?, first_seen = ? "
                     "WHERE watch_id = ? AND item_id = ?", (t, t, t - 3 * DAY, t - 3 * DAY, watch_id, item_id))


def test_cleanup_moves_old_sales_to_archive_and_drops_unsold():
    wid = _mac_watch()
    _sale(wid, "old_sold", 1400, spec=MAC)
    _sale(wid, "old_unsold", 900, spec=MAC)
    _sale(wid, "fresh", 1450, spec=MAC)
    for item in ("old_sold", "old_unsold"):
        _age(wid, item, 120)
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET sold_check = 'unsold' WHERE item_id = 'old_unsold'")
        conn.execute("UPDATE listing_obs SET sold_check = 'sold' WHERE item_id = 'old_sold'")
    assert db.cleanup_old_listing_obs() == 2
    rows = db.get_archived_sales(["MacBook Pro M3"])
    assert [(r["item_id"], r["price"], r["confirmed"], r["spec_group"]) for r in rows] == [("old_sold", 1400, 1, MAC)]
    assert {r["item_id"] for r in db.get_sold_listings(wid)} == {"fresh"}     # свіжі лишились у базі
    assert db.cleanup_old_listing_obs() == 0 and len(db.get_archived_sales(["MacBook Pro M3"])) == 1


def test_deleting_watch_keeps_its_sales():
    arch = _mac_watch(chat_id=1)
    _sale(arch, "a1", 1400, spec=MAC)
    db.archive_watch(arch, 1)
    assert db.delete_archived_watch(arch, 1)
    assert [r["item_id"] for r in db.get_archived_sales(["MacBook Pro M3"])] == ["a1"]


def test_estimate_adds_older_sales_when_recent_are_few():
    wid = _mac_watch()
    _sale(wid, "new1", 1300, spec=MAC)
    _sale(wid, "new2", 1320, spec=MAC)
    for i, p in enumerate((1500, 1520, 1480)):        # 100 днів тому — уже в архіві
        _sale(wid, f"old{i}", p, spec=MAC)
        _age(wid, f"old{i}", 100)
    db.cleanup_old_listing_obs()
    est = appraise.estimate(TITLE, MAC, True, live=False)
    assert est["source"] == "sold_old" and est["sold_count"] == 5 and est["old_count"] == 3
    from ui import screen_appraise
    text = screen_appraise.estimate_text({"title": TITLE, "spec": MAC, "estimate": est})
    assert "2–6 місяців тому" in text and "ціни могли змінитися" in text


def test_too_old_sales_are_ignored():
    wid = _mac_watch()
    for i, p in enumerate((1500, 1520, 1480, 1490, 1510)):
        _sale(wid, f"old{i}", p, spec=MAC)
        _age(wid, f"old{i}", 200)                     # старше за APPRAISE_OLD_DAYS
    db.cleanup_old_listing_obs()
    assert len(db.get_archived_sales(["MacBook Pro M3"])) == 5     # в архіві зберігаються
    assert appraise.estimate(TITLE, MAC, True, live=False)["source"] is None


def test_unknown_product_is_remembered_and_scanned(fake_ebay, monkeypatch):
    monkeypatch.setattr(discovery, "CANDIDATES", [])
    monkeypatch.setattr(discovery, "browse_budget_left", lambda: 4000)
    monkeypatch.setattr(appraise, "live_listings", lambda query, laptop: [])
    res = appraise.appraise(1, "Nintendo Switch OLED weiß")
    assert res["collecting"]
    assert [p["query"] for p in db.list_appraised_products()] == ["Nintendo Switch OLED weiß"]
    fake_ebay.listings = SWITCH
    assert discovery.scan_appraised() == 1
    assert discovery.scan_appraised() == 0            # раз на добу
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM discovery_obs WHERE candidate = ?",
                         (db.APPRAISED_PREFIX + "Nintendo Switch OLED weiß",)).fetchone()["c"]
    assert n > 0
    # наступна оцінка бере продажі цього джерела
    _, candidates = appraise.matching_sources("Nintendo Switch OLED weiß 64GB")
    assert (db.APPRAISED_PREFIX + "Nintendo Switch OLED weiß", "Nintendo Switch OLED weiß") in candidates


def test_tracked_product_is_not_remembered():
    _mac_watch(chat_id=1)
    watches, candidates = appraise.matching_sources(TITLE)
    assert not appraise.remember_appraised(TITLE, "MacBook Pro M3", watches, candidates, 1400)
    assert db.list_appraised_products() == []


def test_hidden_candidate_scanned_once_a_day(fake_ebay, one_candidate, monkeypatch):  # noqa: F811
    import config
    monkeypatch.setattr(config, "OWNER_TELEGRAM_ID", 1, raising=False)
    db.set_hidden_candidates(1, ["Nintendo Switch OLED"])
    fake_ebay.listings = SWITCH
    calls = []
    real = discovery.analyze_candidate
    monkeypatch.setattr(discovery, "analyze_candidate", lambda *a: calls.append(a[1]) or real(*a))
    assert discovery.run_discovery(force=True) == 0   # у звичайному аналізі — пропущено
    assert calls == ["Nintendo Switch OLED"]           # але статистика — зібрана
    discovery.run_discovery(force=True)
    assert len(calls) == 1                             # наступного разу — лише через добу
    assert discovery.top_recommendations(1) == []      # у списку «💡» не з'являється


def test_discovery_sales_kept_90_days_then_archived(fake_ebay, one_candidate):  # noqa: F811
    fake_ebay.listings = SWITCH
    discovery.run_discovery(force=True)
    fake_ebay.listings = [it for it in SWITCH if it["itemId"] not in ("x10", "x11")]
    discovery.run_discovery(force=True)
    with db.get_conn() as conn:
        conn.execute("UPDATE discovery_obs SET last_seen = last_seen - 40 * 86400, gone_at = gone_at - 40 * 86400")
    discovery.run_discovery(force=True)
    assert len(db.get_discovery_sold("Nintendo Switch OLED", 90)) == 2        # 40 днів — ще в базі
    with db.get_conn() as conn:
        conn.execute("UPDATE discovery_obs SET last_seen = last_seen - 60 * 86400, gone_at = gone_at - 60 * 86400 "
                     "WHERE status = 'gone'")
    discovery.archive_old_discovery()
    assert db.get_discovery_sold("Nintendo Switch OLED", 365) == []
    assert {r["item_id"] for r in db.get_archived_sales(["Nintendo Switch OLED"])} == {"x10", "x11"}
