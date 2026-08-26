"""Общая обвязка операторских скриптов: .env, подключение, повторы.

Раньше каждый скрипт нёс свою копию _load_dotenv() и свой цикл повторов
(см. seed_auth_users.py). Скриптов стало шесть, и копий столько же быть не
должно — иначе правка таймаута в одном месте молча расходится с остальными.

Приложение (backend/app/db.py) сюда не ходит и от этого модуля не зависит:
там пул на каждую схему и асинхронный путь, здесь — короткоживущие
соединения операторских скриптов. Общее у них только одно — стенд рвёт
примерно половину НОВЫХ соединений, поэтому connect() здесь всегда с
повторами.
"""

import os
import sys
import time

import psycopg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Стенд теряет часть новых соединений (замер: 4 обрыва из 8 попыток, по 20 с
# каждый). Пять попыток с нарастающей паузой закрывают наблюдаемые случаи.
CONNECT_ATTEMPTS = 5
CONNECT_TIMEOUT_S = 15


def load_dotenv(path: str | None = None) -> None:
    """Подхватывает .env из корня репозитория, не затирая заданное снаружи."""
    if path is None:
        path = os.path.join(REPO_ROOT, ".env")
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


def conninfo() -> str:
    load_dotenv()
    missing = [
        name
        for name in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
        if not os.getenv(name)
    ]
    if missing:
        raise SystemExit(
            f"Не заданы переменные окружения: {', '.join(missing)} (см. .env.example)."
        )
    return psycopg.conninfo.make_conninfo(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=CONNECT_TIMEOUT_S,
        client_encoding="UTF8",
        # search_path задаётся параметром соединения, а не отдельным SET.
        # Разница существенная: SET открыл бы транзакцию сразу после connect(),
        # а psycopg не даёт менять conn.read_only внутри транзакции — скрипты,
        # которые просят режим «только чтение», падали бы на ровном месте.
        options="-c search_path=assistant,public",
    )


def connect(autocommit: bool = False) -> psycopg.Connection:
    """Соединение с повторами. Бросает SystemExit, если связи нет вовсе."""
    info = conninfo()
    last = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return psycopg.connect(info, autocommit=autocommit)
        except psycopg.Error as e:
            last = e
            if attempt < CONNECT_ATTEMPTS - 1:
                print(
                    f"  соединение не удалось ({type(e).__name__}), "
                    f"попытка {attempt + 2} из {CONNECT_ATTEMPTS}",
                    file=sys.stderr,
                )
                time.sleep(1.5 * (attempt + 1))
    raise SystemExit(f"Не удалось подключиться к БД: {str(last).strip()}")


def parse_apply_flag(argv: list[str]) -> bool:
    """--apply включает запись. По умолчанию скрипты только показывают план.

    Осознанно наоборот к привычному --dry-run: база общая и боевая для
    демо, случайный запуск не должен ничего в ней менять.
    """
    return "--apply" in argv


def banner(title: str, apply: bool) -> None:
    mode = "ЗАПИСЬ" if apply else "ПРОСМОТР (запись выключена, добавьте --apply)"
    print("=" * 72)
    print(f"  {title}")
    print(f"  Режим: {mode}")
    print("=" * 72)
