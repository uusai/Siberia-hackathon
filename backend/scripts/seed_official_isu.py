"""Загружает выверенный датасет ИГУ в справочные таблицы. Идемпотентно.

    python backend/scripts/seed_official_isu.py            # показать план
    python backend/scripts/seed_official_isu.py --apply    # записать

Источник данных — backend/data/isu_official.json. Ни одной цифры этот скрипт
не придумывает: что есть в файле, то и грузит, а чего нет — того нет.

ИДЕМПОТЕНТНОСТЬ. Каждая вставка идёт через
INSERT ... ON CONFLICT (<естественный ключ>) DO UPDATE, ключи объявлены в
006_official_reference.sql. Повторный запуск обновляет значения на месте и
НЕ создаёт дублей. DELETE в этом файле отсутствует физически — это
проверяется тестом backend/tests/test_data_integrity.py.

СВЯЗЬ С ДЕМО-КОНТУРОМ. В конце скрипт проставляет faculties.unit_id и
programs.official_program_id — но только там, где сопоставление однозначно
(совпал ФГОС-код направления и ровно один кандидат с каждой стороны).
Неоднозначные случаи остаются NULL: выдуманная связь хуже отсутствующей.
"""

import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

DATA_PATH = os.path.join(_common.REPO_ROOT, "backend", "data", "isu_official.json")


def _upsert(cur, table: str, columns: list[str], conflict: str,
            rows: list[tuple], update_columns: list[str] | None = None,
            extra_set: str | None = None) -> int:
    """INSERT ... ON CONFLICT DO UPDATE по списку строк.

    extra_set — присваивания, которые не берутся из EXCLUDED (например
    updated_at = now()).
    """
    if not rows:
        return 0
    if update_columns is None:
        # Разбираем список ключевых колонок по-настоящему, а не подстрокой:
        # 'code' входит в 'unit_id, code, ...' и как имя колонки, и как кусок
        # другого слова, и второе привело бы к молча пропущенному обновлению.
        key_columns = {c.strip() for c in conflict.split(",")}
        update_columns = [c for c in columns if c not in key_columns]
    placeholders = ", ".join(["%s"] * len(columns))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
    if extra_set:
        assignments = f"{assignments}, {extra_set}"
    statement = (
        f"INSERT INTO assistant.{table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
    )
    cur.executemany(statement, rows)
    return len(rows)


def _source_ids(cur, sources: list[dict]) -> dict[str, int]:
    for source in sources:
        cur.execute(
            "INSERT INTO assistant.data_sources (url, title, publisher, checked_at, note) "
            "VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now()), %s) "
            "ON CONFLICT (url) DO UPDATE SET "
            "title = EXCLUDED.title, publisher = EXCLUDED.publisher, "
            "checked_at = EXCLUDED.checked_at, note = EXCLUDED.note",
            (source["url"], source["title"], source.get("publisher"),
             source.get("checked_at"), source.get("note")),
        )
    cur.execute("SELECT url, id FROM assistant.data_sources")
    return dict(cur.fetchall())


def _link_demo_to_official(cur) -> tuple[int, int]:
    """Мостик демо-контур -> официальный справочник по ФГОС-коду.

    Только однозначные совпадения: код направления встречается ровно один раз
    с каждой стороны. Иначе связь не ставится.
    """
    cur.execute(
        "UPDATE assistant.programs p "
        "SET official_program_id = ep.id "
        "FROM assistant.edu_programs ep "
        "WHERE p.official_program_id IS NULL "
        "  AND p.code = ep.code "
        "  AND (SELECT count(*) FROM assistant.edu_programs x WHERE x.code = p.code) = 1 "
        "  AND (SELECT count(*) FROM assistant.programs y WHERE y.code = p.code) = 1"
    )
    programs_linked = cur.rowcount

    # Факультет демо-контура привязываем к подразделению ИГУ только если ВСЕ
    # его сопоставленные направления указывают на одно и то же подразделение.
    cur.execute(
        "UPDATE assistant.faculties f "
        "SET unit_id = sub.unit_id "
        "FROM ( "
        "  SELECT p.faculty_id, min(ep.unit_id) AS unit_id "
        "  FROM assistant.programs p "
        "  JOIN assistant.edu_programs ep ON ep.id = p.official_program_id "
        "  GROUP BY p.faculty_id "
        "  HAVING count(DISTINCT ep.unit_id) = 1 "
        ") sub "
        "WHERE f.id = sub.faculty_id AND f.unit_id IS NULL"
    )
    return programs_linked, cur.rowcount


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    _common.banner("Загрузка официального справочника ИГУ", apply)

    if not os.path.isfile(DATA_PATH):
        print(f"Нет файла с данными: {DATA_PATH}", file=sys.stderr)
        return 2

    with open(DATA_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    sections = [
        ("sources", "источники"),
        ("units", "подразделения"),
        ("programs", "направления"),
        ("exams", "вступительные испытания"),
        ("program_exams", "испытания по направлениям"),
        ("minimum_scores", "минимальные баллы"),
        ("passing_scores", "проходные баллы"),
        ("enrollment_places", "места приёма"),
        ("tuition_fees", "стоимость обучения"),
        ("admission_deadlines", "сроки приёма"),
        ("admission_documents", "документы"),
        ("benefits_quotas", "льготы и квоты"),
        ("dormitories", "общежития"),
        ("campus_buildings", "корпуса"),
        ("contacts", "контакты"),
        ("faq", "вопросы и ответы"),
    ]
    print("\nВ файле:")
    for key, label in sections:
        print(f"  {label:<30} {len(data.get(key, []))}")

    if not apply:
        print("\nЗапись выключена. Добавьте --apply.")
        return 0

    written: dict[str, int] = {}

    with _common.connect() as conn:
        with conn.cursor() as cur:
            src = _source_ids(cur, data.get("sources", []))
            written["data_sources"] = len(src)

            def sid(record):
                url = record.get("source")
                return src.get(url) if url else None

            # -- подразделения -------------------------------------------
            written["university_units"] = _upsert(
                cur, "university_units",
                ["official_name", "short_name", "kind", "description", "address",
                 "contact_phone", "contact_email", "site_url",
                 "source_id", "source_url", "checked_at", "data_status"],
                "official_name",
                [(u["official_name"], u.get("short_name"), u["kind"],
                  u.get("description"), u.get("address"), u.get("contact_phone"),
                  u.get("contact_email"), u.get("site_url"), sid(u),
                  u.get("source"), u.get("checked_at"), u.get("data_status", "official"))
                 for u in data.get("units", [])],
            )

            cur.execute("SELECT official_name, id FROM assistant.university_units")
            unit_ids = dict(cur.fetchall())

            # -- направления ---------------------------------------------
            written["edu_programs"] = _upsert(
                cur, "edu_programs",
                ["unit_id", "code", "name", "level", "study_form", "duration_years",
                 "profile", "description", "page_url",
                 "source_id", "source_url", "checked_at", "data_status"],
                "unit_id, code, level, study_form, profile",
                [(unit_ids[p["unit"]], p["code"], p["name"], p["level"],
                  p["study_form"], p.get("duration_years"), p.get("profile"),
                  p.get("description"), p.get("page_url"), sid(p),
                  p.get("source"), p.get("checked_at"), p.get("data_status", "official"))
                 for p in data.get("programs", [])],
            )

            cur.execute(
                "SELECT unit_id, code, level, study_form, coalesce(profile, ''), id "
                "FROM assistant.edu_programs"
            )
            program_ids = {
                (u, c, lv, sf, pr): pid for u, c, lv, sf, pr, pid in cur.fetchall()
            }

            def program_id(ref: dict) -> int:
                return program_ids[(
                    unit_ids[ref["unit"]], ref["code"], ref["level"],
                    ref["study_form"], ref.get("profile") or "",
                )]

            # -- испытания -----------------------------------------------
            written["entrance_exams"] = _upsert(
                cur, "entrance_exams", ["name", "kind", "description"], "name",
                [(e["name"], e["kind"], e.get("description"))
                 for e in data.get("exams", [])],
            )
            cur.execute("SELECT name, id FROM assistant.entrance_exams")
            exam_ids = dict(cur.fetchall())

            written["program_exams"] = _upsert(
                cur, "program_exams",
                ["program_id", "admission_year", "exam_id", "requirement", "slot",
                 "priority", "source_id", "source_url", "checked_at", "data_status"],
                "program_id, admission_year, exam_id, slot",
                [(program_id(r["program"]), r["admission_year"], exam_ids[r["exam"]],
                  r["requirement"], r.get("slot", 1), r.get("priority"), sid(r),
                  r.get("source"), r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("program_exams", [])],
            )

            # -- баллы ---------------------------------------------------
            written["minimum_scores"] = _upsert(
                cur, "minimum_scores",
                ["admission_year", "exam_id", "program_id", "level", "min_score",
                 "source_id", "source_url", "checked_at", "data_status"],
                "admission_year, exam_id, program_id, level",
                [(r["admission_year"], exam_ids[r["exam"]],
                  program_id(r["program"]) if r.get("program") else None,
                  r["level"], r.get("min_score"), sid(r), r.get("source"),
                  r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("minimum_scores", [])],
            )

            written["passing_scores"] = _upsert(
                cur, "passing_scores",
                ["program_id", "admission_year", "study_form", "funding_basis",
                 "competition_group", "score", "source_id", "source_url",
                 "checked_at", "data_status"],
                "program_id, admission_year, study_form, funding_basis, competition_group",
                [(program_id(r["program"]), r["admission_year"], r["study_form"],
                  r["funding_basis"], r["competition_group"], r.get("score"),
                  sid(r), r.get("source"), r.get("checked_at"),
                  r.get("data_status", "historical"))
                 for r in data.get("passing_scores", [])],
            )

            # -- места, стоимость, сроки ---------------------------------
            written["enrollment_places"] = _upsert(
                cur, "enrollment_places",
                ["program_id", "admission_year", "study_form", "funding_basis",
                 "quota_kind", "seats", "source_id", "source_url", "checked_at",
                 "data_status"],
                "program_id, admission_year, study_form, funding_basis, quota_kind",
                [(program_id(r["program"]), r["admission_year"], r["study_form"],
                  r["funding_basis"], r["quota_kind"], r.get("seats"), sid(r),
                  r.get("source"), r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("enrollment_places", [])],
            )

            written["tuition_fees"] = _upsert(
                cur, "tuition_fees",
                ["program_id", "academic_year", "study_form", "price_rub",
                 "source_id", "source_url", "checked_at", "data_status"],
                "program_id, academic_year, study_form",
                [(program_id(r["program"]), r["academic_year"], r["study_form"],
                  r.get("price_rub"), sid(r), r.get("source"), r.get("checked_at"),
                  r.get("data_status", "official"))
                 for r in data.get("tuition_fees", [])],
            )

            written["admission_deadlines"] = _upsert(
                cur, "admission_deadlines",
                ["admission_year", "level", "study_form", "funding_basis", "stage",
                 "date_from", "date_to", "description", "source_id", "source_url",
                 "checked_at", "data_status"],
                "admission_year, level, study_form, funding_basis, stage",
                [(r["admission_year"], r["level"], r.get("study_form"),
                  r.get("funding_basis"), r["stage"], r.get("date_from"),
                  r.get("date_to"), r.get("description"), sid(r), r.get("source"),
                  r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("admission_deadlines", [])],
            )

            # -- документы, льготы ---------------------------------------
            written["admission_documents"] = _upsert(
                cur, "admission_documents",
                ["admission_year", "level", "applicant_category", "doc_name",
                 "is_required", "note", "source_id", "source_url", "checked_at",
                 "data_status"],
                "admission_year, level, applicant_category, doc_name",
                [(r["admission_year"], r["level"], r.get("applicant_category", "все"),
                  r["doc_name"], r.get("is_required", True), r.get("note"), sid(r),
                  r.get("source"), r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("admission_documents", [])],
            )

            written["benefits_quotas"] = _upsert(
                cur, "benefits_quotas",
                ["admission_year", "kind", "title", "description", "source_id",
                 "source_url", "checked_at", "data_status"],
                "admission_year, kind, title",
                [(r["admission_year"], r["kind"], r["title"], r.get("description"),
                  sid(r), r.get("source"), r.get("checked_at"),
                  r.get("data_status", "official"))
                 for r in data.get("benefits_quotas", [])],
            )

            # -- общежития, корпуса, контакты ----------------------------
            written["dormitories"] = _upsert(
                cur, "dormitories",
                ["name", "address", "description", "provided_to", "contact_phone",
                 "source_id", "source_url", "checked_at", "data_status"],
                "name",
                [(r["name"], r.get("address"), r.get("description"),
                  r.get("provided_to"), r.get("contact_phone"), sid(r),
                  r.get("source"), r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("dormitories", [])],
            )

            written["campus_buildings"] = _upsert(
                cur, "campus_buildings",
                ["name", "address", "unit_id", "description", "source_id",
                 "source_url", "checked_at", "data_status"],
                "name, address",
                [(r["name"], r.get("address"),
                  unit_ids.get(r["unit"]) if r.get("unit") else None,
                  r.get("description"), sid(r), r.get("source"), r.get("checked_at"),
                  r.get("data_status", "official"))
                 for r in data.get("campus_buildings", [])],
            )

            written["contacts"] = _upsert(
                cur, "contacts",
                ["scope", "unit_id", "title", "contact_phone", "contact_email",
                 "address", "work_hours", "site_url", "source_id", "source_url",
                 "checked_at", "data_status"],
                "scope, title",
                [(r["scope"], unit_ids.get(r["unit"]) if r.get("unit") else None,
                  r["title"], r.get("contact_phone"), r.get("contact_email"),
                  r.get("address"), r.get("work_hours"), r.get("site_url"), sid(r),
                  r.get("source"), r.get("checked_at"), r.get("data_status", "official"))
                 for r in data.get("contacts", [])],
            )

            # -- FAQ -----------------------------------------------------
            written["faq_entries"] = _upsert(
                cur, "faq_entries",
                ["category", "question", "answer", "keywords", "source_id",
                 "source_url", "checked_at", "data_status"],
                "category, question",
                [(r["category"], r["question"], r["answer"], r.get("keywords", []),
                  sid(r), r.get("source"), r.get("checked_at"),
                  r.get("data_status", "official"))
                 for r in data.get("faq", [])],
                update_columns=["answer", "keywords", "source_id", "source_url",
                                "checked_at", "data_status"],
                # updated_at из EXCLUDED не берём: в INSERT его нет, срабатывает
                # DEFAULT now(). При обновлении отметку надо освежить явно.
                extra_set="updated_at = now()",
            )

            programs_linked, faculties_linked = _link_demo_to_official(cur)

    print("\nЗаписано (INSERT или UPDATE, дублей не создаётся):")
    for table, count in written.items():
        print(f"  {table:<24} {count}")
    print(f"\nМостик демо -> официальный справочник:")
    print(f"  programs.official_program_id проставлен: {programs_linked}")
    print(f"  faculties.unit_id проставлен:            {faculties_linked}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"[FAIL] в датасете нет ссылки на {e}", file=sys.stderr)
        sys.exit(1)
