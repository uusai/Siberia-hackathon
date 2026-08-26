"""Аутентификация: bcrypt-хеши паролей + JWT-токены.

Учётные записи лежат в отдельной схеме `auth` (таблица auth.users), намеренно
недоступной чат-агенту: ai_agent.get_db_schema() и get_db_relationships()
фильтруют table_schema = 'assistant', а security.ALLOWED_TABLES не содержит ни
одной таблицы схемы auth.

ОБЛАСТЬ ПРИМЕНЕНИЯ: регистрация (signup) сознательно НЕ реализована — это
осознанное ограничение объёма для хакатона. Пользователи заводятся один раз
скриптом backend/scripts/seed_auth_users.py и никак иначе.
"""

import datetime as dt
import os

import bcrypt
import jwt
import psycopg
from fastapi import Header, HTTPException

from . import db


def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


_load_dotenv()

# Параметры подключения к БД живут в db.py — здесь только авторизация.

JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 8

# bcrypt учитывает только первые 72 байта пароля и с версии 5.0 бросает
# ValueError на всё, что длиннее, вместо молчаливой обрезки. Обрезаем сами —
# одинаково при хешировании и при проверке, иначе длинный пароль не сойдётся.
_BCRYPT_MAX_BYTES = 72

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def _jwt_secret() -> str:
    """Секрет для подписи JWT.

    Значения по умолчанию нет и быть не должно: захардкоженный секрет означает
    токены, которые может подделать любой, кто видел исходники.
    """
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET не задан в окружении. Задайте его в .env (см. .env.example)."
        )
    return secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            _password_bytes(password), password_hash.encode("utf-8")
        )
    except ValueError:
        # Битый или усечённый хеш в БД — считаем пароль неверным, а не 500.
        return False


def create_access_token(username: str, role: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Проверяет подпись и срок действия. Бросает jwt.InvalidTokenError."""
    return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])


def get_user_by_username(username: str) -> dict | None:
    """Читает учётную запись из auth.users.

    username — пользовательский ввод, поэтому уходит параметром запроса (%s),
    а не склейкой строк. Пул для схемы auth свой, с search_path=auth (не
    assistant): этот запрос физически не может задеть учебные таблицы.
    """
    query = (
        "SELECT id, username, password_hash, role FROM users "
        "WHERE username = %s LIMIT 1"
    )

    try:
        rows = db.fetch_all("auth", query, (username,))
    except (db.DBUnavailable, psycopg.Error):
        raise HTTPException(status_code=503, detail="База данных недоступна")

    if not rows:
        return None

    row = rows[0]
    return {
        "id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "role": row[3],
    }


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI-зависимость: проверяет заголовок `Authorization: Bearer <token>`.

    Возвращает {username, role} из проверенного токена либо 401 на любой
    некорректный вход.
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация",
            headers=_UNAUTHORIZED_HEADERS,
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Ожидается заголовок вида 'Authorization: Bearer <token>'",
            headers=_UNAUTHORIZED_HEADERS,
        )

    try:
        payload = decode_access_token(token.strip())
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Срок действия токена истёк",
            headers=_UNAUTHORIZED_HEADERS,
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Недействительный токен",
            headers=_UNAUTHORIZED_HEADERS,
        )

    username = payload.get("sub")
    role = payload.get("role")
    if not username or not role:
        raise HTTPException(
            status_code=401,
            detail="Недействительный токен",
            headers=_UNAUTHORIZED_HEADERS,
        )

    return {"username": username, "role": role}
