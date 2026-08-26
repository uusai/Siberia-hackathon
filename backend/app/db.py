"""Пул соединений с PostgreSQL.

Заменяет прежние вызовы psql подпроцессом. Три причины, все измеренные:

1. СТАБИЛЬНОСТЬ. Сервер стенда терял примерно половину НОВЫХ соединений
   (8 попыток подряд: 4 успеха по ~1.3 с, 4 обрыва по 20 с с
   "server closed the connection unexpectedly"), при том что на кластере
   было занято 8 слотов из 100. Прежняя схема поднимала новый коннект на
   каждый запрос, то есть каждый вопрос был подбрасыванием монеты. Пул
   держит соединения открытыми, так что за коннект платит только первый
   запрос.

2. КОДИРОВКА. psql получал SQL аргументом командной строки, а Windows
   конвертирует аргументы в ANSI-кодовую страницу, где нет кириллицы:
   `WHERE degree = 'бакалавриат'` доезжал как `'???????????'` и запрос
   молча возвращал ноль строк вместо ошибки. Драйвер передаёт всё по
   протоколу в UTF-8.

3. ПАРАМЕТРЫ. Настоящая привязка на стороне сервера вместо подстановок
   psql через -v/:'name', которые ломались и на кодировке, и на -c.

Пул на каждую схему свой: у чат-пути search_path=assistant, у авторизации
search_path=auth. Так изоляция схемы auth держится на уровне соединения, а
не только на проверках в security.py.
"""

import os
import threading
import time

import psycopg
from psycopg import conninfo as _conninfo_mod
from psycopg_pool import ConnectionPool, PoolTimeout


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

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

STATEMENT_TIMEOUT_MS = int(os.getenv("SQL_STATEMENT_TIMEOUT_MS", "5000"))

# Кластер общий с другими командами (max_connections=100) — держим пул
# маленьким и не занимаем чужие слоты.
POOL_MIN = int(os.getenv("PG_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("PG_POOL_MAX", "4"))

# Сколько ждать установки соединения и сколько — свободного слота в пуле.
CONNECT_TIMEOUT_S = int(os.getenv("PG_CONNECT_TIMEOUT_S", "10"))
POOL_WAIT_S = float(os.getenv("PG_POOL_WAIT_S", "20"))

# Сколько раз повторить запрос при обрыве соединения. Пул уже подменяет
# мёртвые соединения, но стенд рвёт связь и посреди работы.
RETRIES = int(os.getenv("PG_RETRIES", "2"))
RETRY_PAUSE_S = float(os.getenv("PG_RETRY_PAUSE_S", "0.5"))

_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


class DBUnavailable(Exception):
    """Не удалось выполнить запрос: БД недоступна или рвёт соединение."""


def _conninfo(schema: str) -> str:
    return _conninfo_mod.make_conninfo(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=CONNECT_TIMEOUT_S,
        client_encoding="UTF8",
        options=f"-c search_path={schema} -c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


def get_pool(schema: str) -> ConnectionPool:
    """Возвращает (создавая при первом обращении) пул для указанной схемы."""
    pool = _pools.get(schema)
    if pool is not None:
        return pool

    with _pools_lock:
        pool = _pools.get(schema)
        if pool is None:
            pool = ConnectionPool(
                _conninfo(schema),
                min_size=POOL_MIN,
                max_size=POOL_MAX,
                timeout=POOL_WAIT_S,
                # Пересоздаём соединения, которые сервер успел уронить,
                # вместо того чтобы отдать вызывающему битый коннект.
                check=ConnectionPool.check_connection,
                # Стенд рвёт долгоживущие соединения — обновляем их сами.
                max_lifetime=float(os.getenv("PG_MAX_LIFETIME_S", "600")),
                name=f"pool-{schema}",
                open=True,
            )
            _pools[schema] = pool
    return pool


def _run(schema: str, sql: str, params, *, fetch: bool):
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            with get_pool(schema).connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall() if fetch and cur.description else []
        except (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout) as e:
            # Обрыв соединения или нехватка слотов — имеет смысл повторить.
            last_error = e
            if attempt < RETRIES:
                time.sleep(RETRY_PAUSE_S * (attempt + 1))
    raise DBUnavailable(str(last_error).strip() or "нет связи с БД")


def fetch_all(schema: str, sql, params=None) -> list[tuple]:
    """Выполняет SELECT и возвращает все строки. Бросает DBUnavailable.

    sql — строка либо psycopg.sql.Composable (когда запрос собирается из
    имён таблиц/колонок и их нужно безопасно заэкранировать).
    """
    return _run(schema, sql, params, fetch=True)


def execute(schema: str, sql: str, params=None) -> None:
    """Выполняет запрос без чтения результата. Бросает DBUnavailable."""
    _run(schema, sql, params, fetch=False)


def close_all() -> None:
    """Закрывает все пулы (для тестов и корректной остановки)."""
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
