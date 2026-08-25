# Siberia-hackathon

# AI-ассистент университета

Текстовый ассистент, который отвечает на вопросы о данных университета
на естественном языке: генерирует SQL-запрос, проверяет его на
безопасность и возвращает ответ вместе с таблицей.

## Требования

- Docker + Docker Compose (`docker compose version`)
- Доступ к PostgreSQL с данными университета
- API-ключ Yandex GPT (Foundation Models)

## Быстрый старт

1. Склонировать репозиторий и перейти в папку проекта:

   ```bash
   git clone <ссылка-на-репозиторий>
   cd Hackathon
   ```

2. Создать файл `.env` в корне проекта на основе `.env.example`:

   ```bash
   cp .env.example .env
   ```

   Заполнить переменные:

   ```env
   DATABASE_URL=
   DB_HOST=
   DB_PORT=
   DB_NAME=
   DB_USER=
   DB_PASSWORD=

   API_KEY=
   FOLDER_ID=
   MODEL_NAME=
   API_URL=
   ```

   - `DB_*` — параметры подключения к PostgreSQL
   - `API_KEY`, `FOLDER_ID`, `MODEL_NAME`, `API_URL` — доступ к Yandex GPT

3. Собрать и запустить контейнеры:

   ```bash
   docker compose up --build
   ```

   Или в фоновом режиме:

   ```bash
   docker compose up --build -d
   ```

4. Открыть в браузере:

   ```
   http://localhost
   ```

## Проверка, что всё поднялось

```bash
curl http://localhost:8000/health
```

Должно вернуть:

```json
{"status": "ok"}
```

## Порты

| Сервис    | Порт |
|-----------|------|
| Frontend  | 80   |
| Backend   | 8000 |

## Остановка

```bash
docker compose down
```

## Пересборка после изменений

Изменения в `backend/` подхватываются автоматически (uvicorn `--reload`
+ смонтированный volume). Изменения в `frontend/` требуют пересборки
образа:

```bash
docker compose build --no-cache frontend
docker compose up -d frontend
```