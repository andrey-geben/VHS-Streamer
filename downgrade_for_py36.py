#!/usr/bin/env python3
"""
Понижение синтаксиса Python до совместимого с Python 3.6.

Использовать ОДИН РАЗ перед сборкой Docker-образа для Jetson.
Изменяет только указанную папку — оригинальный проект не трогает.

Использование:
    python downgrade_for_py36.py <путь_к_копии_проекта>

Например:
    python downgrade_for_py36.py VHS-Streamer-Jetson/VHS-Streamer

Что делает:
    * удаляет "from __future__ import annotations"
    * X | None     →  Optional[X]
    * list[X]      →  List[X]
    * dict[K, V]   →  Dict[K, V]
    * set[X]       →  Set[X]
    * tuple[...]   →  Tuple[...]
    * frozenset[X] →  FrozenSet[X]
    * добавляет "from typing import ..." где нужно

Безопасность:
    * каждое изменение проверяется через compile();
    * если результат не компилируется — файл НЕ меняется,
      выводится предупреждение.
"""
import re
import sys
from pathlib import Path


# Регулярка для X | None → Optional[X]
RE_UNION_NONE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_\.]*)\s*\|\s*None\b"
)

# Замены типов (порядок важен)
TYPE_SUBSTITUTIONS = [
    (re.compile(r"\blist\s*\["),      "List["),
    (re.compile(r"\bdict\s*\["),      "Dict["),
    (re.compile(r"\bset\s*\["),       "Set["),
    (re.compile(r"\btuple\s*\["),     "Tuple["),
    (re.compile(r"\bfrozenset\s*\["), "FrozenSet["),
]

# Что нужно импортировать из typing
TYPING_NAMES = {
    "Optional", "List", "Dict", "Set", "Tuple", "FrozenSet", "Union", "Any",
}


# ----------------------------------------------------------------------
def remove_future_annotations(text: str) -> str:
    """Убирает строку from __future__ import annotations."""
    return re.sub(
        r"^from __future__ import annotations\s*\n",
        "",
        text,
        flags=re.MULTILINE,
    )


def apply_replacements(text: str) -> str:
    """Все замены типов."""
    # 1. X | None → Optional[X]
    text = RE_UNION_NONE.sub(r"Optional[\1]", text)
    # 2. list[ → List[, dict[ → Dict[ и т.д.
    for pattern, repl in TYPE_SUBSTITUTIONS:
        text = pattern.sub(repl, text)
    return text


def add_typing_import(text: str) -> str:
    """
    Добавляет from typing import ... если в тексте
    используются имена Optional, List, Dict, Set, Tuple, FrozenSet, Union.
    """
    used = set(re.findall(
        r"\b(Optional|List|Dict|Set|Tuple|FrozenSet|Union|Any)\b",
        text,
    ))
    used &= TYPING_NAMES
    if not used:
        return text

    # Есть ли уже from typing import ...
    m = re.search(r"^from typing import ([^\n]+)", text, flags=re.MULTILINE)
    if m:
        existing = {n.strip() for n in m.group(1).split(",")}
        combined = existing | used
        new_line = "from typing import " + ", ".join(sorted(combined))
        return text[:m.start()] + new_line + text[m.end():]

    # Нет — вставляем после последнего import
    lines = text.splitlines(keepends=True)
    last_import_idx = -1
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            last_import_idx = i

    new_line = "from typing import " + ", ".join(sorted(used)) + "\n"
    if last_import_idx < 0:
        return new_line + text
    lines.insert(last_import_idx + 1, new_line)
    return "".join(lines)


def downgrade_text(text: str) -> str:
    """Все преобразования."""
    text = remove_future_annotations(text)
    text = apply_replacements(text)
    text = add_typing_import(text)
    return text


# ----------------------------------------------------------------------
def process_file(path: Path) -> str:
    """
    Обрабатывает один файл.
    Возвращает: 'changed' | 'unchanged' | 'error'.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"    ! не удалось прочитать: {e}")
        return "error"

    try:
        new_text = downgrade_text(original)
    except Exception as e:
        print(f"    ! ошибка преобразования: {e}")
        return "error"

    if new_text == original:
        return "unchanged"

    # Проверяем, что результат компилируется
    try:
        compile(new_text, str(path), "exec")
    except SyntaxError as e:
        print(f"    ! результат не компилируется: {e}")
        print(f"      Файл НЕ изменён.")
        return "error"

    try:
        path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        print(f"    ! не удалось записать: {e}")
        return "error"

    return "changed"


# ----------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    root = Path(sys.argv[1])
    if not root.exists():
        print(f"Путь не существует: {root}")
        sys.exit(1)

    if root.is_file():
        files = [root]
    else:
        files = sorted(root.rglob("*.py"))

    # Исключаем кеш
    files = [
        f for f in files
        if "__pycache__" not in f.parts and ".venv" not in f.parts
    ]

    print(f"Обрабатываю {len(files)} файлов в {root}")
    print()

    stats = {"changed": 0, "unchanged": 0, "error": 0}

    for f in files:
        rel = f.relative_to(root)
        print(f"  {rel}")
        result = process_file(f)
        stats[result] += 1
        if result == "changed":
            print(f"    ✓ изменён")
        elif result == "error":
            print(f"    ✗ ошибка")

    print()
    print(f"Итого: изменено {stats['changed']}, "
          f"без изменений {stats['unchanged']}, "
          f"ошибок {stats['error']}")

    if stats["error"] > 0:
        print()
        print("⚠ Некоторые файлы не были обработаны.")
        print("  Проверьте вручную — вывод выше.")
        sys.exit(1)


if __name__ == "__main__":
    main()