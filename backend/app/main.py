from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import ai_agent

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

    sql_reply = ai_agent.call_gpt(sql_system_prompt, user_input)
    sql = ai_agent.extract_sql(sql_reply)

    if not sql:
        return ChatResponse(answer="Не удалось сгенерировать SQL-запрос. Попробуйте переформулировать вопрос.")

    db_result = ai_agent.run_sql_through_security(sql)

    interpret_input = (
        f"Исходный вопрос пользователя:\n{user_input}\n\n"
        f"Выполненный SQL-запрос:\n{sql}\n\n"
        f"Результат из базы данных:\n{db_result}"
    )
    human_answer = ai_agent.call_gpt(interpret_system_prompt, interpret_input)

    return ChatResponse(answer=human_answer, sql=sql)