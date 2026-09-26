"""
Повідомлення про знахідки та проблеми з ринковими даними.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings import EBAY_SELLING_FEES_PCT, RESALE_SHIPPING_EUR, log
from textparse import _group_label
from market import estimate_resale_profit, max_buy_price
from panel import notify


async def _send_single_deal(app, w, deal_id, it, stat):
    sale_price = stat["sale_price"] or stat["median_price"]
    _, profit = estimate_resale_profit(sale_price, it["total_price"])
    buy_limit = max_buy_price(sale_price)

    warning = "\n⚠️ Низький рейтинг продавця — перевір уважно перед покупкою" if it["suspicious"] else ""
    best_offer_note = (
        "\n🎯 Можна запропонувати свою ціну (Best Offer) — реальна ціна може бути ще нижчою"
        if it["has_best_offer"] else ""
    )
    spec_note = f" · 💾 {it['spec_group']}" if it["spec_group"] != "unspecified" else ""
    price_drop_note = "\n🔻 Ціна впала ще нижче з моменту першої появи цього лота" if it.get("price_dropped") else ""

    text = (
        f"🔥 Вигідний лот: {w['label']}{price_drop_note}\n\n"
        f"{it['title']}\n"
        f"💶 Ціна з доставкою: {it['total_price']:.0f}€ (купувати варто до ~{buy_limit:.0f}€)\n"
        f"📈 Реалістична ціна продажу: ~{sale_price:.0f}€ ({stat['sale_source']})\n"
        f"📊 Медіана пропозицій: ~{stat['median_price']:.0f}€\n"
        f"💰 Орієнтовний прибуток: ~{profit:.0f}€ "
        f"(після комісії eBay ~{EBAY_SELLING_FEES_PCT}% і доставки ~{RESALE_SHIPPING_EUR}€)\n"
        f"🏷️ Стан: {it.get('condition') or 'н/д'}{spec_note}{warning}{best_offer_note}\n"
        f"⚠️ Ціна продажу — оцінка, а не підтверджений продаж.\n"
        f"🔗 {it['url']}"
    )
    buttons = [
        [
            InlineKeyboardButton("✅ Куплено", callback_data=f"buy:{deal_id}"),
            InlineKeyboardButton("❌ Пропущено", callback_data=f"skip:{deal_id}"),
        ],
        [InlineKeyboardButton("🚫 Не той товар", callback_data=f"rejd:{deal_id}")],
        [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
    ]
    await notify(app, w["chat_id"], text, reply_markup=InlineKeyboardMarkup(buttons))


async def _send_grouped_deals(app, w, new_deals):
    lines = [f"🔥 Знайдено {len(new_deals)} вигідних лотів: {w['label']}\n"]
    reject_buttons = []
    for n, (deal_id, it, stat) in enumerate(new_deals, 1):
        sale_price = stat["sale_price"] or stat["median_price"]
        _, profit = estimate_resale_profit(sale_price, it["total_price"])
        warning = " ⚠️" if it["suspicious"] else ""
        offer_mark = " 🎯" if it["has_best_offer"] else ""
        drop_mark = " 🔻" if it.get("price_dropped") else ""
        spec_note = f" · 💾 {it['spec_group']}" if it["spec_group"] != "unspecified" else ""
        reject_buttons.append(InlineKeyboardButton(f"🚫 #{n} не той", callback_data=f"rejd:{deal_id}"))
        lines.append(
            f"{n}. {it['title'][:120]}\n"
            f"💰 {it['total_price']:.0f}€ → продаж ~{sale_price:.0f}€, прибуток ~{profit:.0f}€"
            f"{spec_note}{warning}{offer_mark}{drop_mark}\n🔗 {it['url']}"
        )
    rows = [reject_buttons[i:i + 3] for i in range(0, len(reject_buttons), 3)]
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    await notify(app, w["chat_id"], "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _notify_median_ready(app, w, new_stats):
    """Сповіщає, щойно для товару вперше порахувалась статистика групи."""
    lines = [f"📊 Ринок для «{w['label']}» проаналізовано — тепер шукаю вигідні лоти!\n"]
    for s in new_stats:
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price)
        lines.append(
            f"• {_group_label(s['cond_group'], s['spec_group'])}: продати ~{sale_price:.0f}€, "
            f"купувати до ~{buy_limit:.0f}€ ({s['sample_size']} оголошень)"
        )
    try:
        await notify(app, w["chat_id"], "\n".join(lines))
    except Exception as e:
        log.warning("Не вдалося надіслати сповіщення про ринок для watch #%s: %s", w["id"], e)


async def _notify_median_problem(app, w, reason):
    text = (
        f"⚠️ Не вдалося порахувати медіану для «{w['label']}».\n\n"
        f"Причина: {reason}\n\n"
        "Спробуй оновити оголошення пізніше або перевірити назву товару."
    )
    buttons = [[
        InlineKeyboardButton("📊 Відкрити товар", callback_data=f"watch_details:{w['id']}")
    ]]
    try:
        await notify(app, w["chat_id"], text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        log.warning("Не вдалося надіслати пояснення проблеми медіани для watch #%s: %s", w["id"], e)


async def _notify_median_error(app, w, error):
    reason = str(error).strip() or error.__class__.__name__
    await _notify_median_problem(
        app,
        w,
        f"внутрішня помилка під час запиту або обробки даних: {reason}",
    )