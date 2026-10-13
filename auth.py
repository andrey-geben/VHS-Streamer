"""
Аутентификация и авторизация.

Две роли:
    - admin  — полный доступ (настройки, архив, все действия);
    - viewer — только просмотр камер и архива.

Поддерживаются два способа аутентификации:
    1. Сессия (cookie) — для веб-UI;
    2. API-токен — заголовок `X-API-Token` или ?token=... для внешних систем.
"""
from functools import wraps
from flask import (
    session, request, redirect, url_for, jsonify,
    current_app, abort,
)


# ----------------------------------------------------------------------
# Проверка логина/пароля
# ----------------------------------------------------------------------
def _find_user(username: str, password: str) -> dict | None:
    """Ищет пользователя в конфиге. Возвращает dict с ролью или None."""
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    if not security.get("auth_enabled", True):
        # auth выключен — считаем всех админами
        return {"username": username or "anonymous", "role": "admin"}
    for u in security.get("users", []):
        if u.get("username") == username and u.get("password") == password:
            return {"username": u["username"], "role": u.get("role", "viewer")}
    return None


def verify_api_token(token: str) -> bool:
    """Проверяет API-токен."""
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    expected = security.get("api_token")
    return bool(expected) and token == expected


# ----------------------------------------------------------------------
# Внутренний хелпер: определить текущего пользователя
# ----------------------------------------------------------------------
def _current_user() -> dict | None:
    """Возвращает {'username':..., 'role':...} или None."""
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    if not security.get("auth_enabled", True):
        return {"username": "anonymous", "role": "admin"}

    # API-токен
    token = request.headers.get("X-API-Token") or request.args.get("token")
    if token and verify_api_token(token):
        return {"username": "api", "role": "admin"}

    # Сессия
    user = session.get("user")
    if user:
        return user
    return None


# ----------------------------------------------------------------------
# Декораторы
# ----------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if user is None:
            # Для API / видеопотока — 401, для UI — редирект на /login
            accept = request.headers.get("Accept", "")
            if (
                request.path.startswith("/api/")
                or "video_feed" in request.path
                or "application/json" in accept
            ):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        # Сохраним пользователя в g-подобной структуре через flask.g
        from flask import g
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        from flask import g
        if getattr(g, "user", {}).get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ----------------------------------------------------------------------
# Публичные функции для роутов
# ----------------------------------------------------------------------
def do_login(username: str, password: str) -> bool:
    """Проверяет креды и создаёт сессию."""
    user = _find_user(username, password)
    if not user:
        return False
    session["user"] = user
    session.permanent = True
    return True


def do_logout():
    session.pop("user", None)


def get_current_user() -> dict | None:
    return _current_user()