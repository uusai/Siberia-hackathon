import sys
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import ai_agent
from . import security

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
    role: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None


def _safe_log_audit(**kwargs) -> None:
    try:
        security.log_audit_entry(**kwargs)
    except Exception as e:
        print(f"[audit_log] Не удалось записать запись аудита: {e}", file=sys.stderr)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    start_time = time.monotonic()
    llm_ms = 0
    username = "anonymous"
    role = request.role
    user_input = request.question.strip()

    if not user_input:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        _safe_log_audit(
            username=username,
            role=role,
            question=user_input,
            generated_sql=None,
            executed_sql=None,
            verdict="no_sql",
            reject_reason=None,
            row_count=None,
            duration_ms=duration_ms,
            llm_ms=llm_ms,
            model=ai_agent.MODEL_NAME,
        )
        return ChatResponse(answer="Пустой вопрос.")

    llm_start = time.monotonic()
    sql_reply = ai_agent.call_gpt(sql_system_prompt, user_input)
    llm_ms += int((time.monotonic() - llm_start) * 1000)
    sql = ai_agent.extract_sql(sql_reply)

    if not sql:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        _safe_log_audit(
            username=username,
            role=role,
            question=user_input,
            generated_sql=None,
            executed_sql=None,
            verdict="no_sql",
            reject_reason=None,
            row_count=None,
            duration_ms=duration_ms,
            llm_ms=llm_ms,
            model=ai_agent.MODEL_NAME,
        )
        return ChatResponse(answer="Не удалось сгенерировать SQL-запрос. Попробуйте переформулировать вопрос.")

    try:
        executed_sql = security.validate_sql(sql)
        db_result = security.execute_sql(sql)
    except security.SQLSecurityError as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        _safe_log_audit(
            username=username,
            role=role,
            question=user_input,
            generated_sql=sql,
            executed_sql=None,
            verdict="rejected",
            reject_reason=str(e),
            row_count=None,
            duration_ms=duration_ms,
            llm_ms=llm_ms,
            model=ai_agent.MODEL_NAME,
        )
        return ChatResponse(answer=f"[Запрос отклонён проверкой безопасности] {e}", sql=sql)

    row_count = len(db_result.splitlines()) if db_result and not db_result.startswith("[Ошибка") else 0

    interpret_input = (
        f"Исходный вопрос пользователя:\n{user_input}\n\n"
        f"Выполненный SQL-запрос:\n{sql}\n\n"
        f"Результат из базы данных:\n{db_result}"
    )
    llm_start = time.monotonic()
    human_answer = ai_agent.call_gpt(interpret_system_prompt, interpret_input)
    llm_ms += int((time.monotonic() - llm_start) * 1000)

    duration_ms = int((time.monotonic() - start_time) * 1000)
    _safe_log_audit(
        username=username,
        role=role,
        question=user_input,
        generated_sql=sql,
        executed_sql=executed_sql,
        verdict="ok",
        reject_reason=None,
        row_count=row_count,
        duration_ms=duration_ms,
        llm_ms=llm_ms,
        model=ai_agent.MODEL_NAME,
    )

    return ChatResponse(answer=human_answer, sql=sql)