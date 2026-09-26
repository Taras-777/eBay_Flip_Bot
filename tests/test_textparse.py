"""Розбір назв оголошень: конфігурації, варіанти моделей, аксесуари, переклад категорій."""
import pytest

from textparse import _title_matches_search, category_label, extract_spec_key, spec_key_from_aspects

EXCLUDE = "broken defekt teile parts kaputt"


@pytest.mark.parametrize("title,expected", [
    ("Apple MacBook Pro 14 M1 Pro 16GB 512GB", "16GB+512GB+M1PRO"),
    ("Dell XPS 13 i7-1165G7 16GB 512GB", "16GB+512GB+I7-1165G7"),
    ("Lenovo Core Ultra 7 155H 32GB 1TB", "1TB+32GB+COREULTRA7-155H"),
    ("iPhone 15 Pro 256GB Titan", "256GB"),
    ("Nintendo Switch OLED", "unspecified"),
])
def test_extract_spec_key(title, expected):
    assert extract_spec_key(title) == expected


@pytest.mark.parametrize("query,title,expected", [
    ("PlayStation 5", "Sony PlayStation 5 Digital Edition 825 GB Konsole", True),
    ("PlayStation 5", "Sony PlayStation 5 Pro 2TB Spielekonsole", False),
    ("PS5 Pro", "Sony PlayStation 5 Pro 2TB Spielekonsole", True),
    ("PlayStation 5", "Sony Playstation 3 PS3 Slim HEN PS3HEN 3.5.0 PS1 PS2 Jailbreak Emu", False),
    ("PlayStation 5", "Sony PlayStation 4 500GB - Schwarz, Firmware 5.05, First Release Console", False),
    ("PlayStation 5", "Sony PlayStation 4 1TB Spielkonsole, 5 Controllern Schwarz und 44 Spielen TOP", False),
    ("PlayStation 5", "Sony PlayStation Portal 30th Anniversary - Limited Edition - PS5 Remote Player", False),
    ("PlayStation 5", "Sony PlayStation 5 disc Edition 825GB Selten Störungen Farbe Weiß", True),
    ("PlayStation 5", "Sony PS5 825 GB Digital Edition - Sehr guter Zustand", True),
    ("PS5", "Sony Playstation5 Slim Disc 1TB", True),
    ("iPhone 13", "Apple iPhone 13 Pro Max 256GB", False),
    ("iphone 13 pro", "D&G iPhone 13 Pro Case Pink", False),
    ("iphone 13 pro", "Apple iPhone 13 Pro 128GB Graphit Akku 89%", True),
    ("ThinkPad X1 Carbon Gen 10", "Lenovo ThinkPad X1 Carbon Gen 10 i7 16GB 512GB Windows 11 Pro", True),
    ("Nintendo Switch", "Nintendo Switch Lite Konsole", False),
])
def test_title_matches_search(query, title, expected):
    assert _title_matches_search(title, query, EXCLUDE) is expected


def test_phone_ram_from_aspects_does_not_split_groups():
    aspects = {"speicherkapazität": "128 GB", "arbeitsspeicher": "6 GB"}
    assert spec_key_from_aspects("Apple iPhone 13 Pro", aspects) == "128GB"


def test_laptop_ram_from_aspects():
    aspects = {"ssd-speicherkapazität": "512 GB", "arbeitsspeichergröße": "16 GB"}
    assert spec_key_from_aspects("ThinkPad X1", aspects) == "16GB+512GB"


@pytest.mark.parametrize("name,expected", [
    ("Handys & Smartphones", "Мобільні телефони й смартфони"),
    ("Handy-Zubehör", "Аксесуари для телефонів"),
    ("Retro-Konsolen", "Ретро консолі"),
    ("Unbekannte Kategorie", "Unbekannte Kategorie"),
])
def test_category_translation(name, expected):
    assert category_label(name) == expected