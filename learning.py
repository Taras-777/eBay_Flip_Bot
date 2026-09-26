"""
«🚫 Не той товар»: запам'ятовування чужих лотів і автоматичне вивчення
слів, за якими такі лоти можна відсіювати й надалі.

Як бот вивчає слово: воно має бути щонайменше у MIN_REJECTS_FOR_WORD
відхилених назвах і майже не траплятися (≤ MAX_ACCEPTED_SHARE) у назвах
правильних оголошень цього товару. Тоді слово додається до виключених —
і далі лоти з ним не потрапляють ні в пошук, ні в статистику. Кожне вивчене
слово можна скасувати кнопкою, після чого бот його більше не пропонує.
"""

from collections import Counter

from settings import MIN_SAMPLE_SIZE, log
from textparse import CONSOLE_PRODUCT_TERMS, LAPTOP_PRODUCT_TERMS, MODEL_VARIANT_TERMS, _search_tokens
from db import (
    delete_listing_obs_by_ids,
    get_current_listings,
    get_learned_words,
    get_rejected_titles,
    get_watch,
    reject_item,
    set_learned_word,
    update_watch_exclude,
)

MIN_REJECTS_FOR_WORD = 2
MAX_ACCEPTED_SHARE = 0.10
MAX_NEW_WORDS_PER_REJECT = 3

# Слова-варіанти моделі («slim», «pro», «digital»…) ніколи не вивчаємо
# автоматично: відхилені «PS3 Slim» не мають вимкнути «PS5 Slim»
PROTECTED_WORDS = CONSOLE_PRODUCT_TERMS | LAPTOP_PRODUCT_TERMS | MODEL_VARIANT_TERMS


def learn_exclude_words(watch):
    """Слова, які варто виключити, за відхиленими назвами товару."""
    watch_id = watch["id"]
    rejected = get_rejected_titles(watch_id)
    if len(rejected) < MIN_REJECTS_FOR_WORD:
        return []

    accepted = [_search_tokens(r["title"]) for r in get_current_listings(watch_id) if r.get("title")]
    # Без достатньої вибірки правильних лотів не можна перевірити, що слово
    # не трапляється в них, — тоді краще нічого не вчити
    if len(accepted) < MIN_SAMPLE_SIZE:
        return []

    blocked = (_search_tokens(watch["query"]) | _search_tokens(watch.get("exclude") or "")
               | set(get_learned_words(watch_id)) | PROTECTED_WORDS)
    counts = Counter(tok for title in rejected for tok in _search_tokens(title))
    limit = MAX_ACCEPTED_SHARE * len(accepted)

    words = []
    for word, n in counts.most_common():
        if n < MIN_REJECTS_FOR_WORD:
            break
        if word in blocked or not 3 <= len(word) <= 30 or word.isdigit():
            continue
        if sum(1 for toks in accepted if word in toks) > limit:
            continue
        words.append(word)
        if len(words) >= MAX_NEW_WORDS_PER_REJECT:
            break
    return words


def _set_exclude_words(watch, words_to_add=(), words_to_remove=()):
    current = (watch.get("exclude") or "").split()
    remove = {w.casefold() for w in words_to_remove}
    result = [w for w in current if w.casefold() not in remove]
    for w in words_to_add:
        if w not in (x.casefold() for x in result):
            result.append(w)
    update_watch_exclude(watch["id"], watch["chat_id"], " ".join(result))


def reject_and_learn(watch, item_id, title):
    """
    Позначає лот як «не той товар» і, якщо вже є закономірність, додає нові
    слова до виключених. Повертає список щойно вивчених слів.
    """
    reject_item(watch["id"], item_id, title)
    try:
        words = learn_exclude_words(watch)
    except Exception as e:
        log.warning("Не вдалося проаналізувати відхилені лоти для watch #%s: %s", watch["id"], e)
        return []
    if not words:
        return []

    _set_exclude_words(watch, words_to_add=words)
    for word in words:
        set_learned_word(watch["id"], word, "excluded")

    # Поточні спостереження з цими словами прибираємо одразу: інакше вони
    # зникнуть із пошуку (через «-слово») і зарахуються як продані лоти
    word_set = set(words)
    stale = [r["item_id"] for r in get_current_listings(watch["id"])
             if _search_tokens(r.get("title") or "") & word_set]
    delete_listing_obs_by_ids(watch["id"], stale)
    log.info("Watch #%s: вивчено виключені слова %s", watch["id"], words)
    return words


def unlearn_word(watch_id, chat_id, word):
    """Скасовує вивчене слово: прибирає з виключених і більше не пропонує."""
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        return False
    _set_exclude_words(watch, words_to_remove=[word])
    set_learned_word(watch_id, word, "ignored")
    return True


def learned_words_note(words):
    quoted = ", ".join(f"«{w}»" for w in words)
    return (f"🧠 Помітив закономірність: {quoted} — у відхилених лотах і майже ніколи в правильних. "
            f"Тепер такі лоти відсіюються автоматично.")