"""Общий SSO-cookie для локального контура VHS."""

from flask import request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "vhs_sso"
SALT = "vhs-sso"
MAX_AGE = 7 * 24 * 3600


def _serializer(secret: str):
    return URLSafeTimedSerializer(secret, salt=SALT)


def read(secret: str):
    if not secret:
        return None
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = _serializer(secret).loads(raw, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not data.get("username"):
        return None
    return {"username": str(data["username"]), "role": str(data.get("role", "admin"))}


def set_cookie(response, secret: str, username: str, role: str = "admin"):
    if not secret or not username:
        return response
    token = _serializer(secret).dumps({"username": username, "role": role})
    response.set_cookie(
        COOKIE_NAME, token, max_age=MAX_AGE, httponly=True,
        samesite="Lax", secure=request.is_secure, path="/",
    )
    return response


def clear_cookie(response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
