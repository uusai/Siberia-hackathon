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

import asyncio
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

# Кластер общий с другими командами (max_connections=100), поэтому пул
# небольшой. Но min_size=1 был ошибкой: пул наращивает соединения по
# одному, и при одновременных запросах они выстраивались в очередь.
# Замер: 4 параллельных запроса по секунде занимали 4.1 с при min_size=1
# и 1.6 с при прогретом пуле. Плюс на этом сервере холодное соединение
# стоит от 1.3 до 20 секунд, так что держать их тёплыми выгодно вдвойне.
# Схем две (assistant и auth), то есть суммарно занимаем до 12 слотов.
POOL_MIN = int(os.getenv("PG_POOL_MIN", "3"))
POOL_MAX = int(os.getenv("PG_POOL_MAX", "6"))

# Сколько ждать установки соединения и сколько — свободного слота в пуле.
CONNECT_TIMEOUT_S = int(os.getenv("PG_CONNECT_TIMEOUT_S", "10"))
POOL_WAIT_S = float(os.getenv("PG_POOL_WAIT_S", "20"))

# Сколько раз повторить запрос при обрыве соединения. Пул уже подменяет
# мёртвые соединения, но стенд рвёт связь и посреди работы.
RETRIES = int(os.getenv("PG_RETRIES", "2"))
RETRY_PAUSE_S = float(os.getenv("PG_RETRY_PAUSE_S", "0.5"))

# Отдельный, заведомо короткий бюджет для health-проверки: см. ping().
PING_TIMEOUT_S = float(os.getenv("PG_PING_TIMEOUT_S", "3"))

# Синхронный пул: им пользуются операторские скрипты, CLI-режим
# ai_agent.main() и тесты, где event loop'а нет. Веб-путь ходит сюда же,
# но через asyncio.to_thread (см. afetch_all ниже): раньше эндпоинты были
# объявлены async def, а внутри вызывали блокирующий драйвер, из-за чего
# на время каждого запроса вставал весь событийный цикл и пользователи
# ждали не своей очереди к БД, а вообще всего — включая /health.
_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()

# Ошибки, при которых повтор осмыслен: обрыв соединения или нехватка слотов.
_RETRYABLE = (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout)


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


def _run(schema: str, sql: str, params, *, fetch: bool, session_vars=None,
         read_only: bool = False, with_columns: bool = False):
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            with get_pool(schema).connection() as conn:
                with conn.cursor() as cur:
                    if read_only:
                        # ОБЯЗАТЕЛЬНО первым запросом транзакции: Postgres не
                        # даёт вызвать SET TRANSACTION после того, как в ней
                        # уже что-то выполнялось.
                        cur.execute("SET TRANSACTION READ ONLY")
                    # Сессионные переменные (например app.student_id) ставим
                    # третьим аргументом true — это SET LOCAL, область
                    # действия ограничена текущей транзакцией. Соединение
                    # уходит обратно в пул чистым, так что чужой запрос
                    # значение не подхватит. В read-only транзакции такие
                    # вызовы разрешены: параметр сессии — не запись в данные.
                    for name, value in (session_vars or {}).items():
                        cur.execute(
                            "SELECT set_config(%s, %s, true)", (name, str(value))
                        )
                    cur.execute(sql, params)
                    if not (fetch and cur.description):
                        return ([], []) if with_columns else []
                    rows = cur.fetchall()
                    if with_columns:
                        # cur.description живёт только до выхода из блока —
                        # имена снимаем здесь, а не у вызывающего кода.
                        return [d.name for d in cur.description], rows
                    return rows
        except _RETRYABLE as e:
            # Обрыв соединения или нехватка слотов — имеет смысл повторить.
            last_error = e
            if attempt < RETRIES:
                time.sleep(RETRY_PAUSE_S * (attempt + 1))
    raise DBUnavailable(str(last_error).strip() or "нет связи с БД")


def fetch_all(schema: str, sql, params=None, session_vars=None,
              read_only: bool = False) -> list[tuple]:
    """Выполняет SELECT и возвращает все строки. Бросает DBUnavailable.

    sql — строка либо psycopg.sql.Composable (когда запрос собирается из
    имён таблиц/колонок и их нужно безопасно заэкранировать).

    session_vars — параметры сессии, выставляемые на время транзакции.
    Через них личные вьюхи my_* узнают, чьи данные показывать: значение
    приходит из проверенного токена, а не из SQL, который написала модель.

    read_only — выполнить в транзакции только для чтения. Так ходит весь
    путь чат-агента: у пользователя БД полные права на запись, и без
    этого единственной преградой между сгенерированным моделью запросом
    и DELETE оставался бы блеклист слов в security.py.
    """
    return _run(schema, sql, params, fetch=True, session_vars=session_vars,
                read_only=read_only)


def fetch_all_with_columns(schema: str, sql, params=None, session_vars=None,
                           read_only: bool = False) -> tuple[list[str], list[tuple]]:
    """То же, что fetch_all(), но возвращает ещё и имена колонок.

    Нужно чат-пути: раньше строки склеивались в текст «a|b|c» и уходили только
    в промпт модели, а до фронтенда доезжала одна проза. Таблицу в ответе он
    поэтому пытался выпарсить обратно из текста. Имена колонок есть в
    cur.description и всегда были — их просто выбрасывали.
    """
    return _run(schema, sql, params, fetch=True, session_vars=session_vars,
                read_only=read_only, with_columns=True)


def execute(schema: str, sql: str, params=None) -> None:
    """Выполняет запрос без чтения результата. Бросает DBUnavailable."""
    _run(schema, sql, params, fetch=False)


def close_all() -> None:
    """Закрывает синхронные пулы (для тестов и скриптов)."""
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


# ---------------------------------------------------------------------------
# Асинхронный путь — им пользуется веб-приложение
# ---------------------------------------------------------------------------
#
# Реализовано рабочим потоком поверх синхронного пула, а НЕ через
# AsyncConnectionPool. Причина проверена на этой машине: psycopg в
# асинхронном режиме отказывается работать на ProactorEventLoop, который
# на Windows стоит по умолчанию, и требует SelectorEventLoop. Переключить
# политику можно, но API для этого объявлен устаревшим в Python 3.14 и
# удаляется в 3.16, плюс сработает только если успеть выставить её до
# того, как uvicorn создаст цикл. Ставить демо на такую конструкцию не
# стоит.
#
# Для нашей нагрузки разницы нет: запрос к БД занимает ~0.5 с, а пул всё
# равно ограничен четырьмя соединениями, так что дальше четырёх потоков
# работа не разойдётся. Событийный цикл при этом свободен полностью —
# именно это и требовалось.

async def afetch_all(schema: str, sql, params=None, session_vars=None,
                     read_only: bool = False) -> list[tuple]:
    """Асинхронный SELECT. Аргументы те же, что у fetch_all()."""
    return await asyncio.to_thread(
        fetch_all, schema, sql, params, session_vars, read_only
    )


async def afetch_all_with_columns(
    schema: str, sql, params=None, session_vars=None, read_only: bool = False
) -> tuple[list[str], list[tuple]]:
    """Асинхронный вариант fetch_all_with_columns(). Контракт тот же."""
    return await asyncio.to_thread(
        fetch_all_with_columns, schema, sql, params, session_vars, read_only
    )


async def aexecute(schema: str, sql, params=None) -> None:
    """Асинхронный запрос без чтения результата."""
    await asyncio.to_thread(execute, schema, sql, params)


def ping(schema: str = "assistant") -> bool:
    """Жива ли БД. ОДНА попытка, без повторов.

    Через _run() ходить нельзя: он ретраит до RETRIES раз с паузами, то есть
    на упавшей базе отвечает до полуминуты. Этот вызов обслуживает /health,
    который фронтенд опрашивает каждые 25 секунд, — он обязан вернуться
    быстро и с ответом «нет», а не подвесить опрос.
    """
    try:
        with get_pool(schema).connection(timeout=PING_TIMEOUT_S) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        # Любая причина — обрыв, таймаут, нет слотов — для health одинакова.
        return False


async def aping(schema: str = "assistant") -> bool:
    """Асинхронный вариант ping()."""
    return await asyncio.to_thread(ping, schema)


async def aclose_all() -> None:
    """Закрывает пулы при остановке приложения."""
    await asyncio.to_thread(close_all)
