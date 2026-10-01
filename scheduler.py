"""
Фонові перевірки: цикл перевірки товарів, пошук вигідних лотів,
обробник помилок, запуск і зупинка планувальника.
"""

import asyncio
import config
import time
from concurrent.futures import ThreadPoolExecutor
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

from settings import (
    CHECK_INTERVAL_MINUTES,
    DEAL_SCAN_LIMIT,
    ERROR_NOTICE_INTERVAL,
    LOW_BUDGET_INTERVAL_MINUTES,
    MAX_CHECK_INTERVAL_MINUTES,
    MARKET_REFRESH_MINUTES,
    MAX_SPEC_LOOKUPS_PER_DEAL_SCAN,
    LAPTOP_SALES_WINDOW_DAYS,
    SALES_WINDOW_DAYS,
    SEARCH_RESERVE,
    THREAD_POOL_SIZE,
    WATCH_CONCURRENCY,
    log,
)
from db import (
    get_api_calls_today,
    set_meta,
    add_deal,
    bulk_upsert_seen_items,
    cleanup_old_listing_obs,
    purge_deleted_watches,
    track_prices,
    cleanup_old_seen_items,
    get_market_stats,
    get_min_profit,
    get_seen_items,
    get_sold_listings,
    list_watches,
)
from ebay_api import BudgetExhausted, browse_budget_left, fetch_browse_rate_limit, seconds_until_reset
from market import (
    _annotate_items,
    _apply_item_filters,
    _recalculate_watch_medians,
    _stat_for_item,
    collapse_watch_categories,
    prune_laptop_parts,
    normalize_saved_specs,
    laptop_unknown,
    max_buy_price,
)
from panel import repost_panel
from discovery import run_discovery
from trading_api import verify_disappeared
from notifications import (
    _notify_median_error,
    _notify_median_ready,
    _notify_new_deals,
    _notify_price_drops,
)
from sales import is_slow_seller, price_drops, sales_note
from shared_market import deal_scan
from netstatus import is_down, mark_down, mark_up
from deal_check import recheck_deals
from deal_reprice import reprice_deals
from markdowns import run_markdown_scan
from backup import daily_backup


async def check_all_watches(app: Application):
    watches = list_watches(active_only=True)
    semaphore = asyncio.Semaphore(WATCH_CONCURRENCY)
    skipped: list = []

    async def check(w):
        async with semaphore:
            # Ліміт закінчився посеред циклу — решту товарів перевіримо після скидання
            if browse_budget_left() <= 0:
                skipped.append(w["id"])
                return
            try:
                await check_one_watch(app, w)
            except BudgetExhausted:
                skipped.append(w["id"])
            except Exception as e:
                log.exception("Помилка при перевірці watch #%s: %s", w["id"], e)
                await _notify_median_error(app, w, e)

    await asyncio.gather(*(check(w) for w in watches))
    if skipped:
        log.warning("Ліміт запитів eBay вичерпано — пропущено товарів: %s (до скидання ліміту)", len(skipped))

    # Нові вигідні пропозиції — одне сповіщення на чат за весь цикл перевірки
    for chat_id, found in app.bot_data.pop("new_deals_by_chat", {}).items():
        await _notify_new_deals(app, chat_id, found)

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
        drops = await asyncio.to_thread(price_drops, w["id"])
        if drops:
            await _notify_price_drops(app, w, drops)
    else:
        stats = {(r["cond_group"], r["spec_group"]): r for r in rows}
        def _deal_scan():
            # Спільний пошук: той самий товар інших користувачів — один запит на всіх
            found = deal_scan(w, DEAL_SCAN_LIMIT)
            _annotate_items(found, max_lookups=MAX_SPEC_LOOKUPS_PER_DEAL_SCAN, watch=w)
            return _apply_item_filters(w, found)

        items = await asyncio.to_thread(_deal_scan)

    if items:   # «📉 Знизили ціну»: перша й поточна ціна кожного оголошення
        await asyncio.to_thread(track_prices, w["id"], items)
    if not items or not stats:
        return

    new_deals = []
    sold = get_sold_listings(w["id"], max(SALES_WINDOW_DAYS, LAPTOP_SALES_WINDOW_DAYS))  # як продаються конфігурації
    min_profit = get_min_profit(w["chat_id"])
    seen_map = get_seen_items(w["id"], [it["item_id"] for it in items])
    seen_updates = []  # записуються одним пакетом наприкінці
    for it in items:
        seen = seen_map.get(it["item_id"])
        is_new = seen is None
        price_dropped = (not is_new) and seen["last_price"] is not None and it["effective_price"] < seen["last_price"] - 0.01

        if not is_new and not price_dropped:
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue

        if laptop_unknown(it):
            # Відеокарта ще невідома: не записуємо в seen_items — наступного циклу,
            # коли бот прочитає характеристики, оголошення оціниться як нове
            continue
        stat = _stat_for_item(stats, it)
        if stat is None:
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue  # для цього стану ще немає надійної статистики

        sale_price = stat["sale_price"] or stat["median_price"]
        # Вигідно, якщо ціна купівлі (для Best Offer — з урахуванням торгу)
        # не вища за максимальну, що ще дає мінімальний прибуток при перепродажі
        # Вигідно — лише якщо прибуток ≥ мінімуму користувача за ЦІНОЮ ОГОЛОШЕННЯ (торг — бонус,
        # а не підстава: «можна торгуватись» не робить збиткову пропозицію вигідною)
        if it["total_price"] > max_buy_price(sale_price, min_profit):
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue
        # Дешево, але така конфігурація не продається (при живому ринку) — не сповіщаємо
        if is_slow_seller(sold, it["cond_group"], it["spec_group"]):
            log.info("watch #%s: %s — вигідна ціна, але %s не продається, пропускаю",
                     w["id"], it["item_id"], it["spec_group"])
            seen_updates.append((it["item_id"], it["effective_price"], None))
            continue
        it["sales_note"] = sales_note(sold, stat["cond_group"], stat["spec_group"])
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
            cond_group=stat["cond_group"],
            spec_group=stat["spec_group"],
            listed_at=it.get("created_at"),
            item_spec=it["spec_group"],
            sale_source=stat.get("sale_source"),
            sale_sample=stat.get("sample_size"),
        )
        seen_updates.append((it["item_id"], it["effective_price"], it["effective_price"]))
        new_deals.append((deal_id, it, stat))

    bulk_upsert_seen_items(w["id"], seen_updates)

    if not new_deals:
        return

    # Самі пропозиції — у «🔥 Вигідні пропозиції»; сповіщення — одне на цикл (check_all_watches)
    app.bot_data.setdefault("new_deals_by_chat", {}).setdefault(w["chat_id"], []).extend(
        (deal_id, it, stat, w) for deal_id, it, stat in new_deals)


def _refresh_deals():
    """Спершу переоцінка за свіжою статистикою (без запитів до eBay) — тоді перевірка,
    чи лоти ще продаються, не витрачає запити на пропозиції, що вже невигідні."""
    try:
        reprice_deals()
    except Exception as e:
        log.warning("Не вдалося переоцінити вигідні пропозиції: %s", e)
    return recheck_deals()


_error_notice = {"last": 0.0}


TELEGRAM_RECHECK_SECONDS = 5


def _start_telegram_watch(app):
    """Після мережевої помилки Telegram — стежимо, коли зв'язок повернеться."""
    task = app.bot_data.get("telegram_watch_task")
    if task is None or task.done():
        app.bot_data["telegram_watch_task"] = asyncio.create_task(_wait_telegram_back(app))


async def _wait_telegram_back(app):
    while is_down("Telegram"):
        await asyncio.sleep(TELEGRAM_RECHECK_SECONDS)
        try:
            await app.bot.get_me()
        except (NetworkError, TimedOut):
            continue
        except Exception as e:
            log.debug("Перевірка зв'язку з Telegram: %s", e)
            continue
        mark_up("Telegram")


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Необроблені помилки: у лог повністю, власнику — коротке повідомлення.
    Тимчасові мережеві збої Telegram лише логуються."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Тимчасова мережева помилка Telegram: %s", err)
        mark_down("Telegram")
        _start_telegram_watch(context.application)
        return
    log.error("Помилка під час обробки оновлення", exc_info=err)
    if config.OWNER_TELEGRAM_ID and time.time() - _error_notice["last"] > ERROR_NOTICE_INTERVAL:
        _error_notice["last"] = time.time()
        try:
            await context.bot.send_message(
                chat_id=config.OWNER_TELEGRAM_ID,
                text=f"⚠️ Помилка в боті: {type(err).__name__}: {str(err)[:300]}\n"
                     "Повні деталі — в логах (sudo docker compose logs).",
            )
        except Exception:
            pass


def pace_interval(app, left, now=None):
    """Інтервал між циклами пошуку (хв). Бот міряє, скільки запитів Browse він зробив за
    останню годину, і порівнює з тим, скільки можна витрачати на годину до скидання ліміту:
    витрачає більше — інтервал ×1.5 (до MAX_CHECK_INTERVAL_MINUTES), помітно менше — ÷1.5
    (до CHECK_INTERVAL_MINUTES). Так ліміту вистачає на всю добу, а не до другої ночі."""
    now = now or time.time()
    pace = app.bot_data.setdefault("pace", {"interval": float(CHECK_INTERVAL_MINUTES), "samples": []})
    used = get_api_calls_today("browse")
    samples = pace["samples"]
    if samples and used < samples[-1][1]:
        samples.clear()   # власний лічильник почав нову добу
    samples.append((now, used))
    while samples and samples[0][0] < now - 3600:
        samples.pop(0)
    first_t, first_used = samples[0]
    span = now - first_t
    if span >= 20 * 60:
        rate = (used - first_used) / span * 3600            # запитів на годину зараз
        hours = max(0.5, (seconds_until_reset() or 12 * 3600) / 3600)
        allowed = max(left, 0) / hours                       # можна на годину, щоб дотягнути
        interval = pace["interval"]
        if rate > allowed:
            interval = min(MAX_CHECK_INTERVAL_MINUTES, interval * 1.5)
        elif rate < allowed * 0.6:
            interval = max(CHECK_INTERVAL_MINUTES, interval / 1.5)
        if interval != pace["interval"]:
            log.info("Запитів eBay ~%.0f/год, можна ~%.0f/год до скидання — пошук нових оголошень раз на %.0f хв",
                     rate, allowed, interval)
        pace["interval"] = interval
    set_meta("check_interval", f"{pace['interval']:.0f}")
    return pace["interval"]


async def scheduler_loop(app: Application):
    try:
        await asyncio.sleep(5)
        last_cleanup_at = 0.0
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

            # «💡 Що перепродавати» — окремою задачею, щоб не затримувати перевірку товарів
            task = app.bot_data.get("discovery_task")
            if task is None or task.done():
                app.bot_data["discovery_task"] = asyncio.create_task(asyncio.to_thread(run_discovery))

            # «Справді продано?» — перевірка зниклих оголошень через Trading API
            # (лише коли власник підключив акаунт eBay; окремий ліміт, Browse не витрачає)
            task = app.bot_data.get("sold_check_task")
            if task is None or task.done():
                app.bot_data["sold_check_task"] = asyncio.create_task(asyncio.to_thread(verify_disappeared))

            # «📉 Знизили ціну»: найдешевші оголошення кожного товару раз на кілька годин
            task = app.bot_data.get("markdown_task")
            if task is None or task.done():
                app.bot_data["markdown_task"] = asyncio.create_task(asyncio.to_thread(run_markdown_scan))

            # «🔥 Вигідні пропозиції»: чи лоти ще продаються — продані/зняті зникають зі списку
            task = app.bot_data.get("deal_check_task")
            if task is None or task.done():
                app.bot_data["deal_check_task"] = asyncio.create_task(asyncio.to_thread(_refresh_deals))

            # Резервна копія бази — раз на добу (окремим потоком, не заважає перевіркам)
            task = app.bot_data.get("backup_task")
            if task is None or task.done():
                app.bot_data["backup_task"] = asyncio.create_task(asyncio.to_thread(daily_backup))

            # Раз на добу прибираємо застарілі записи seen_items
            now = time.time()
            if now - last_cleanup_at > 86400:
                try:
                    removed = await asyncio.to_thread(cleanup_old_seen_items)
                    removed_obs = await asyncio.to_thread(cleanup_old_listing_obs)
                    await asyncio.to_thread(purge_deleted_watches)
                    await asyncio.to_thread(collapse_watch_categories)
                    await asyncio.to_thread(prune_laptop_parts)
                    await asyncio.to_thread(normalize_saved_specs)
                    if removed or removed_obs:
                        log.info("Очищено застарілих записів: seen_items %s, listing_obs %s",
                                 removed, removed_obs)
                except Exception as e:
                    log.warning("Не вдалося очистити seen_items: %s", e)
                last_cleanup_at = now

            # Рівномірно на добу: інтервал підлаштовується, щоб запитів вистачило до скидання
            left = browse_budget_left()
            interval = pace_interval(app, left)
            if left < SEARCH_RESERVE:
                interval = max(interval, LOW_BUDGET_INTERVAL_MINUTES)
                # Прокинутись одразу після скидання ліміту eBay, а не чекати повні 30 хв
                until_reset = seconds_until_reset()
                # +3 хв: одразу о 09:00 eBay ще може відповідати 429 — ліміт скидається не миттєво
                if until_reset is not None and 0 < until_reset + 180 < interval * 60:
                    interval = (until_reset + 180) / 60
                log.info("Бюджет eBay майже вичерпано (лишилось %s) — наступна перевірка через %.0f хв",
                         left, interval)
            await asyncio.sleep(interval * 60)
    except asyncio.CancelledError:
        log.info("Фоновий планувальник зупинено")
        raise


async def post_init(app: Application):
    # Більше потоків для мережевих запитів: фонова перевірка товарів не
    # займає всі потоки, тож кнопки не чекають, поки вона закінчиться
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE, thread_name_prefix="bot"))
    app.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(app))
    # Список команд, що випадає над клавіатурою в Telegram
    await app.bot.set_my_commands([("menu", "📋 Головне меню")])


async def post_shutdown(app: Application):
    for key in ("scheduler_task", "telegram_watch_task"):
        task = app.bot_data.pop(key, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
