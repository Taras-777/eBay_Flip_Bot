"""
Оформлення екранів: однаковий вигляд усіх меню.

Правила:
  * заголовок — емодзі + жирна назва, під ним (за потреби) короткий рядок курсивом «що тут»;
  * головні цифри — зверху й жирним; блоки розділені порожнім рядком;
  * довгі пояснення — у згорнутій цитаті (розгортається натисканням), щоб не займали екран;
  * кнопки: головна дія — на всю ширину, другорядні — по дві в рядку; «◀️ Назад» — останнім рядком.
"""

import html

from telegram import InlineKeyboardButton

RULE = "━━━━━━━━━━━━━━"


def title(emoji, text, subtitle=None, rule=False, count=None):
    """«🔥 <b>Вигідні пропозиції</b> (3)» + рядок курсивом під ним; rule — лінія-розділювач нижче."""
    lines = [f"{emoji} <b>{text}</b>" + (f" ({count})" if count is not None else "")]
    if subtitle:
        lines.append(f"<i>{subtitle}</i>")
    if rule:
        lines.append(RULE)
    return "\n".join(lines)


def section(emoji, text):
    """Підзаголовок блоку: «📋 <b>Товари в категорії</b>»."""
    return f"{emoji} <b>{text}</b>"


def hint(text, title_text="ℹ️ Як це працює"):
    """Пояснення в згорнутій цитаті: на екрані — лише перший рядок, решта — після натискання."""
    return f"<blockquote expandable><b>{title_text}</b>\n{text}</blockquote>"


def note_line(note):
    """Повідомлення про щойно виконану дію («✅ Збережено») — окремим блоком над екраном."""
    return f"{note}\n\n" if note else ""


def kv(label, value, bold=True):
    """«💶 Ціна: <b>420€</b>»."""
    return f"{label}: <b>{value}</b>" if bold else f"{label}: {value}"


def esc(text):
    return html.escape(str(text or ""), quote=False)


def grid(buttons, cols=2):
    """Кнопки рядками по cols."""
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def back_row(callback="menu:home", label="◀️ Меню"):
    return [InlineKeyboardButton(label, callback_data=callback)]


def menu_row():
    return back_row("menu:home", "🏠 Меню")
