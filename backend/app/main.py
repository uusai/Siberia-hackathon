from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import ai_agent

app = FastAPI()

# Разрешаем запросы с фронтенда (открытого как файл или на другом порту)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # для хакатона ок; в проде — конкретные домены
    allow_methods=["*"],
    allow_headers=["*"],
)

# Системные промпты собираются один раз при старте сервера
# (внутри build_sql_system_prompt уже вызывается get_db_schema())
sql_system_prompt = ai_agent.build_sql_system_prompt()
interpret_system_prompt = ai_agent.build_interpret_system_prompt()


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    user_input = request.question.strip()
    if not user_input:
        return ChatResponse(answer="Пустой вопрос.")

    # Фаза 1: генерация SQL
    sql_reply = ai_agent.call_gpt(sql_system_prompt, user_input)
    sql = ai_agent.extract_sql(sql_reply)

    if not sql:
        return ChatResponse(answer="Не удалось сгенерировать SQL-запрос. Попробуйте переформулировать вопрос.")

    # Проверка безопасности + выполнение в БД
    db_result = ai_agent.run_sql_through_security(sql)

    # Фаза 2: расшифровка результата на человеческий язык
    interpret_input = (
        f"Исходный вопрос пользователя:\n{user_input}\n\n"
        f"Выполненный SQL-запрос:\n{sql}\n\n"
        f"Результат из базы данных:\n{db_result}"
    )
    human_answer = ai_agent.call_gpt(interpret_system_prompt, interpret_input)

    return ChatResponse(answer=human_answer, sql=sql)