"""«📊 Статистика»: активність моїх товарів і «💡 Що перепродавати»."""
import asyncio
import time
from datetime import datetime, timedelta

import db
import screen_stats
from settings import LOCAL_TZ
from test_sales import _screen

DAY = 86400


def _obs(wid, item, first_ago, gone_ago=None, sold="sold"):
    db.update_listing_observations(wid, [{"item_id": item, "cond_group": "used", "spec_group": "1TB",
                                          "total_price": 300}])
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute("UPDATE listing_obs SET first_seen = ? WHERE item_id = ?", (now - first_ago, item))
        if gone_ago is not None:
            status = "gone" if sold == "sold" else "ended"     # знятий без продажу — «ended», як після перевірки
            conn.execute("UPDATE listing_obs SET status = ?, gone_at = ?, sold_check = ? WHERE item_id = ?",
                         (status, now - gone_ago, sold, item))


def test_stats_counts_and_trend(monkeypatch):
    ps4 = db.add_watch(1, "PS4", "PS4", "", "", 15)
    quiet = db.add_watch(1, "MacBook", "MacBook", "", "", 15)
    _obs(ps4, "a", 3 * DAY, gone_ago=2 * DAY)        # продано давно
    _obs(ps4, "b", 2 * DAY, gone_ago=3600)           # продано сьогодні
    _obs(ps4, "c", 3600)                              # нове
    _obs(ps4, "d", 5 * DAY, gone_ago=3600, sold="unsold")   # знято без продажу — не продаж
    _obs(quiet, "m", 10 * DAY)
    today = datetime.now(LOCAL_TZ).date()
    with db.get_conn() as conn:
        for day, price in ((today - timedelta(days=7), 100), (today, 90)):
            conn.execute("INSERT INTO price_history (watch_id, cond_group, spec_group, day, median_price) "
                         "VALUES (?, 'used', '*', ?, ?)", (ps4, day.isoformat(), price))
    text, pages, page = screen_stats.stats_text(1)
    assert "<b>PS4</b>\n   оголошень 4 (🆕 +1) · продано 2 (🆕 +1) · 📉 -10%" in text
    assert "<b>MacBook</b>\n   оголошень 1 · продано 0" in text
    assert text.index("PS4") < text.index("MacBook")          # активніші вгорі


def test_stats_discovery_section_and_pages(monkeypatch):
    names = [c[1] for c in screen_stats.CANDIDATES][:20]
    now = int(time.time())
    with db.get_conn() as conn:
        for i, name in enumerate(names):
            conn.execute("INSERT INTO discovery_obs (candidate, item_id, price, first_seen, last_seen, status) "
                         "VALUES (?, ?, 100, ?, ?, 'active')", (name, f"x{i}", now - 3600 * (i + 1), now))
    db.set_hidden_candidates(1, [names[0]])
    text, pages, page = screen_stats.stats_text(1)
    assert pages == 2 and "💡 <b>Що перепродавати</b> · сторінка 1 з 2" in text
    assert f"<b>{names[0]}</b>" not in text                     # прихований не показуємо
    handlers, shown, press = _screen(monkeypatch)
    upd, ctx = press("stats:1")
    asyncio.run(handlers.cmd_stats(upd, ctx))
    assert "сторінка 2 з 2" in shown[-1][0] and "📦 <b>Мої товари</b>" not in shown[-1][0]
    assert "◀️ Попередні" in shown[-1][1]
