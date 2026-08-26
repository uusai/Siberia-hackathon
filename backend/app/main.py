import sys
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import ai_agent
from . import db
from . import security
from .auth import (
    create_access_token,
    get_current_user,
    get_user_by_username,
    verify_password,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Закрываем пулы явно: иначе psycopg пытается дожать свои потоки уже на
    # финализации интерпретатора и падает с PythonFinalizationError.
    db.close_all()


app = FastAPI(lifespan=lifespan)

# allow_origins оставлен проницаемым ради хакатонского демо (фронтенд может
# быть открыт как файл или с произвольного порта). Перед любым реальным
# развёртыванием заменить "*" на список конкретных доменов.
# allow_headers сузили с "*" до явного списка: фронтенду нужен только
# Authorization для Bearer-токена и Content-Type для JSON-тела.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)

sql_system_prompt = ai_agent.build_sql_system_prompt()
interpret_system_prompt = ai_agent.build_interpret_system_prompt()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class ChatRequest(BaseModel):
    question: str


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
    # Намеренно без авторизации: фронтенд пингует этот эндпоинт до логина.
    return {"status": "ok"}


@app.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    user = get_user_by_username(request.username)
    # Ответ одинаков и для несуществующего логина, и для неверного пароля,
    # чтобы перебором нельзя было выяснить, какие учётки существуют.
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = create_access_token(username=user["username"], role=user["role"])
    return LoginResponse(access_token=token, role=user["role"])


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    start_time = time.monotonic()
    llm_ms = 0
    # username и role берутся ИЗ ПРОВЕРЕННОГО ТОКЕНА, а не из тела запроса.
    username = current_user["username"]
    role = current_user["role"]
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

    # Валидация выполняется РОВНО ОДИН РАЗ: дальше в БД уходит уже проверенный
    # текст, execute_validated_sql() его не перепроверяет.
    try:
        executed_sql = security.validate_sql(sql)
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

    db_result = security.execute_validated_sql(executed_sql)

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
