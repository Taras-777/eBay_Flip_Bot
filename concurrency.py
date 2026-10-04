"""
Одночасна обробка натискань.

Раніше бот обробляв оновлення строго по черзі: одна повільна дія (оновлення цін,
перевірка оголошення) змушувала всі інші кнопки чекати. Тепер звичайні кнопки
обробляються паралельно (до settings.CONCURRENT_UPDATES одночасно).

Діалоги (додавання товару, редагування, перевірка оголошення, вхід в eBay) — виняток:
вони покладаються на те, що кроки йдуть по черзі (бібліотека прямо про це попереджає).
Тому все, що стосується діалогу (його початок, кроки й кнопки під час нього), іде через
спільний замок — по одному, як раніше.
"""

import asyncio

from telegram.ext import BaseUpdateProcessor


class DialogSafeUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates):
        super().__init__(max_concurrent_updates)
        self.conversations = []   # заповнює main після створення діалогів
        self._dialog_lock = asyncio.Lock()

    def _is_dialog_update(self, update):
        for conv in self.conversations:
            try:
                if conv.check_update(update) is not None:
                    return True
            except Exception:
                return True   # не впевнені — безпечніше по черзі
        return False

    async def do_process_update(self, update, coroutine):
        if self._is_dialog_update(update):
            async with self._dialog_lock:
                await coroutine
        else:
            await coroutine

    async def initialize(self):
        pass

    async def shutdown(self):
        pass
