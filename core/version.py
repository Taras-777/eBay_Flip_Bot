"""
Версія бота у вигляді v2.15, яка зростає сама після кожної зміни коду.

Як це працює: при запуску бот рахує «відбиток» вмісту всіх своїх .py-файлів (корінь і пакети
core, ebay, analysis, ui; config.py із секретами — ні)
і порівнює з тим, що запам'ятав у базі минулого разу. Якщо код змінився
(після оновлення з GitHub) — номер збірки збільшується на 1. Перезапуск без
змін коду номер не змінює. База лежить у data/, тож номер переживає
перезбірку Docker.
"""

import hashlib
import os

from core.db import get_meta, set_meta

MAJOR = 2  # змінюй вручну лише для великих переробок: v3.1, v3.2…
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # корінь проєкту (над core/)
CODE_DIRS = ("", "core", "ebay", "analysis", "ui")

_cache = {"version": None}


def code_fingerprint():
    digest = hashlib.sha1(usedforsecurity=False)  # лише відбиток коду, не безпека
    for folder in CODE_DIRS:
        path = os.path.join(ROOT, folder)
        if not os.path.isdir(path):
            continue
        for name in sorted(n for n in os.listdir(path) if n.endswith(".py") and n != "config.py"):
            digest.update(f"{folder}/{name}".encode())
            with open(os.path.join(path, name), "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()


def get_version():
    """«v2.N». Номер збірки N зростає, щойно змінився код."""
    if _cache["version"]:
        return _cache["version"]
    fingerprint = code_fingerprint()
    build = int(get_meta("build_number", "0") or 0)
    if get_meta("code_fingerprint") != fingerprint:
        build += 1
        set_meta("build_number", build)
        set_meta("code_fingerprint", fingerprint)
    _cache["version"] = f"v{MAJOR}.{build}"
    return _cache["version"]
