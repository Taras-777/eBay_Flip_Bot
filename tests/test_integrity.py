"""
Цілісність коду: кожне ім'я, до якого звертається модуль, має існувати.
Саме цей тест ловить помилки на кшталт випадково видаленої функції, яка
ще десь викликається (їх інакше видно лише під час роботи бота).
"""
import ast
import builtins
import importlib
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["settings", "textparse", "db", "ebay_api", "market", "panel", "access",
           "notifications", "handlers", "scheduler", "main"]


def undefined_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - defined)


@pytest.mark.parametrize("module", MODULES)
def test_no_undefined_names(module):
    assert undefined_names(os.path.join(ROOT, f"{module}.py")) == []


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_imported_names_exist():
    """`from X import Y` — Y справді є в модулі X."""
    for module in MODULES:
        tree = ast.parse(open(os.path.join(ROOT, f"{module}.py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in MODULES:
                source = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(source, alias.name), f"{module}: {node.module}.{alias.name} не існує"