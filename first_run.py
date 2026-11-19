"""Первый запуск: создание config.yaml с безопасными значениями.

Зачем: при переносе на другую машину рядом с .exe может не быть config.yaml.
Раньше он копировался из примера с паролем CHANGE-ME — оператор не знал,
как войти, а в конфиге оставалась камера-заглушка, которая не работает.

Теперь при первом старте:
  * создаём config.yaml из config.example.yaml;
  * генерируем случайный пароль администратора и печатаем его в консоль;
  * генерируем server.secret_key;
  * возвращаем учётные данные, чтобы приложение показало их в баннере.
"""

from __future__ import annotations

import logging
import secrets
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def ensure_config(config_path: Path | str, example_path: Path | str) -> dict | None:
    """Создаёт config.yaml при первом запуске. None — если файл уже есть."""
    config_path = Path(config_path)
    example_path = Path(example_path)

    if config_path.exists():
        return None
    if not example_path.exists():
        raise FileNotFoundError(f"Нет ни config.yaml, ни {example_path}")

    shutil.copy2(example_path, config_path)

    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.error("Не удалось разобрать config.example.yaml: %s", exc)
        return None

    password = secrets.token_urlsafe(9)
    security = cfg.setdefault("security", {})
    users = security.setdefault("users", [])
    admin = next((u for u in users if str(u.get("role")) == "admin"), None)
    if admin is None:
        admin = {"username": "admin", "role": "admin"}
        users.insert(0, admin)
    admin.setdefault("username", "admin")
    admin["password"] = password

    server = cfg.setdefault("server", {})
    server["secret_key"] = secrets.token_hex(32)

    config_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    info = {"username": admin["username"], "password": password, "path": str(config_path)}
    logger.warning(
        "Первый запуск: создан %s, логин %s, пароль %s",
        config_path, info["username"], password,
    )
    return info


def print_first_run_banner(info: dict | None, url: str = "") -> None:
    """Печатает заметный баннер с учётными данными первого запуска."""
    if not info:
        return
    line = "=" * 66
    print(
        f"\n{line}\n"
        f"  ПЕРВЫЙ ЗАПУСК — создан файл настроек\n"
        f"  {info['path']}\n\n"
        f"  Логин:  {info['username']}\n"
        f"  Пароль: {info['password']}\n\n"
        f"  Запишите пароль и смените его в разделе «Настройки».\n"
        f"{f'  Панель: {url}' if url else ''}\n"
        f"{line}\n",
        flush=True,
    )
