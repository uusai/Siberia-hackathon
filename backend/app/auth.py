"""Аутентификация: bcrypt-хеши паролей + JWT-токены.

Учётные записи лежат в отдельной схеме `auth` (таблица auth.users), намеренно
недоступной чат-агенту: ai_agent.get_db_schema() и get_db_relationships()
фильтруют table_schema = 'assistant', а security.ALLOWED_TABLES не содержит ни
одной таблицы схемы auth.

ОБЛАСТЬ ПРИМЕНЕНИЯ: регистрация (signup) сознательно НЕ реализована — это
осознанное ограничение объёма для хакатона. Пользователи заводятся один раз
скриптом backend/scripts/seed_auth_users.py и никак иначе.
"""

import asyncio
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

# Версия состава токена. Поднимать при КАЖДОМ изменении набора полей.
#
# Зачем: когда в токен добавили student_id, у всех, кто вошёл раньше,
# в localStorage остался токен без него — валидный по подписи и сроку, но
# без личности. Симптом получался обманчивый: ассистент отвечал «не знаю,
# как вас зовут» и «пар нет», хотя данные в базе были на месте. Теперь
# такой токен отвергается сразу, фронтенд по 401 отправляет на вход и
# выдаёт свежий.
#
# 1 — sub/role. 2 — плюс student_id/teacher_id.
TOKEN_VERSION = 2

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


def create_access_token(
    username: str,
    role: str,
    student_id: int | None = None,
    teacher_id: int | None = None,
) -> str:
    """Токен с личностью пользователя.

    student_id кладём в токен, потому что дальше он уходит в сессионную
    переменную app.student_id, по которой фильтруются личные вьюхи my_*.
    Источник — только БД на момент логина: из тела запроса эти поля не
    принимаются никогда.
    """
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "ver": TOKEN_VERSION,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    if student_id is not None:
        payload["student_id"] = student_id
    if teacher_id is not None:
        payload["teacher_id"] = teacher_id
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
        "SELECT id, username, password_hash, role, student_id, teacher_id "
        "FROM users WHERE username = %s LIMIT 1"
    )

    try:
        rows = db.fetch_all("auth", query, (username,), read_only=True)
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
        "student_id": row[4],
        "teacher_id": row[5],
    }


async def aget_user_by_username(username: str) -> dict | None:
    """Асинхронный вариант get_user_by_username(). Контракт тот же."""
    query = (
        "SELECT id, username, password_hash, role, student_id, teacher_id "
        "FROM users WHERE username = %s LIMIT 1"
    )

    try:
        rows = await db.afetch_all("auth", query, (username,), read_only=True)
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
        "student_id": row[4],
        "teacher_id": row[5],
    }


async def averify_password(password: str, password_hash: str) -> bool:
    """bcrypt в рабочем потоке.

    Проверка пароля намеренно медленная (cost 12 — сотни миллисекунд), и
    это чистая нагрузка на процессор. В событийном цикле она заблокировала
    бы всех остальных на время каждого логина.
    """
    return await asyncio.to_thread(verify_password, password, password_hash)


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

    if payload.get("ver") != TOKEN_VERSION:
        # Токен выдан прежней версией приложения: подпись верна, но состав
        # полей устарел. Молча работать с ним нельзя — получится ассистент,
        # который «не знает», как зовут вошедшего.
        raise HTTPException(
            status_code=401,
            detail="Сессия устарела, войдите заново",
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

    return {
        "username": username,
        "role": role,
        "student_id": payload.get("student_id"),
        "teacher_id": payload.get("teacher_id"),
    }
