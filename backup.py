"""
Резервні копії бази: раз на добу стиснута копія в data/backups/, зберігаються
останні BACKUP_KEEP. Власник може отримати свіжу копію файлом у «⚙️ Налаштування».

Копія робиться через sqlite3 backup API — безпечно навіть поки бот пише в базу.
"""

import glob
import gzip
import os
import shutil
import sqlite3
import time
from datetime import datetime

import settings
from settings import LOCAL_TZ, log

BACKUP_KEEP = 7
BACKUP_EVERY_HOURS = 24


def backup_dir():
    return os.path.join(settings.DATA_DIR, "backups")


def list_backups():
    """Файли копій, найновіші першими."""
    return sorted(glob.glob(os.path.join(backup_dir(), "ebay_flip_bot-*.sqlite3.gz")), reverse=True)


def make_backup():
    """Нова стиснута копія бази. Повертає шлях до файлу."""
    os.makedirs(backup_dir(), exist_ok=True)
    stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d_%H%M%S")
    raw = os.path.join(backup_dir(), f"ebay_flip_bot-{stamp}.sqlite3")
    src = sqlite3.connect(settings.DB_PATH, timeout=30)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(raw, "rb") as f_in, gzip.open(raw + ".gz", "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw)
    for old in list_backups()[BACKUP_KEEP:]:
        try:
            os.remove(old)
        except OSError:
            pass
    log.info("Резервна копія бази: %s (%.1f МБ)", os.path.basename(raw) + ".gz",
             os.path.getsize(raw + ".gz") / 1024 / 1024)
    return raw + ".gz"


def daily_backup():
    """Нова копія, якщо остання старша за BACKUP_EVERY_HOURS. Викликати з потоку."""
    backups = list_backups()
    if backups and time.time() - os.path.getmtime(backups[0]) < BACKUP_EVERY_HOURS * 3600:
        return None
    try:
        return make_backup()
    except Exception as e:
        log.warning("Не вдалося зробити резервну копію бази: %s", e)
        return None


def backups_summary():
    """Рядок для «⚙️ Налаштування»."""
    backups = list_backups()
    if not backups:
        return "💾 Резервних копій ще немає — перша з'явиться протягом доби"
    last = datetime.fromtimestamp(os.path.getmtime(backups[0]), LOCAL_TZ).strftime("%d.%m о %H:%M")
    return f"💾 Резервні копії: остання {last}, зберігається {len(backups)} з {BACKUP_KEEP}"
