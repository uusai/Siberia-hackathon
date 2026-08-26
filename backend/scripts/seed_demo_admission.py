"""Демонстрационные места, стоимость обучения и проходные баллы.

    python backend/scripts/seed_demo_admission.py            # показать план
    python backend/scripts/seed_demo_admission.py --apply    # записать

ЧЕСТНО О ПРИРОДЕ ЭТИХ ДАННЫХ. Количество мест, стоимость обучения и
проходные баллы по направлениям в открытых документах приёмной кампании ИГУ
не опубликованы: Правила приёма и приложения к ним их не содержат, а
priem.isu.ru закрыт для автоматических запросов. Числа ниже СГЕНЕРИРОВАНЫ,
чтобы представления programs_admission и passing_scores_view было на чём
показать, и каждая такая строка получает data_status='demo'.

Из-за этого ассистент, отвечая на «сколько бюджетных мест» или «какой был
проходной балл», обязан предупредить, что это демонстрационные данные, и
дать ссылку на официальный источник — правило вшито в промпт интерпретации
(backend/app/ai_agent.py). Ни одна строка отсюда не помечается 'official'.

Когда появятся официальные цифры, тот же seed_official_isu.py перезапишет
их по естественному ключу — удалять ничего не нужно.

Генерация детерминированная: повторный запуск даёт те же числа, поэтому
скрипт идемпотентен и в части значений, а не только в части ключей.
"""

import os
import random
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

YEAR = 2026
ACADEMIC_YEAR = "2026/2027"
HISTORY_YEARS = (2024, 2025)
SEED = 20260826

NOTICE = ("Демонстрационные данные хакатонского стенда. Официальные сведения — "
          "на сайте приёмной комиссии ИГУ.")
DEMO_SOURCE = "https://isu.ru/Abitur/"

# Базовая стоимость года обучения по форме — от неё считается цена
# направления. Порядок величин взят правдоподобным, но это НЕ прайс ИГУ.
BASE_PRICE = {"очная": 168000, "очно-заочная": 118000, "заочная": 92000}

# Популярные направления оттягивают на себя конкурс: у них выше проходной
# балл и меньше бюджетных мест. Ключ — первые две цифры ФГОС-кода.
POPULAR_GROUPS = {"09", "01", "02", "10", "40", "38", "45"}


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    _common.banner("Демонстрационные места, стоимость и проходные баллы", apply)

    with _common.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, code, level, study_form FROM assistant.edu_programs "
                "ORDER BY id"
            )
            programs = cur.fetchall()
            print(f"\nНаправлений в справочнике: {len(programs)}")
            if not programs:
                print("Справочник пуст — сначала seed_official_isu.py --apply")
                return 1

            rng = random.Random(SEED)
            places, fees, passing = [], [], []

            for program_id, code, level, study_form in programs:
                group = code[:2]
                popular = group in POPULAR_GROUPS

                budget_main = rng.choice([10, 12, 15, 18, 20, 25]) if popular \
                    else rng.choice([15, 20, 25, 30, 35])
                paid_main = rng.choice([10, 15, 20, 25, 30])
                # Квоты по закону — не менее 10% бюджетных мест каждая.
                special = max(1, round(budget_main * 0.1))
                separate = max(1, round(budget_main * 0.1))
                target = rng.choice([0, 1, 2, 3, 5])

                for basis, quota, seats in (
                    ("бюджет", "основные места", budget_main),
                    ("бюджет", "особая квота", special),
                    ("бюджет", "отдельная квота", separate),
                    ("бюджет", "целевая квота", target),
                    ("контракт", "основные места", paid_main),
                ):
                    places.append((program_id, YEAR, study_form, basis, quota, seats))

                price = BASE_PRICE[study_form] + rng.randrange(0, 9) * 4000
                if popular:
                    price += 16000
                fees.append((program_id, ACADEMIC_YEAR, study_form, price))

                # Проходной балл прошлых лет: на платных местах ниже, чем на
                # бюджете, в особой квоте ниже, чем в общем конкурсе.
                for year in HISTORY_YEARS:
                    base = rng.randint(205, 250) if popular else rng.randint(160, 215)
                    base += 3 if year == 2025 else 0
                    for basis, quota, delta in (
                        ("бюджет", "основные места", 0),
                        ("бюджет", "особая квота", -28),
                        ("контракт", "основные места", -22),
                    ):
                        passing.append((program_id, year, study_form, basis, quota,
                                        max(120, base + delta)))

            print(f"  мест приёма:      {len(places)}")
            print(f"  строк стоимости:  {len(fees)}")
            print(f"  проходных баллов: {len(passing)}")

            if not apply:
                print("\nЗапись выключена. Добавьте --apply.")
                return 0

            cur.executemany(
                "INSERT INTO assistant.enrollment_places "
                "(program_id, admission_year, study_form, funding_basis, "
                " quota_kind, seats, source_url, checked_at, data_status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), 'demo') "
                "ON CONFLICT (program_id, admission_year, study_form, "
                "             funding_basis, quota_kind) "
                "DO UPDATE SET seats = EXCLUDED.seats, data_status = 'demo'",
                [row + (DEMO_SOURCE,) for row in places],
            )
            cur.executemany(
                "INSERT INTO assistant.tuition_fees "
                "(program_id, academic_year, study_form, price_rub, "
                " source_url, checked_at, data_status) "
                "VALUES (%s, %s, %s, %s, %s, now(), 'demo') "
                "ON CONFLICT (program_id, academic_year, study_form) "
                "DO UPDATE SET price_rub = EXCLUDED.price_rub, data_status = 'demo'",
                [row + (DEMO_SOURCE,) for row in fees],
            )
            cur.executemany(
                "INSERT INTO assistant.passing_scores "
                "(program_id, admission_year, study_form, funding_basis, "
                " competition_group, score, source_url, checked_at, data_status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), 'demo') "
                "ON CONFLICT (program_id, admission_year, study_form, "
                "             funding_basis, competition_group) "
                "DO UPDATE SET score = EXCLUDED.score, data_status = 'demo'",
                [row + (DEMO_SOURCE,) for row in passing],
            )

            for table in ("enrollment_places", "tuition_fees", "passing_scores"):
                cur.execute(f"SELECT count(*) FROM assistant.{table}")
                print(f"  {table}: {cur.fetchone()[0]} строк")

    print(f"\n{NOTICE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
