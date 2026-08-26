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
    aget_user_by_username,
    averify_password,
    create_access_token,
    get_current_user,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Закрываем пулы явно: иначе psycopg пытается дожать свои потоки уже на
    # финализации интерпретатора и падает с PythonFinalizationError.
    await db.aclose_all()


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

# Промпт свой на каждую роль: схема в нём урезана до того, что роли
# действительно доступно. Иначе модель уверенно строит запрос к закрытой
# таблице, проверка его отклоняет, и пользователь видит отказ вместо ответа.
SQL_PROMPTS: dict[str, str] = {
    role: ai_agent.build_sql_system_prompt(role)
    for role in security.ALLOWED_TABLES_BY_ROLE
}
interpret_system_prompt = ai_agent.build_interpret_system_prompt()

# Схема в промпт тянется из БД живьём, поэтому новая таблица увеличивает его
# молча. Печатаем размер на старте: раздувание промпта проще заметить в логе
# запуска, чем по деградации ответов на защите.
for _role, _prompt in sorted(SQL_PROMPTS.items()):
    print(
        f"[prompt] {_role}: {len(_prompt)} символов, "
        f"{len(security.allowed_tables_for(_role))} объектов БД",
        file=sys.stderr,
    )


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


async def _safe_log_audit(**kwargs) -> None:
    try:
        await security.alog_audit_entry(**kwargs)
    except Exception as e:
        print(f"[audit_log] Не удалось записать запись аудита: {e}", file=sys.stderr)


@app.get("/health")
async def health():
    # Намеренно без авторизации: фронтенд пингует этот эндпоинт до логина.
    return {"status": "ok"}


@app.get("/meta/data-status")
async def data_status(current_user: dict = Depends(get_current_user)):
    """Сводка «сколько данных официальных, а сколько демонстрационных».

    Под авторизацией, как и /chat: эндпоинт служебный. Читает готовое
    представление assistant.data_status_summary (миграция 008); если она ещё
    не применена, честно отвечает, что сводка недоступна, а не падает.
    """
    try:
        rows = await db.afetch_all(
            "assistant",
            "SELECT table_name, data_status, rows FROM data_status_summary "
            "ORDER BY table_name, data_status",
            read_only=True,
        )
    except Exception as e:
        return {"available": False, "reason": str(e).strip()}

    summary: dict[str, dict[str, int]] = {}
    for table, status, count in rows:
        summary.setdefault(table, {})[status] = count
    return {"available": True, "tables": summary}


@app.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    user = await aget_user_by_username(request.username)
    # Ответ одинаков и для несуществующего логина, и для неверного пароля,
    # чтобы перебором нельзя было выяснить, какие учётки существуют.
    if user is None or not await averify_password(
        request.password, user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = create_access_token(
        username=user["username"],
        role=user["role"],
        student_id=user.get("student_id"),
        teacher_id=user.get("teacher_id"),
    )
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

    sql_system_prompt = SQL_PROMPTS.get(role)
    if sql_system_prompt is None:
        # Роль из токена не совпала ни с одной известной: отказ, а не
        # доступ ко всему.
        raise HTTPException(status_code=403, detail=f"Неизвестная роль: {role}")

    # Личность для личных вьюх my_*. Берётся только из проверенного токена
    # и уходит в сессионную переменную транзакции — сгенерированный моделью
    # SQL на неё повлиять не может (set_config запрещён проверкой).
    session_vars = {}
    if current_user.get("student_id") is not None:
        session_vars["app.student_id"] = current_user["student_id"]
    if current_user.get("teacher_id") is not None:
        session_vars["app.teacher_id"] = current_user["teacher_id"]

    if not user_input:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        await _safe_log_audit(
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
    sql_reply = await ai_agent.acall_gpt(sql_system_prompt, user_input)
    llm_ms += int((time.monotonic() - llm_start) * 1000)
    sql = ai_agent.extract_sql(sql_reply)

    if not sql:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        await _safe_log_audit(
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
        executed_sql = security.validate_sql(sql, role)
    except security.SQLSecurityError as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        await _safe_log_audit(
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
        # Пользователю — объяснение словами, в audit_log — точная причина
        # отказа (она уже записана выше в reject_reason).
        return ChatResponse(answer=security.explain_rejection(e), sql=sql)

    db_result = await security.aexecute_validated_sql(
        executed_sql, session_vars=session_vars
    )

    # Одна попытка самоисправления. СУБД в ошибке называет проблему точно
    # («column "semester" does not exist»), и модель по такой подсказке чинит
    # запрос с первого раза. Без этого пользователь видел бы текст ошибки
    # вместо ответа — при том, что нужная колонка есть в схеме прямо в
    # промпте. Повтор ровно один: если и он не помог, дело не в опечатке.
    if db_result.startswith("[Ошибка БД]"):
        llm_start = time.monotonic()
        retry_reply = await ai_agent.acall_gpt(
            sql_system_prompt,
            ai_agent.build_correction_input(user_input, sql, db_result),
        )
        llm_ms += int((time.monotonic() - llm_start) * 1000)
        retry_sql = ai_agent.extract_sql(retry_reply)
        if retry_sql:
            try:
                retry_executed = security.validate_sql(retry_sql, role)
            except security.SQLSecurityError:
                retry_executed = None
            if retry_executed:
                retry_result = await security.aexecute_validated_sql(
                    retry_executed, session_vars=session_vars
                )
                if not retry_result.startswith("[Ошибка"):
                    sql, executed_sql, db_result = (
                        retry_sql, retry_executed, retry_result,
                    )

    row_count = len(db_result.splitlines()) if db_result and not db_result.startswith("[Ошибка") else 0

    interpret_input = (
        f"Исходный вопрос пользователя:\n{user_input}\n\n"
        f"Выполненный SQL-запрос:\n{sql}\n\n"
        f"Результат из базы данных:\n{db_result}"
    )
    llm_start = time.monotonic()
    human_answer = await ai_agent.acall_gpt(interpret_system_prompt, interpret_input)
    llm_ms += int((time.monotonic() - llm_start) * 1000)

    duration_ms = int((time.monotonic() - start_time) * 1000)
    await _safe_log_audit(
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
