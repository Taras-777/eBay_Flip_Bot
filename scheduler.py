"""
Фонові перевірки: цикл перевірки товарів, пошук вигідних лотів,
обробник помилок, запуск і зупинка планувальника.
"""

import asyncio
import config
import time
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

from settings import (
    CHECK_INTERVAL_MINUTES,
    DEAL_SCAN_LIMIT,
    ERROR_NOTICE_INTERVAL,
    LOW_BUDGET_INTERVAL_MINUTES,
    MARKET_REFRESH_MINUTES,
    MAX_SPEC_LOOKUPS_PER_DEAL_SCAN,
    SEARCH_RESERVE,
    WATCH_CONCURRENCY,
    log,
)
from db import (
    add_deal,
    bulk_upsert_seen_items,
    cleanup_old_listing_obs,
    cleanup_old_seen_items,
    get_market_stats,
    get_seen_items,
    list_watches,
    watch_category_ids,
)
from ebay_api import _watch_search_kwargs, browse_budget_left, fetch_browse_rate_limit, search_in_categories
from market import (
    _annotate_items,
    _apply_item_filters,
    _recalculate_watch_medians,
    _stat_for_item,
    max_buy_price,
)
from panel import repost_panel
from notifications import _notify_median_error, _notify_median_ready, _send_grouped_deals, _send_single_deal


async def check_all_watches(app: Application):
    watches = list_watches(active_only=True)
    semaphore = asyncio.Semaphore(WATCH_CONCURRENCY)

    async def check(w):
        async with semaphore:
            try:
                await check_one_watch(app, w)
            except Exception as e:
                log.exception("Помилка при перевірці watch #%s: %s", w["id"], e)
                await _notify_median_error(app, w, e)

    await asyncio.gather(*(check(w) for w in watches))

    # Сповіщення з'явились над панеллю — переносимо панель униз, по разу на чат
    chats = app.bot_data.pop("chats_to_repost_panel", set())
    for chat_id in chats:
        await repost_panel(app, chat_id)


async def check_one_watch(app: Application, w: dict):
    rows = get_market_stats(w["id"])
    market_is_stale = (
        not rows
        or min(r["updated_at"] for r in rows) < time.time() - MARKET_REFRESH_MINUTES * 60
    )
    if market_is_stale:
        items, stats, newly = await _recalculate_watch_medians(w)
        if newly:
            await _notify_median_ready(app, w, [stats[key] for key in newly])
    else:
        stats = {(r["cond_group"], r["spec_group"]): r for r in rows}
        def _deal_scan():
            found = search_in_categories(
                watch_category_ids(w), limit=DEAL_SCAN_LIMIT, fresh=True, **_watch_search_kwargs(w),
            )
            _annotate_items(found, max_lookups=MAX_SPEC_LOOKUPS_PER_DEAL_SCAN, watch=w)
            return _apply_item_filters(w, found)

        items = await asyncio.to_thread(_deal_scan)

    if not items or not stats:
        return

    new_deals = []
    seen_map = get_seen_items(w["id"], [it["item_id"] for it in items])
    seen_updates = []  # записуються одним пакетом наприкінці
    for it in items:
        seen = seen_map.get(it["item_id"])
        is_new = seen is None
        price_dropped = (not is_new) and seen["last_price"] is not None and it["effective_price"] < seen["last_price"] - 0.01

        if not is_new and not price_dropped:
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue

        stat = _stat_for_item(stats, it)
        if stat is None:
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue  # для цього стану ще немає надійної статистики

        sale_price = stat["sale_price"] or stat["median_price"]
        # Вигідно, якщо ціна купівлі (для Best Offer — з урахуванням торгу)
        # не вища за максимальну, що ще дає бажаний прибуток при перепродажі
        if it["effective_price"] > max_buy_price(sale_price, w["discount_threshold_pct"]):
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue
        discount_pct = (sale_price - it["total_price"]) / sale_price * 100

        already_notified_price = seen["last_notified_price"] if seen else None
        if already_notified_price is not None and it["effective_price"] >= already_notified_price - 0.01:
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue

        it["price_dropped"] = price_dropped and not is_new
        deal_id = add_deal(
            watch_id=w["id"],
            item_id=it["item_id"],
            title=it["title"],
            total_price=it["total_price"],
            currency=it["currency"],
            median_price=sale_price,
            discount_pct=discount_pct,
            url=it["url"],
            suspicious=it["suspicious"],
            has_best_offer=it["has_best_offer"],
        )
        seen_updates.append((it["item_id"], it["effective_price"], it["effective_price"]))
        new_deals.append((deal_id, it, stat))

    bulk_upsert_seen_items(w["id"], seen_updates)

    if not new_deals:
        return

    MAX_INDIVIDUAL_DEALS = 5
    if len(new_deals) <= MAX_INDIVIDUAL_DEALS:
        for deal_id, it, stat in new_deals:
            await _send_single_deal(app, w, deal_id, it, stat)
    else:
        await _send_grouped_deals(app, w, new_deals)


_error_notice = {"last": 0.0}


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Необроблені помилки: у лог повністю, власнику — коротке повідомлення.
    Тимчасові мережеві збої Telegram лише логуються."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Тимчасова мережева помилка Telegram: %s", err)
        return
    log.error("Помилка під час обробки оновлення", exc_info=err)
    if config.OWNER_TELEGRAM_ID and time.time() - _error_notice["last"] > ERROR_NOTICE_INTERVAL:
        _error_notice["last"] = time.time()
        try:
            await context.bot.send_message(
                chat_id=config.OWNER_TELEGRAM_ID,
                text=f"⚠️ Помилка в боті: {type(err).__name__}: {str(err)[:300]}\n"
                     "Повні деталі — в логах (journalctl -u ebay-bot).",
            )
        except Exception:
            pass


async def scheduler_loop(app: Application):
    try:
        await asyncio.sleep(5)
        last_cleanup_at = 0
        while True:
            try:
                if browse_budget_left() > 0:
                    await check_all_watches(app)
                else:
                    log.warning("Добовий бюджет запитів eBay вичерпано — пропускаю перевірку")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Будь-яка непередбачена помилка не має зупиняти фонові перевірки назавжди
                log.exception("Помилка циклу перевірки — продовжую з наступного разу")

            try:
                await asyncio.to_thread(fetch_browse_rate_limit)
            except Exception as e:
                log.debug("Не вдалося отримати ліміти eBay API: %s", e)

            # Раз на добу прибираємо застарілі записи seen_items
            now = time.time()
            if now - last_cleanup_at > 86400:
                try:
                    removed = await asyncio.to_thread(cleanup_old_seen_items)
                    removed_obs = await asyncio.to_thread(cleanup_old_listing_obs)
                    if removed or removed_obs:
                        log.info("Очищено застарілих записів: seen_items %s, listing_obs %s",
                                 removed, removed_obs)
                except Exception as e:
                    log.warning("Не вдалося очистити seen_items: %s", e)
                last_cleanup_at = now

            # Бюджет на межі — перевіряємо рідше, щоб дотягнути до скидання ліміту
            left = browse_budget_left()
            interval = CHECK_INTERVAL_MINUTES if left >= SEARCH_RESERVE else LOW_BUDGET_INTERVAL_MINUTES
            if interval != CHECK_INTERVAL_MINUTES:
                log.info("Бюджет eBay майже вичерпано (лишилось %s) — наступна перевірка через %s хв",
                         left, interval)
            await asyncio.sleep(interval * 60)
    except asyncio.CancelledError:
        log.info("Фоновий планувальник зупинено")
        raise


async def post_init(app: Application):
    app.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(app))
    # Список команд, що випадає над клавіатурою в Telegram
    await app.bot.set_my_commands([
        ("start", "Головне меню та довідка"),
        ("menu", "Показати меню"),
        ("addwatch", "➕ Додати товар для відстеження"),
        ("list", "📦 Мої відстеження"),
        ("stats", "📊 Моя статистика"),
        ("remove", "Вимкнути відстеження за id"),
        ("setthreshold", "Змінити бажаний прибуток"),
        ("setminprice", "Змінити мінімальну ціну"),
        ("cancel", "Скасувати поточну дію"),
    ])


async def post_shutdown(app: Application):
    task = app.bot_data.pop("scheduler_task", None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)