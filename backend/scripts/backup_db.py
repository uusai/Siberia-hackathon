"""Резервная копия схем assistant и auth в файлы.

    python backend/scripts/backup_db.py

pg_dump на стенде нет, поэтому выгружаем сами: по файлу .jsonl на таблицу
плюс manifest.json со счётчиками строк. Счётчики из манифеста — эталон для
проверки «ни одна существующая запись не удалена» (см.
backend/tests/test_data_integrity.py).

ВАЖНО: выгрузка содержит персональные данные (ФИО, паспорта, телефоны,
почты студентов и абитуриентов). Каталог backend/backups/ добавлен в
.gitignore и в репозиторий попадать не должен.

Скрипт только читает. Запись в БД он не делает вообще, поэтому флага
--apply у него нет.
"""

import datetime as dt
import decimal
import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

SCHEMAS = ("assistant", "auth")


def _default(value):
    """JSON не умеет date/datetime/Decimal — приводим к строке/числу."""
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return str(value)


def main() -> int:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(_common.REPO_ROOT, "backend", "backups", stamp)
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 72)
    print("  Резервная копия БД (только чтение)")
    print(f"  Каталог: {out_dir}")
    print("=" * 72)

    manifest = {"created_at": dt.datetime.now().isoformat(), "tables": {}}

    with _common.connect() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE' "
                "ORDER BY table_schema, table_name",
                (list(SCHEMAS),),
            )
            tables = cur.fetchall()

            for schema, table in tables:
                key = f"{schema}.{table}"
                try:
                    cur.execute(f'SELECT * FROM "{schema}"."{table}"')
                except psycopg.Error as e:
                    print(f"  [FAIL] {key}: {str(e).strip()}", file=sys.stderr)
                    return 1

                columns = [d.name for d in cur.description]
                path = os.path.join(out_dir, f"{schema}.{table}.jsonl")
                count = 0
                with open(path, "w", encoding="utf-8") as fh:
                    for row in cur:
                        fh.write(
                            json.dumps(
                                dict(zip(columns, row)),
                                ensure_ascii=False,
                                default=_default,
                            )
                            + "\n"
                        )
                        count += 1

                manifest["tables"][key] = {"rows": count, "columns": columns}
                print(f"  [OK]   {key}: {count} строк")

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    # Указатель на последнюю копию: тестам нужен предсказуемый путь, а не
    # угадывание самого свежего каталога по имени.
    latest = os.path.join(_common.REPO_ROOT, "backend", "backups", "latest.json")
    with open(latest, "w", encoding="utf-8") as fh:
        json.dump({"dir": out_dir, "manifest": manifest_path}, fh,
                  ensure_ascii=False, indent=2)

    print(f"\nМанифест: {manifest_path}")
    print(f"Всего таблиц: {len(manifest['tables'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
