"""Версія бота (v2.N) зростає сама після кожної зміни коду."""
import re

import pytest

import panel
import version


@pytest.fixture(autouse=True)
def fresh_version_cache():
    version._cache["version"] = None
    yield
    version._cache["version"] = None


def restart():
    """Імітація перезапуску бота: версія рахується заново."""
    version._cache["version"] = None
    return version.get_version()


def test_version_format():
    assert re.fullmatch(r"v\d+\.\d+", version.get_version())


def test_build_number_grows_only_when_code_changes(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(version, "ROOT", str(tmp_path))
    assert restart() == f"v{version.MAJOR}.1"      # перший запуск
    assert restart() == f"v{version.MAJOR}.1"      # перезапуск без змін коду

    (tmp_path / "a.py").write_text("x = 2\n")      # оновили код
    assert restart() == f"v{version.MAJOR}.2"
    (tmp_path / "b.py").write_text("y = 1\n")      # додали файл
    assert restart() == f"v{version.MAJOR}.3"


def test_main_menu_shows_version(monkeypatch):
    monkeypatch.setattr(panel, "is_owner", lambda uid: False)
    assert f"🏷 Версія {version.get_version()}" in panel.main_menu_text(5)
