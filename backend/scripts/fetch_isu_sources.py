"""Скачивает официальные страницы ИГУ и складывает снимки на диск.

    python backend/scripts/fetch_isu_sources.py
    python backend/scripts/fetch_isu_sources.py --only structure

Зачем отдельный шаг, а не «прочитал и вписал в JSON»: цифры приёмной кампании
меняются, и через месяц никто не вспомнит, откуда взялся минимальный балл 40.
Снимок + дата загрузки делают датасет проверяемым — можно открыть
backend/data/raw/<slug>.html и увидеть, что там было написано.

Часть страниц приёмной комиссии закрыта для автоматических запросов
(priem.isu.ru отдаёт 401). Это не ошибка скрипта: он честно записывает в
sources.json статус ответа, а сидер по такому источнику ставит
data_status='unverified' и NULL вместо значения. Выдумывать цифру нельзя.

Скрипт ничего не пишет в БД. В сеть ходит только на isu.ru.
"""

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

RAW_DIR = os.path.join(_common.REPO_ROOT, "backend", "data", "raw")

TIMEOUT_S = 30
USER_AGENT = "Mozilla/5.0 (compatible; ISU-assistant-hackathon/1.0)"

# Страницы, из которых собирается датасет. slug — имя файла снимка.
SOURCES = [
    {
        "slug": "structure",
        "url": "https://isu.ru/ru/university/structure/faculties/main/",
        "title": "ИГУ — Институты и факультеты",
    },
    {
        "slug": "documents",
        "url": "https://isu.ru/Abitur/perechen-dokumentov/",
        "title": "ИГУ — Перечень документов для поступления",
    },
    {
        "slug": "rules-bachelor-2026",
        "url": "https://isu.ru/Abitur/pk2026/bachelor/pravila_priema_bak/",
        "title": "ИГУ — Правила приёма на бакалавриат и специалитет, 2026",
    },
    {
        "slug": "rules-bachelor-2025",
        "url": "https://isu.ru/Abitur/pk2025/bachelor/pravila_priema_bak/",
        "title": "ИГУ — Правила приёма на бакалавриат и специалитет, 2025",
    },
    {
        "slug": "rules-master-2026",
        "url": "https://isu.ru/Abitur/pk2026/master/pravila_priema_mag/",
        "title": "ИГУ — Правила приёма в магистратуру, 2026",
    },
    {
        "slug": "priem-main",
        "url": "https://priem.isu.ru/",
        "title": "ИГУ — Приёмная комиссия",
    },
    {
        "slug": "unit-imei",
        "url": "https://isu.ru/ru/university/structure/faculties/imei/",
        "title": "Институт математики и информационных технологий ИГУ",
    },
    {
        "slug": "unit-law",
        "url": "https://isu.ru/ru/university/structure/faculties/law/",
        "title": "Юридический институт ИГУ",
    },
    {
        "slug": "unit-bio",
        "url": "https://isu.ru/ru/university/structure/faculties/bio/",
        "title": "Институт биологических наук ИГУ",
    },
    {
        "slug": "unit-philo",
        "url": "https://isu.ru/ru/university/structure/faculties/philo/",
        "title": "Институт филологии, иностранных языков и медиакоммуникации ИГУ",
    },
    {
        "slug": "unit-iss",
        "url": "https://isu.ru/ru/university/structure/faculties/iss/",
        "title": "Институт социальных наук ИГУ",
    },
    {
        "slug": "unit-miel",
        "url": "https://isu.ru/ru/university/structure/faculties/miel/",
        "title": "Международный институт экономики и лингвистики ИГУ",
    },
    {
        "slug": "unit-pi",
        "url": "https://isu.ru/ru/university/structure/faculties/pi/",
        "title": "Педагогический институт ИГУ",
    },
    {
        "slug": "unit-hist",
        "url": "https://isu.ru/ru/university/structure/faculties/hist/",
        "title": "Исторический факультет ИГУ",
    },
    {
        "slug": "unit-phy",
        "url": "https://isu.ru/ru/university/structure/faculties/phy/",
        "title": "Физический факультет ИГУ",
    },
    {
        "slug": "unit-chem",
        "url": "https://isu.ru/ru/university/structure/faculties/chem/",
        "title": "Химический факультет ИГУ",
    },
    {
        "slug": "unit-geog",
        "url": "https://isu.ru/ru/university/structure/faculties/geog/",
        "title": "Географический факультет ИГУ",
    },
    {
        "slug": "unit-psi",
        "url": "https://isu.ru/ru/university/structure/faculties/psi/",
        "title": "Факультет психологии ИГУ",
    },
    {
        "slug": "unit-buk",
        "url": "https://isu.ru/ru/university/structure/faculties/buk/",
        "title": "Байкальская международная бизнес-школа ИГУ",
    },
    {
        "slug": "unit-geol",
        "url": "https://isu.ru/ru/university/structure/faculties/geol/",
        "title": "Геологический факультет ИГУ",
    },
    {
        "slug": "unit-fsr",
        "url": "https://isu.ru/ru/university/structure/faculties/fsr/",
        "title": "Факультет бизнес-коммуникаций и информатики ИГУ",
    },
    {
        "slug": "dormitories",
        "url": "https://isu.ru/ru/student/dormitory/",
        "title": "ИГУ — Общежития",
    },
]


def fetch(source: dict) -> dict:
    request = urllib.request.Request(
        source["url"], headers={"User-Agent": USER_AGENT}, method="GET"
    )
    record = {
        "slug": source["slug"],
        "url": source["url"],
        "title": source["title"],
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            body = resp.read()
            record["status"] = resp.status
    except urllib.error.HTTPError as e:
        record["status"] = e.code
        record["error"] = f"HTTP {e.code}"
        return record
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        record["status"] = None
        record["error"] = str(e)
        return record

    path = os.path.join(RAW_DIR, f"{source['slug']}.html")
    with open(path, "wb") as fh:
        fh.write(body)
    record["bytes"] = len(body)
    record["path"] = path
    return record


def main(argv: list[str]) -> int:
    only = None
    if "--only" in argv:
        index = argv.index("--only")
        if index + 1 < len(argv):
            only = argv[index + 1]

    os.makedirs(RAW_DIR, exist_ok=True)
    print("=" * 72)
    print("  Загрузка официальных источников ИГУ (только чтение из сети)")
    print(f"  Каталог снимков: {RAW_DIR}")
    print("=" * 72)

    records = []
    ok = 0
    for source in SOURCES:
        if only and source["slug"] != only:
            continue
        record = fetch(source)
        records.append(record)
        if "error" in record:
            print(f"  [--]   {record['slug']:<22} {record['error']}")
        else:
            ok += 1
            print(f"  [OK]   {record['slug']:<22} {record['bytes']} байт")

    index_path = os.path.join(RAW_DIR, "sources.json")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)

    print(f"\nУспешно: {ok} из {len(records)}. Индекс: {index_path}")
    print("Недоступные страницы — не повод выдумывать данные: соответствующие")
    print("записи должны получить data_status='unverified' и NULL вместо значений.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
