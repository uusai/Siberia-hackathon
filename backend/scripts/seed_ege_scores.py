"""Раскладывает суммарный балл ЕГЭ заявления на предметные баллы.

    python backend/scripts/seed_ege_scores.py            # показать план
    python backend/scripts/seed_ege_scores.py --apply    # записать

ЗАЧЕМ. В assistant.applications есть только ege_total — сумма. Вопросы вида
«какой средний балл по информатике у поступавших» на неё не отвечаются:
предметных баллов в базе не было вовсе, а таблица assistant.ege_scores из
миграции 002 стояла пустой — сама миграция до этой доработки не применялась,
хотя роль administration уже ссылалась на ege_scores_summary в whitelist.

ОТКУДА БЕРУТСЯ ПРЕДМЕТЫ. Набор предметов заявления определяется официальным
перечнем вступительных испытаний ИГУ на это направление, если он загружен в
assistant.program_exams. Если официального перечня для направления нет,
берётся набор по умолчанию для профиля направления (по первым двум цифрам
ФГОС-кода). Так предметные баллы не противоречат тому, что реально сдают.

ЧТО ЭТО ЗА ДАННЫЕ. Демонстрационные: сумма настоящая (она уже лежала в
applications), разложение по предметам — синтетическое. Строки помечаются
через саму таблицу applications, у которой data_status='demo', и агрегат
ege_scores_summary виден только роли administration. Никакой строки с
пометкой 'official' этот скрипт не создаёт.

Идемпотентно: UNIQUE (application_id, subject) + ON CONFLICT DO UPDATE.
Пересчёт на месте нужен, потому что набор предметов зависит от справочника
испытаний: он уточняется, и баллы должны уточняться вместе с ним.
Разрушающих операций нет — строки только добавляются и обновляются.
"""

import os
import random
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

# Набор предметов по укрупнённой группе ФГОС, когда официального перечня
# для направления в базе нет. Русский язык обязателен везде.
DEFAULT_SUBJECTS = {
    "01": ["Русский язык", "Математика (профильный уровень)", "Информатика"],
    "02": ["Русский язык", "Математика (профильный уровень)", "Информатика"],
    "03": ["Русский язык", "Математика (профильный уровень)", "Физика"],
    "05": ["Русский язык", "Математика (профильный уровень)", "География"],
    "06": ["Русский язык", "Биология", "Химия"],
    "09": ["Русский язык", "Математика (профильный уровень)", "Информатика"],
    "10": ["Русский язык", "Математика (профильный уровень)", "Информатика"],
    "38": ["Русский язык", "Математика (профильный уровень)", "Обществознание"],
    "40": ["Русский язык", "Обществознание", "История"],
    "44": ["Русский язык", "Обществознание", "Математика (профильный уровень)"],
}
FALLBACK_SUBJECTS = ["Русский язык", "Математика (профильный уровень)", "Обществознание"]

# Детерминированный генератор: повторный запуск на тех же данных даёт те же
# баллы. Случайность здесь — способ разложить сумму, а не источник правды.
SEED = 20260826


def _split_total(total: int, count: int, rng: random.Random) -> list[int]:
    """Разбивает сумму на count баллов в диапазоне 0..100.

    Простое деление поровну дало бы всем одинаковые баллы, и вопрос «по
    какому предмету баллы выше» потерял бы смысл. Поэтому раскидываем с
    разбросом, но так, чтобы сумма сошлась точно.
    """
    base = total // count
    scores = [base] * count
    scores[0] += total - base * count

    for _ in range(count * 3):
        i, j = rng.randrange(count), rng.randrange(count)
        if i == j:
            continue
        shift = rng.randint(1, 8)
        if scores[i] - shift >= 20 and scores[j] + shift <= 100:
            scores[i] -= shift
            scores[j] += shift
    return scores


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    _common.banner("Разложение баллов ЕГЭ по предметам", apply)

    with _common.connect() as conn:
        with conn.cursor() as cur:
            # Официальные наборы испытаний, если справочник уже загружен.
            #
            # Берём ПО ОДНОМУ предмету на слот, а не только обязательные.
            # Обязательных часто два («математика» и «русский»), третий слот —
            # выбор из нескольких предметов. Если считать только обязательные,
            # сумма за три экзамена делится на два, и средний балл по предмету
            # улетает к сотне — ровно это и вылезло на первом прогоне.
            cur.execute(
                "SELECT code, array_agg(exam_name ORDER BY slot) FROM ("
                "  SELECT DISTINCT ON (ep.code, pe.slot) "
                "         ep.code, pe.slot, ee.name AS exam_name "
                "  FROM assistant.program_exams pe "
                "  JOIN assistant.edu_programs ep ON ep.id = pe.program_id "
                "  JOIN assistant.entrance_exams ee ON ee.id = pe.exam_id "
                "  ORDER BY ep.code, pe.slot, pe.priority NULLS LAST, ee.name"
                ") slots GROUP BY code"
            )
            official = dict(cur.fetchall())
            print(f"\nОфициальных наборов испытаний в справочнике: {len(official)}")

            cur.execute(
                "SELECT a.id, a.ege_total, p.code "
                "FROM assistant.applications a "
                "JOIN assistant.admission_campaigns ac ON ac.id = a.campaign_id "
                "JOIN assistant.programs p ON p.id = ac.program_id "
                "ORDER BY a.id"
            )
            rows = cur.fetchall()
            print(f"Заявлений к разложению: {len(rows)}")

            rng = random.Random(SEED)
            payload = []
            for app_id, total, code in rows:
                group = (code or "")[:2]
                subjects = official.get(code) or DEFAULT_SUBJECTS.get(
                    group, FALLBACK_SUBJECTS
                )
                subjects = list(dict.fromkeys(subjects))[:4]
                if not subjects:
                    continue
                # Сумма в applications рассчитана на три предмета; если набор
                # шире или уже, масштабируем, иначе получим баллы выше 100.
                scaled = min(100 * len(subjects), max(total, 20 * len(subjects)))
                for subject, score in zip(subjects, _split_total(scaled, len(subjects), rng)):
                    payload.append((app_id, subject, max(0, min(100, score))))

            print(f"Строк к вставке: {len(payload)}")
            if not apply:
                print("\nЗапись выключена. Добавьте --apply.")
                return 0

            cur.executemany(
                "INSERT INTO assistant.ege_scores (application_id, subject, score) "
                "VALUES (%s, %s, %s) ON CONFLICT (application_id, subject) "
                "DO UPDATE SET score = EXCLUDED.score",
                payload,
            )
            cur.execute("SELECT count(*) FROM assistant.ege_scores")
            print(f"\n[OK] ege_scores: {cur.fetchone()[0]} строк")
            cur.execute(
                "SELECT subject, count(*), round(avg(score), 1) "
                "FROM assistant.ege_scores GROUP BY subject ORDER BY 2 DESC"
            )
            for subject, count, avg in cur.fetchall():
                print(f"  {subject:<38} {count:>6}  средний {avg}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
