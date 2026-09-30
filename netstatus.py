"""
Стан мережевого з'єднання з eBay і Telegram — щоб після збою в лозі було
видно не лише помилки, а й момент, коли зв'язок відновився.
"""

import threading
import time

from settings import log

_down_since: dict = {}
_lock = threading.Lock()


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} хв {seconds % 60} с"
    return f"{seconds // 3600} год {seconds % 3600 // 60} хв"


def mark_down(name):
    """Запит до сервісу не вдався через мережу."""
    with _lock:
        _down_since.setdefault(name, time.time())


def mark_up(name):
    """Запит пройшов. Якщо перед цим був збій — пише в лог, що зв'язок відновився."""
    with _lock:
        since = _down_since.pop(name, None)
    if since is not None:
        log.info("✅ З'єднання з %s відновлено (перерва %s)", name, _duration(time.time() - since))
        return True
    return False


def is_down(name):
    with _lock:
        return name in _down_since
