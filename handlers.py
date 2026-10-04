"""
Екрани й діалоги бота. Код розділено на модулі screen_*.py за екранами;
цей файл лише збирає їх разом (для main.py і тестів).
"""

from telegram.ext import ConversationHandler  # noqa: F401

from screen_common import (  # noqa: F401
    _ebay_configured,
    _category_keyboard,
    _aspect_keyboard,
    ASPECT_CHOICE_TEXT,
    _ago,
    _when,
)
from screen_addwatch import (  # noqa: F401
    ASK_QUERY,
    ASK_CATEGORY,
    ASK_ASPECT,
    ASK_MIN_PRICE_CHOICE,
    ASK_CUSTOM_MIN_PRICE,
    NEW_WATCH_KEYS,
    _clear_new_watch,
    addwatch_start,
    addwatch_got_query,
    addwatch_shared_choice,
    _ask_categories,
    _render_new_watch_categories,
    addwatch_category_choice,
    _propose_aspects,
    _render_new_watch_aspects,
    addwatch_aspect_choice,
    _propose_min_price,
    addwatch_min_price_choice,
    addwatch_custom_min_price,
    _finalize_watch,
    addwatch_cancel,
    addwatch_menu_interrupt,
)
from screen_watch import (  # noqa: F401
    cmd_start,
    cmd_menu,
    cmd_list,
    _watch_details_text,
    _show_watch_details,
    watch_details_callback,
    all_configs_callback,
    _render_aspect_picker,
    required_aspect_callback,
    toggle_aspect_callback,
    save_aspects_callback,
    set_required_aspect_callback,
    change_category_callback,
    _render_watch_categories,
    set_category_callback,
    EDIT_VALUE,
    edit_menu_callback,
    edit_value_start,
    _apply_edit,
    edit_value_button,
    edit_value_text,
    edit_interrupt,
    edit_cancel,
    recalculate_median_callback,
    refresh_all_callback,
    refresh_stop_callback,
    delwatch_ask_callback,
    delwatch_yes_callback,
    cmd_stats,
    learned_words_callback,
    _show_learned_words,
    _learned_excluded,
)
from screen_sales import (  # noqa: F401
    SALES_LIST_MAX,
    _sales_specs,
    _flt_spec,
    _filtered,
    _listed,
    _spec_label,
    _sales_text,
    _sales_keyboard,
    _show_sales,
    sales_callback,
    sales_check_now_callback,
    sales_reject_callback,
    config_listings_callback,
)
from screen_listings import (  # noqa: F401
    LISTINGS_MAX_PAGES,
    LISTINGS_PAGE_SIZE,
    LISTINGS_CHUNK,
    LISTINGS_CACHE_SECONDS,
    _new_fetch_state,
    _fetch_cheapest,
    _has_more,
    view_listings_callback,
    listing_old_callback,
    _listing_state_key,
    _short_listing_label,
    _render_listing_panel,
    listing_page_callback,
    reject_listing_callback,
    reject_deal_callback,
    unlearn_word_callback,
    deal_action_callback,
)
from screen_deals import (  # noqa: F401
    min_profit_callback,
    DEALS_PER_PAGE,
    _deal_card,
    _render_deals,
    deals_callback,
    deal_inbox_action_callback,
    deals_clear_callback,
)
from screen_discover import (  # noqa: F401
    _recommendation_text,
    discover_callback,
    discover_refresh_callback,
    discover_add_callback,
    discover_hide_callback,
    discover_hidden_callback,
)
from screen_check import (  # noqa: F401
    CHECK_LINK,
    check_listing_start,
    check_listing_text,
    check_listing_interrupt,
    check_cancel,
)


import importlib as _importlib

_SCREENS = [_importlib.import_module(m) for m in (
    "screen_common", "screen_addwatch", "screen_watch", "screen_sales",
    "screen_listings", "screen_deals", "screen_discover", "screen_check", "screen_markdowns", "screen_undo", "screen_stats",
    "screen_notices", "screen_unknown",
)]


def __getattr__(name):
    """Сумісність: handlers.<будь-що>, що є в якомусь з екранів (напр. імпортовані функції)."""
    for module in _SCREENS:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module 'handlers' has no attribute {name!r}")
