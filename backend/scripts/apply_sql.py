"""Применяет .sql файл к базе. Замена `psql -f`, которого на стенде нет.

    python backend/scripts/apply_sql.py backend/sql/005_provenance.sql --apply

Без --apply файл только читается и показывается сводка — чтобы случайный
запуск не менял общую базу.

Файл исполняется ЦЕЛИКОМ одним execute() в одной транзакции: psycopg3 это
умеет, когда в запросе нет параметров. Разбивать по ';' самостоятельно
намеренно не стали — это ломается на dollar-quoted телах и строковых
литералах с точкой с запятой, а выигрыш только в детальности лога.

Миграции проекта пишутся идемпотентно (CREATE TABLE IF NOT EXISTS,
ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE VIEW), поэтому повторный
запуск безопасен.
"""

import os
import re
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

# Разрушающие конструкции в миграциях этого проекта запрещены: база общая и
# боевая для демо, а восстановить удалённое неоткуда. Проверка грубая (по
# тексту), но именно поэтому её невозможно случайно обойти опечаткой.
_DESTRUCTIVE = re.compile(
    r"\b(drop\s+(table|database|schema|column)|truncate|delete\s+from)\b",
    re.IGNORECASE,
)


def main(argv: list[str]) -> int:
    paths = [a for a in argv[1:] if not a.startswith("--")]
    apply = _common.parse_apply_flag(argv)

    if not paths:
        print("Укажите путь к .sql файлу.", file=sys.stderr)
        return 2

    for path in paths:
        if not os.path.isfile(path):
            print(f"Файл не найден: {path}", file=sys.stderr)
            return 2

    _common.banner("Применение SQL-миграций", apply)

    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()

        hit = _DESTRUCTIVE.search(sql)
        if hit:
            print(
                f"[ОТКАЗ] {path}: обнаружена разрушающая конструкция "
                f"'{hit.group(0)}'. Миграции проекта только добавляют.",
                file=sys.stderr,
            )
            return 1

        statements = len([s for s in sql.split(";") if s.strip()])
        print(f"\n{path}: {len(sql)} символов, ~{statements} операторов")

        if not apply:
            print("  пропущено (нет --apply)")
            continue

        try:
            with _common.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
            print("  [OK] применено")
        except psycopg.Error as e:
            print(f"  [FAIL] {str(e).strip()}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
