"""Сброс авторизации из командной строки.

Зачем: на другой машине config.yaml уже создан со случайным паролем,
и «забыл пароль» превращается в ручную правку YAML. Теперь достаточно
запустить exe с флагом:

    VHS-Streamer.exe --reset-auth   # вернуть admin/admin
    VHS-Streamer.exe --no-auth      # вообще отключить вход

Правка выполняется до запуска сервера, приложение сразу завершается.
"""

from __future__ import annotations

import io
from pathlib import Path

import yaml

DEFAULT_USER = {"username": "admin", "password": "admin", "role": "admin"}


def apply(config_path: Path | str, reset_auth: bool = False,
          no_auth: bool = False) -> dict:
    """Правит config.yaml. Возвращает {'changed': bool, 'message': str}."""
    config_path = Path(config_path)
    if not config_path.exists():
        return {"changed": False,
                "message": f"Файл не найден: {config_path}"}

    with io.open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        cfg = {}

    security = cfg.setdefault("security", {})
    actions = []

    if reset_auth:
        security["users"] = [dict(DEFAULT_USER)]
        actions.append("пользователи сброшены на admin/admin")

    if no_auth:
        security["auth_enabled"] = False
        actions.append("вход отключён (auth_enabled: false)")

    if not actions:
        return {"changed": False, "message": "Нечего менять"}

    with io.open(config_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))

    return {"changed": True, "message": "; ".join(actions)}


def handle_argv(argv: list[str], config_path: Path | str) -> bool:
    """Если в аргументах есть флаги — применяет и возвращает True."""
    reset_auth = "--reset-auth" in argv
    no_auth = "--no-auth" in argv
    if not (reset_auth or no_auth):
        return False

    result = apply(config_path, reset_auth=reset_auth, no_auth=no_auth)
    line = "=" * 60
    print(f"\n{line}\n  {result['message']}\n  Файл: {config_path}\n{line}\n",
          flush=True)
    return True
