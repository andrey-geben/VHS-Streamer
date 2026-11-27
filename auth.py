"""
Аутентификация и авторизация.

Две роли:
    - admin  — полный доступ (настройки, архив, все действия, все камеры);
    - viewer — просмотр разрешённых камер и их архива.

Разграничение доступа к камерам:
    - В config.yaml у пользователя можно указать поле `cameras`:
        cameras: ["*"]                   — все камеры (по умолчанию, если поле отсутствует)
        cameras: ["cam_1", "cam_2"]      — только перечисленные
        cameras: []                      — ни одной
    - Для admin `cameras` игнорируется — админ видит всё.
    - API-токен = admin (доступ ко всему).

Поддерживаются два способа аутентификации:
    1. Сессия (cookie) — для веб-UI;
    2. API-токен — заголовок `X-API-Token` или ?token=... для внешних систем.
"""
from functools import wraps

import vhs_sso
from flask import (
    session, request, redirect, url_for, jsonify,
    current_app, abort, g,
)

import secrets as _secrets
import time as _time

# ----------------------------------------------------------------------
# Проверка логина/пароля
# ----------------------------------------------------------------------
def _find_user(username: str, password: str) -> dict | None:
    """Ищет пользователя в конфиге. Возвращает dict с ролью и cameras или None."""
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    if not security.get("auth_enabled", True):
        # auth выключен — считаем всех админами (доступ ко всему)
        return {
            "username": username or "anonymous",
            "role": "admin",
            "cameras": None,
        }
    for u in security.get("users", []):
        if u.get("username") == username and u.get("password") == password:
            return {
                "username": u["username"],
                "role": u.get("role", "viewer"),
                # None = все камеры; [] = ни одной; [id,...] = только эти
                "cameras": u.get("cameras"),
            }
    return None


def verify_api_token(token: str) -> bool:
    """Проверяет API-токен."""
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    expected = security.get("api_token")
    return bool(expected) and token == expected

# ----------------------------------------------------------------------
# A.3: Web-токены для MediaMTX authMethod: http (WHEP/WebRTC из iframe)
# ----------------------------------------------------------------------
_MTX_TOKEN_TTL = 12 * 3600
_MTX_TOKEN_SALT = "vhs-mtx-token"


def _mtx_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.secret_key, salt=_MTX_TOKEN_SALT)


def create_mtx_token(username: str) -> str:
    """Подписанный токен для MediaMTX.

    Раньше токен жил в памяти процесса: после перезапуска приложения iframe
    приходил со «старым» токеном, проверка не находила его и превью
    оставались чёрными (mediamtx_auth DENY). Подпись проверяется по
    secret_key, поэтому токен переживает рестарт.
    """
    return _mtx_serializer().dumps({"u": username})


def revoke_mtx_token(token: str) -> None:
    """Подписанный токен нельзя отозвать — только дождаться срока."""
    return None


def _lookup_mtx_token(token: str) -> dict | None:
    """Проверяет подпись и срок. Возвращает {'username': ...} или None."""
    from itsdangerous import BadSignature, SignatureExpired
    try:
        data = _mtx_serializer().loads(token, max_age=_MTX_TOKEN_TTL)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(data, dict) and data.get("u"):
        return {"username": data["u"]}
    return None


def get_mtx_token() -> str | None:
    """Текущий валидный токен из сессии (для шаблонов, шаг A.4).

    После перезапуска Streamer или смены SECRET_KEY старый токен в cookie
    может быть подписан уже недействительным ключом. В этом случае
    возвращаем None, чтобы вызывающий код создал новый токен.
    """
    token = session.get("mtx_token")
    if not token:
        return None
    if _lookup_mtx_token(token) is None:
        session.pop("mtx_token", None)
        return None
    return token


def _get_user_entry(username: str) -> dict | None:
    """Сырая запись пользователя из config.yaml.security.users."""
    cfg = current_app.config.get("APP_CONFIG", {})
    for u in cfg.get("security", {}).get("users", []):
        if u.get("username") == username:
            return u
    return None


def _role_for_username(username: str) -> str:
    return (_get_user_entry(username) or {}).get("role", "viewer")


def _cameras_for_username(username: str):
    return (_get_user_entry(username) or {}).get("cameras")


def resolve_mtx_credentials(user: str, password: str) -> dict | None:
    """
    Проверяет user/password, пришедшие от MediaMTX authMethod: http.
    Возвращает dict с 'kind' или None.
    """
    if not user or not password:
        return None

    # 1. Web-токен (создан при login в Flask)
    rec = _lookup_mtx_token(password)
    if rec and rec["username"] == user:
        return {
            "kind": "web_token",
            "username": user,
            "role": _role_for_username(user),
            "cameras": _cameras_for_username(user),
        }

    # 2. Stream password (vhs_<stream_id>:<read_password>)
    if user.startswith("vhs_"):
        stream_id = user[len("vhs_"):]
        cfg = current_app.config.get("APP_CONFIG", {})
        for s in cfg.get("streams", []):
            if (
                s.get("id") == stream_id
                and s.get("enabled", True)
                and s.get("read_password") == password
            ):
                return {
                    "kind": "stream_password",
                    "username": user,
                    "stream_id": stream_id,
                    "stream_path": s.get("path"),
                    "camera_id": s.get("camera_id"),
                    "role": "viewer",
                    "cameras": None,
                }
        return None

    # 3. Обычный пользователь из config.yaml
    found = _find_user(user, password)
    if found:
        return {"kind": "config_user", **found}
    return None

# ----------------------------------------------------------------------
# Хелперы разграничения доступа к камерам
# ----------------------------------------------------------------------
def get_user_cameras(user: dict | None) -> list[str] | None:
    """
    Возвращает список разрешённых камер для пользователя:
        None       — доступ ко всем камерам;
        []         — ни к одной;
        [id1, id2] — только к перечисленным.

    Логика:
        - admin → None (всегда все);
        - поле `cameras` отсутствует → None (обратная совместимость);
        - `cameras` не список → None (защита от опечаток в config);
        - в списке есть "*" → None (все);
        - иначе — список ID.
    """
    if not user:
        return None
    if user.get("role") == "admin":
        return None
    cams = user.get("cameras")
    if cams is None:
        return None  # поля нет — все камеры (обратная совместимость)
    if not isinstance(cams, list):
        return None
    if "*" in cams:
        return None
    return [str(c) for c in cams if c]


def can_access_camera(cam_id: str, user: dict | None = None) -> bool:
    """
    Проверяет, разрешён ли пользователю доступ к камере `cam_id`.
    Если user не передан — берётся текущий g.user.
    """
    if user is None:
        user = getattr(g, "user", None)
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    cams = get_user_cameras(user)
    if cams is None:
        return True
    return str(cam_id) in cams


def can_access_all(user: dict | None = None) -> bool:
    """True, если пользователь имеет доступ ко всем камерам."""
    if user is None:
        user = getattr(g, "user", None)
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return get_user_cameras(user) is None


def _refresh_from_config(user: dict) -> dict:
    """
    Обновляет role и cameras из config.yaml по username.
    Нужно, чтобы изменения в config.yaml (без перелогина) применялись
    при следующем запросе — например, если админ урезал пользователю
    список камер.
    """
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    if not security.get("auth_enabled", True):
        return user
    username = user.get("username")
    if not username or username in ("api", "anonymous"):
        return user
    for u in security.get("users", []):
        if u.get("username") == username:
            return {
                "username": u["username"],
                "role": u.get("role", "viewer"),
                "cameras": u.get("cameras"),
            }
    # Пользователь удалён из config — оставляем сессию как есть,
    # но пометить его невалидным должен вызывающий код (сейчас не делаем).
    return user


# ----------------------------------------------------------------------
# Внутренний хелпер: определить текущего пользователя
# ----------------------------------------------------------------------
def _current_user() -> dict | None:
    """Возвращает {'username':..., 'role':..., 'cameras':...} или None."""
    cfg = current_app.config.get("APP_CONFIG", {})
    security = cfg.get("security", {})
    if not security.get("auth_enabled", True):
        return {"username": "anonymous", "role": "admin", "cameras": None}

    # API-токен → admin (доверенный системный клиент)
    token = (
        request.headers.get("X-VHS-Key")
        or request.headers.get("X-API-Token")
        or request.args.get("token")
    )
    if token and verify_api_token(token):
        return {"username": "api", "role": "admin", "cameras": None}

    # Сессия
    user = session.get("user")
    if user:
        return _refresh_from_config(user)

    # Общий SSO-cookie между VHS-модулями (один хост, разные порты)
    secret = (security.get("sso_secret") or "").strip()
    sso_user = vhs_sso.read(secret)
    if sso_user:
        refreshed = _find_user(sso_user["username"], None) if False else {
            "username": sso_user["username"],
            "role": sso_user.get("role", "viewer"),
            "cameras": None,
        }
        session["user"] = refreshed
        return refreshed
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
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
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
    # A.3: токен для MediaMTX (WHEP в iframe, шаг A.4)
    session["mtx_token"] = create_mtx_token(username)
    return True


def do_logout():
    token = session.pop("mtx_token", None)
    if token:
        revoke_mtx_token(token)
    session.pop("user", None)


def get_current_user() -> dict | None:
    return _current_user()