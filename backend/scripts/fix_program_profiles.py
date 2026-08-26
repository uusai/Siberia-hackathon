"""Чинит названия и профили направлений в assistant.edu_programs.

    python backend/scripts/fix_program_profiles.py            # только показать
    python backend/scripts/fix_program_profiles.py --apply    # применить

ЧТО БЫЛО СЛОМАНО. Разбор приложения к Правилам приёма (build_dataset.py)
доставал профиль жадным `re.search(r"\\((.*)\\)(.*)$")` — от ПЕРВОЙ открывающей
скобки до ПОСЛЕДНЕЙ закрывающей. На заголовке

    Педагогическое образование (с двумя профилями подготовки) (Математика -Информатика)

он выдавал название «Педагогическое образование» и профиль
«с двумя профилями подготовки) (Математика -Информатика» — с посторонней
скобкой посреди строки и потерянным уточнением из официального названия.

Поражено 26 записей каталога из 113, но проверка баланса скобок ловила лишь
пять: у остальных скобки случайно сходились по количеству.

Разбор исправлен в build_dataset.py (split_title_and_profile), и эта функция
здесь переиспользуется — двух реализаций одного правила быть не должно.
Скрипт восстанавливает исходный заголовок из уже сохранённых значений
(«название (профиль)») и раскладывает его заново.

Данные не пересеиваются: у edu_programs естественный ключ включает profile, и
повторный посев с исправленными значениями завёл бы вторые копии строк вместо
правки существующих. Поэтому UPDATE по id.

search_vector трогать не нужно — это генерируемая колонка, она пересчитается
сама.
"""

import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402
from build_dataset import split_title_and_profile  # noqa: E402


def _restore_title(name: str, profile: str | None) -> str:
    """Собирает заголовок обратно так, как его видел прежний разбор."""
    return f"{name} ({profile})" if profile else name


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    _common.banner("Названия и профили направлений", apply)

    with _common.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, unit_id, code, level, study_form, name, profile "
                "FROM assistant.edu_programs ORDER BY id"
            )
            rows = cur.fetchall()

    changes = []
    keys: dict[tuple, int] = {}
    for pid, unit_id, code, level, study_form, name, profile in rows:
        new_name, new_profile = split_title_and_profile(
            _restore_title(name, profile)
        )
        key = (unit_id, code, level, study_form, new_profile)
        if key in keys:
            print(
                f"  [СТОП] строки {keys[key]} и {pid} после правки совпадут по "
                f"естественному ключу ({code}, профиль {new_profile!r}). "
                f"Правка отменена — разбирайтесь руками.",
                file=sys.stderr,
            )
            return 1
        keys[key] = pid
        if (new_name, new_profile) != (name, profile):
            changes.append((pid, name, profile, new_name, new_profile))

    print(f"\nЗаписей в каталоге: {len(rows)}, к правке: {len(changes)}\n")
    for pid, name, profile, new_name, new_profile in changes:
        print(f"  #{pid}")
        print(f"    было : {name!r} | {profile!r}")
        print(f"    стало: {new_name!r} | {new_profile!r}")

    if not changes:
        print("Каталог уже в порядке.")
        return 0

    if not apply:
        print("\nЗапись выключена. Добавьте --apply, чтобы применить.")
        return 0

    try:
        with _common.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE assistant.edu_programs "
                    "SET name = %s, profile = %s WHERE id = %s",
                    [(n, p, pid) for pid, _, _, n, p in changes],
                )
                written = cur.rowcount
    except psycopg.Error as e:
        print(f"\n[ОШИБКА] {str(e).strip()}", file=sys.stderr)
        return 1

    print(f"\nОбновлено записей: {len(changes)} (rowcount последнего: {written})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
