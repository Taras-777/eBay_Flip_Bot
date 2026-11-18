"""
«📉 Знизили ціну»: перша й поточна ціна оголошень.
"""

import json
import time

from core import settings


# ---------- «📉 Знизили ціну» ----------

PRICE_STEP_EUR = 0.5   # дрібні коливання (копійки курсу валют) — не зниження
TITLE_SAME_MIN = 0.5   # назва «та сама», якщо щонайменше половина слів коротшої назви є в іншій


def _spec_parts(spec):
    """«Slim · 500GB» → {"Slim", "500GB"}; невідома / неповна конфігурація — None (не порівнюємо)."""
    if not spec or spec in ("unspecified", "*") or spec.startswith("GPU ?"):
        return None
    return {p.strip() for p in spec.split(" · ") if p.strip()}


def listing_relisted(old_title, old_spec, new_title, new_spec):
    """Продавець переробив те саме оголошення під інший товар: змінилась конфігурація (1TB → 825GB,
    M3 Pro → M3) або назва майже повністю інша. Уточнення («RTX 4060» → «RTX 4060 · i7 13 gen»), інший порядок
    («500GB · Slim» → «Slim · 500GB») і дописані слова («Top Zustand», «OVP») — не переробка."""
    from analysis.textparse import _search_tokens   # тут: textparse — шар аналізу, база від нього не залежить
    a, b = _spec_parts(old_spec), _spec_parts(new_spec)
    if a and b and not (a <= b or b <= a):
        return True
    old_words, new_words = _search_tokens(old_title or ""), _search_tokens(new_title or "")
    if len(old_words) < 3 or len(new_words) < 3:
        return False   # надто коротка назва — порівнювати ненадійно
    return len(old_words & new_words) / min(len(old_words), len(new_words)) < TITLE_SAME_MIN


def track_prices(watch_id, items):
    """Запам'ятовує ціни оголошень: першу, поточну, найнижчу й час останнього зниження.
    Варіантні оголошення (кілька кольорів/обсягів з різними цінами) не відстежуємо —
    їхня «ціна» стрибає між варіантами."""
    now = int(time.time())
    rows = [(watch_id, it["item_id"], it.get("title"), it.get("url"), it.get("cond_group"),
             it.get("spec_group", "unspecified"), it.get("created_at"), int(bool(it.get("has_best_offer"))),
             it["total_price"], now, it["total_price"], it["total_price"], now, it.get("title"),
             it.get("spec_group", "unspecified"))
            for it in items
            if it.get("item_id") and it.get("total_price") and not it.get("is_variation")]
    if not rows:
        return 0
    with get_conn() as conn:
        # Оголошення переробили під інший товар — стара «перша ціна» вже не про нього: рахуємо з нуля
        known = {r["item_id"]: r for r in conn.execute(
            f"SELECT item_id, first_title, first_spec FROM price_track WHERE watch_id = ? AND item_id IN "
            f"({','.join('?' * len(rows))})", [watch_id] + [r[1] for r in rows]).fetchall()}
        for row in rows:
            old = known.get(row[1])
            if old and listing_relisted(old["first_title"], old["first_spec"], row[2], row[5]):
                conn.execute(
                    """UPDATE price_track SET first_price = ?, first_seen = ?, min_price = ?, price = ?,
                       first_title = ?, first_spec = ?, price_changed_at = NULL, seen_at = NULL,
                       dismissed_price = NULL WHERE watch_id = ? AND item_id = ?""",
                    (row[8], now, row[8], row[8], row[2], row[5], watch_id, row[1]))
        conn.executemany(
            f"""INSERT INTO price_track (watch_id, item_id, title, url, cond_group, spec_group, listed_at,
                                        has_best_offer, first_price, first_seen, price, min_price, last_seen,
                                        first_title, first_spec)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(watch_id, item_id) DO UPDATE SET
                 title = excluded.title, url = excluded.url, cond_group = excluded.cond_group,
                 spec_group = excluded.spec_group, listed_at = COALESCE(price_track.listed_at, excluded.listed_at),
                 has_best_offer = excluded.has_best_offer, last_seen = excluded.last_seen,
                 price_changed_at = CASE WHEN excluded.price < price_track.price - {PRICE_STEP_EUR}
                                         THEN excluded.last_seen ELSE price_track.price_changed_at END,
                 seen_at = CASE WHEN excluded.price < price_track.price - {PRICE_STEP_EUR}
                                THEN NULL ELSE price_track.seen_at END,
                 min_price = MIN(price_track.min_price, excluded.price),
                 first_title = COALESCE(price_track.first_title, excluded.first_title),
                 first_spec = COALESCE(price_track.first_spec, excluded.first_spec),
                 price = excluded.price""",
            rows,
        )
    return len(rows)


def get_markdown_candidates(chat_id, min_drop_pct, min_days, fresh_seconds):
    """Оголошення користувача, які висять ≥ min_days і подешевшали на ≥ min_drop_pct
    від першої побаченої ціни (без відхилених і тих, що вже в «🔥 Вигідні пропозиції»)."""
    now = int(time.time())
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT t.*, w.label AS watch_label, w.chat_id FROM price_track t
               JOIN watches w ON w.id = t.watch_id
               WHERE w.chat_id = ? AND w.active = 1
                 AND t.last_seen >= ? AND t.price <= t.first_price * (1 - ? / 100.0)
                 AND COALESCE(t.listed_at, t.first_seen) <= ?
                 AND (t.dismissed_price IS NULL OR t.price < t.dismissed_price - 0.01)
                 AND NOT EXISTS (SELECT 1 FROM rejected_items r
                                 WHERE r.watch_id = t.watch_id AND r.item_id = t.item_id)
                 AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.watch_id = t.watch_id
                                 AND d.item_id = t.item_id AND d.status IN ('new', 'bought'))""",
            (chat_id, now - fresh_seconds, min_drop_pct, now - min_days * 86400),
        ).fetchall()
    return [dict(r) for r in rows]


def dismiss_tracks(rows):
    """«🧹 Очистити список»: оголошення зникають з «📉 Знизили ціну» і повертаються, лише якщо
    продавець знизить ціну ще раз. rows: [(watch_id, item_id, поточна ціна)]."""
    with get_conn() as conn:
        conn.executemany("UPDATE price_track SET dismissed_price = ? WHERE watch_id = ? AND item_id = ?",
                         [(price, wid, item) for wid, item, price in rows])


def get_track_row(watch_id, item_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM price_track WHERE watch_id = ? AND item_id = ?",
                           (watch_id, item_id)).fetchone()
    return dict(row) if row else None


def mark_track_seen(keys):
    """keys: [(watch_id, item_id)] — користувач побачив ці знижки."""
    with get_conn() as conn:
        conn.executemany("UPDATE price_track SET seen_at = ? WHERE watch_id = ? AND item_id = ? AND seen_at IS NULL",
                         [(int(time.time()), w, i) for w, i in keys])


def mark_track_gone(watch_id, item_id):
    """Оголошення продано чи знято — зі «📉» зникає (якщо з'явиться знову — повернеться)."""
    with get_conn() as conn:
        conn.execute("UPDATE price_track SET last_seen = 0 WHERE watch_id = ? AND item_id = ?", (watch_id, item_id))


def get_tracks_to_refresh(limit, stale_seconds, min_age_days):
    """«📉»: відстежувані оголошення, які висять щонайменше min_age_days (лише такі можуть потрапити
    в «Знизили ціну») і давно не перевірялись, — найдавніше перевірені першими."""
    now = int(time.time())
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT t.* FROM price_track t JOIN watches w ON w.id = t.watch_id
               WHERE w.active = 1 AND t.last_seen > 0 AND t.last_seen < ?
                 AND COALESCE(t.listed_at, t.first_seen) <= ?
               ORDER BY t.last_seen LIMIT ?""",
            (now - stale_seconds, now - min_age_days * 86400, limit)).fetchall()]


def touch_track(watch_id, item_id):
    with get_conn() as conn:
        conn.execute("UPDATE price_track SET last_seen = ? WHERE watch_id = ? AND item_id = ?",
                     (int(time.time()), watch_id, item_id))


def get_drop_pct(chat_id):
    try:
        return float(get_meta(f"drop_pct:{chat_id}") or settings.PRICE_DROP_PCT)
    except ValueError:
        return float(settings.PRICE_DROP_PCT)


def set_drop_pct(chat_id, value):
    set_meta(f"drop_pct:{chat_id}", value)


def get_markdown_mode(chat_id):
    """Які знижки показувати в «📉»: deal / haggle / all (див. settings.MARKDOWN_MODES)."""
    mode = get_meta(f"mdmode:{chat_id}")
    return mode if mode in settings.MARKDOWN_MODES else settings.MARKDOWN_MODE_DEFAULT


def set_markdown_mode(chat_id, mode):
    if mode in settings.MARKDOWN_MODES:
        set_meta(f"mdmode:{chat_id}", mode)


def get_hidden_candidates(chat_id):
    """«💡 Що перепродавати»: назви товарів, позначених «🙈 Не цікавить»."""
    try:
        return list(json.loads(get_meta(f"disc_hidden:{chat_id}") or "[]"))
    except ValueError:
        return []


def set_hidden_candidates(chat_id, names):
    set_meta(f"disc_hidden:{chat_id}", json.dumps(sorted(set(names)), ensure_ascii=False))


def get_sold_only(chat_id):
    """Знахідки «🔥» лише з ціною «Продати» за реальними продажами (а не за поточними оголошеннями)."""
    return get_meta(f"sold_only:{chat_id}") == "1"


def set_sold_only(chat_id, value):
    set_meta(f"sold_only:{chat_id}", "1" if value else "0")


# SQL-умова для «🔥»: якщо користувач увімкнув «лише за продажами» — лише такі пропозиції
SOLD_ONLY_SQL = ("(COALESCE((SELECT m2.value FROM meta m2 WHERE m2.key = 'sold_only:' || w.chat_id), '0') != '1' "
                 "OR COALESCE(d.sale_source, '') LIKE '%продан%')")


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn, get_meta, set_meta  # noqa: E402
