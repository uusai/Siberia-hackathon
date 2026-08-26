import sys
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import ai_agent
from . import db
from . import security
from . import throttle
from .auth import (
    TOKEN_TTL_HOURS,
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

STARTED_AT = time.monotonic()

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

# Роли, которым доступны служебные эндпоинты. Сводка о происхождении данных и
# журнал обращений — инструменты владельца системы, а не ответ пользователю:
# по ним видно, где данные демонстрационные и какие запросы отклонялись.
ADMIN_ROLES = {"administration"}


def log(event: str, **fields) -> None:
    """Одна строка на событие в stderr.

    До этого в лог попадали только сбои, поэтому `docker compose logs backend`
    на рабочем стенде оставался пустым — притом что мониторинг и логирование
    отдельно отмечены как то, что нужно довести до защиты. Формат «ключ=значение»
    выбран за то, что читается и глазами, и grep'ом.
    """
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"{key}={text!r}" if " " in text else f"{key}={text}")
    print(f"[{event}] " + " ".join(parts), file=sys.stderr, flush=True)


# Схема в промпт тянется из БД живьём, поэтому новая таблица увеличивает его
# молча. Печатаем размер на старте: раздувание промпта проще заметить в логе
# запуска, чем по деградации ответов на защите.
for _role, _prompt in sorted(SQL_PROMPTS.items()):
    log(
        "prompt",
        role=_role,
        chars=len(_prompt),
        objects=len(security.allowed_tables_for(_role)),
    )


def _warn_about_weak_secret() -> None:
    """Предупреждение о коротком JWT_SECRET.

    Подпись HS256 стойка ровно настолько, насколько длинен секрет: короткий
    подбирается по перехваченному токену офлайн, а подделанный токен — это
    любая роль на выбор, включая administration. Приложение не останавливаем
    (стенд должен подниматься), но молчать об этом нельзя.
    """
    import os

    secret = os.getenv("JWT_SECRET") or ""
    if len(secret.encode("utf-8")) < 32:
        log(
            "warning",
            what="JWT_SECRET короче 32 байт",
            bytes=len(secret.encode("utf-8")),
            action="сгенерируйте длинный секрет перед защитой: "
                   "python -c \"import secrets; print(secrets.token_urlsafe(48))\"",
        )


_warn_about_weak_secret()


class LoginRequest(BaseModel):
    # Ограничения длины: без них в bcrypt и в БД уезжает сколько угодно текста.
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class ChatRequest(BaseModel):
    # Фронтенд ограничивает поле 600 символами, но прямой POST его не спрашивает,
    # а вопрос целиком уезжает в промпт модели.
    question: str = Field(max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None
    # Исход обращения — тот же, что уходит в audit_log.
    #
    # Нужен клиенту, чтобы отличить ОТКАЗ ПО ПРАВИЛУ от СБОЯ: «эти данные не
    # положены вашей роли» и «база не ответила» выглядят одинаково только на
    # первый взгляд. Раньше фронтенд угадывал это регулярным выражением по
    # тексту ответа, искал «[Запрос отклонён…]» — а explain_rejection() давно
    # отдаёт человеческую фразу без скобок, и условие не срабатывало ни разу:
    # отказы рисовались как обычные ответы.
    verdict: str = "ok"
    # Сколько занял весь путь: вопрос -> модель -> SQL -> проверка -> база ->
    # ответ. На защите это спрашивают первым делом.
    took_ms: int | None = None
    # Строки из БД. Раньше их не было вовсе: результат склеивался в текст только
    # для промпта, а фронтенд пытался выпарсить таблицу обратно из прозы модели —
    # и почти никогда не мог, потому что проза таблицей не является. Объём
    # ограничен security.MAX_LIMIT строк.
    columns: list[str] | None = None
    rows: list[list[str]] | None = None


async def _safe_log_audit(**kwargs) -> None:
    try:
        await security.alog_audit_entry(**kwargs)
    except Exception as e:
        log("audit_log", error=f"не удалось записать запись аудита: {e}")


async def _audit_login(username: str, verdict: str, reason: str | None,
                       duration_ms: int) -> None:
    """Исход входа в тот же журнал, что и вопросы.

    Раньше сюда попадали только обращения к /chat, поэтому подбор пароля не
    оставлял в системе никакого следа. Пароль не логируется: в наборе полей
    log_audit_entry его нет.
    """
    await _safe_log_audit(
        username=username,
        role=None,
        question="",
        generated_sql=None,
        executed_sql=None,
        verdict=verdict,
        reject_reason=reason,
        row_count=None,
        duration_ms=duration_ms,
        llm_ms=0,
        model=ai_agent.MODEL_NAME,
    )


# Результат проверки БД кэшируется: /health опрашивается каждым открытым
# браузером раз в 25 секунд, и ходить в базу на каждый такой запрос незачем.
_HEALTH_CACHE_S = 15.0
_health_cache: tuple[float, bool] | None = None


async def _db_is_alive() -> bool:
    global _health_cache
    now = time.monotonic()
    if _health_cache is not None and now - _health_cache[0] < _HEALTH_CACHE_S:
        return _health_cache[1]
    alive = await db.aping()
    _health_cache = (now, alive)
    return alive


@app.get("/health")
async def health():
    """Живость сервиса и связь с БД.

    Намеренно без авторизации: фронтенд пингует этот эндпоинт до логина.
    Раньше отвечал «ok» безусловно — то есть при лежащей базе индикатор в
    интерфейсе показывал «на связи», а любой вопрос падал. Проверка идёт через
    db.ping(): одна попытка с коротким таймаутом, без повторов.
    """
    alive = await _db_is_alive()
    return {
        "status": "ok",
        "db": "ok" if alive else "down",
        "uptime_s": int(time.monotonic() - STARTED_AT),
    }


def _require_admin(current_user: dict) -> None:
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Этот раздел доступен только администрации.",
        )


@app.get("/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    """Состав проверенного токена.

    Фронтенд держит роль в localStorage и без этого эндпоинта не может её
    перепроверить: подменённое там значение меняло бы подсказки на экране до
    первого запроса. Источник истины — подпись токена, а не браузер.
    """
    return {
        "username": current_user["username"],
        "role": current_user["role"],
        "linked_person": bool(
            current_user.get("student_id") or current_user.get("teacher_id")
        ),
        "token_ttl_hours": TOKEN_TTL_HOURS,
    }


@app.get("/meta/data-status")
async def data_status(current_user: dict = Depends(get_current_user)):
    """Сводка «сколько данных официальных, а сколько демонстрационных».

    Только администрации: эндпоинт служебный и показывает, где в базе стоят
    демонстрационные значения. Читает готовое представление
    assistant.data_status_summary (миграция 008); если она ещё не применена,
    честно отвечает, что сводка недоступна, а не падает.
    """
    _require_admin(current_user)
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


@app.get("/meta/stats")
async def stats(current_user: dict = Depends(get_current_user)):
    """Сводка по журналу обращений: чем система занималась и как быстро.

    Журнал писался с самого начала, но посмотреть его было нечем — ни
    эндпоинта, ни отчёта. На защите «у нас есть логирование» подтверждается
    цифрами отсюда: сколько вопросов, сколько отклонено и почему, за какое
    время отвечаем и какая доля времени уходит в модель.
    """
    _require_admin(current_user)

    try:
        totals = await db.afetch_all(
            "assistant",
            "SELECT verdict, count(*), "
            "       count(*) FILTER (WHERE ts > now() - interval '24 hours') "
            "FROM audit_log GROUP BY verdict ORDER BY 2 DESC",
            read_only=True,
        )
        timings = await db.afetch_all(
            "assistant",
            "SELECT count(*), "
            "       round(percentile_cont(0.5) WITHIN GROUP "
            "             (ORDER BY duration_ms))::int, "
            "       round(percentile_cont(0.95) WITHIN GROUP "
            "             (ORDER BY duration_ms))::int, "
            "       round(avg(llm_ms))::int, "
            "       round(avg(row_count))::int "
            "FROM audit_log WHERE duration_ms IS NOT NULL",
            read_only=True,
        )
        reasons = await db.afetch_all(
            "assistant",
            "SELECT reject_reason, count(*) FROM audit_log "
            "WHERE reject_reason IS NOT NULL "
            "GROUP BY reject_reason ORDER BY 2 DESC LIMIT 10",
            read_only=True,
        )
    except Exception as e:
        return {"available": False, "reason": str(e).strip()}

    count, median_ms, p95_ms, avg_llm_ms, avg_rows = (
        timings[0] if timings else (0, None, None, None, None)
    )
    return {
        "available": True,
        "by_verdict": [
            {"verdict": v, "total": total, "last_24h": recent}
            for v, total, recent in totals
        ],
        "timing_ms": {
            "measured_requests": count,
            "median": median_ms,
            "p95": p95_ms,
            "avg_in_model": avg_llm_ms,
        },
        "avg_rows_returned": avg_rows,
        "top_reject_reasons": [
            {"reason": reason, "count": n} for reason, n in reasons
        ],
    }


def _client_address(request: Request) -> str:
    """Адрес обратившегося.

    За обратным прокси реальный адрес приходит в X-Forwarded-For (первый в
    списке — исходный клиент). Без прокси берётся адрес соединения. Заголовку
    доверяем осознанно: он используется ТОЛЬКО для ограничения частоты, права
    доступа от него не зависят никак. Худшее, чего добьётся подделавший его, —
    обойдёт собственный лимит, и это не повод усложнять раскладку стенда.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/auth/guest", response_model=LoginResponse)
async def guest(request: Request):
    """Токен для встраиваемого виджета — без пароля и без учётной записи.

    Посетитель сайта вуза не заводит логин, чтобы спросить, что сдавать на
    прикладную информатику. Поэтому виджет получает токен роли `guest`, у
    которой в whitelist только официальный справочник приёма
    (security._GUEST_TABLES) — ровно то, что и так опубликовано на сайте.

    Ограничение по адресу здесь обязательно: без пароля токен может взять
    кто угодно, а каждый вопрос — это обращения к платной модели.
    """
    address = _client_address(request)
    wait_s = throttle.guest_retry_after(address)
    if wait_s:
        log("guest", addr=address, verdict="throttled", retry_after=wait_s)
        raise HTTPException(
            status_code=429,
            detail="Слишком много обращений. Попробуйте чуть позже.",
            headers={"Retry-After": str(wait_s)},
        )
    throttle.note_guest_request(address)

    token = create_access_token(username=f"guest:{address}", role="guest")
    log("guest", addr=address, verdict="ok")
    return LoginResponse(access_token=token, role="guest")


@app.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    started = time.monotonic()
    username = request.username.strip()

    # Ограничитель ДО проверки пароля: смысл в том, чтобы перебор не тратил ни
    # bcrypt (сотни миллисекунд на попытку), ни соединение с БД.
    wait_s = throttle.retry_after(username)
    if wait_s:
        await _audit_login(
            username, "login_throttled", f"ждать {wait_s} с",
            int((time.monotonic() - started) * 1000),
        )
        log("login", user=username, verdict="throttled", retry_after=wait_s)
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток входа. Попробуйте позже.",
            headers={"Retry-After": str(wait_s)},
        )

    user = await aget_user_by_username(username)
    # Ответ одинаков и для несуществующего логина, и для неверного пароля,
    # чтобы перебором нельзя было выяснить, какие учётки существуют.
    if user is None or not await averify_password(
        request.password, user["password_hash"]
    ):
        attempts = throttle.register_failure(username)
        await _audit_login(
            username, "login_failed", f"попытка {attempts} из {throttle.MAX_ATTEMPTS}",
            int((time.monotonic() - started) * 1000),
        )
        log("login", user=username, verdict="failed", attempt=attempts)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    throttle.reset(username)
    token = create_access_token(
        username=user["username"],
        role=user["role"],
        student_id=user.get("student_id"),
        teacher_id=user.get("teacher_id"),
    )
    await _audit_login(
        username, "login_ok", None, int((time.monotonic() - started) * 1000)
    )
    log("login", user=username, role=user["role"], verdict="ok")
    return LoginResponse(access_token=token, role=user["role"])


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    http_request: Request,
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

    # Гость платит за квоту модели чужими деньгами: токен ему выдали без
    # пароля, поэтому считать надо не только выдачу токенов, но и сами
    # вопросы — иначе одним токеном можно опустошить квоту.
    if role == "guest":
        address = _client_address(http_request)
        wait_s = throttle.guest_retry_after(address)
        if wait_s:
            log("chat", role=role, verdict="throttled", retry_after=wait_s)
            raise HTTPException(
                status_code=429,
                detail="Слишком много вопросов подряд. Попробуйте чуть позже.",
                headers={"Retry-After": str(wait_s)},
            )
        throttle.note_guest_request(address)

    # Личность для личных вьюх my_*. Берётся только из проверенного токена
    # и уходит в сессионную переменную транзакции — сгенерированный моделью
    # SQL на неё повлиять не может (set_config запрещён проверкой).
    session_vars = {}
    if current_user.get("student_id") is not None:
        session_vars["app.student_id"] = current_user["student_id"]
    if current_user.get("teacher_id") is not None:
        session_vars["app.teacher_id"] = current_user["teacher_id"]

    async def finish(answer, verdict, *, sql=None, executed_sql=None,
                     reason=None, result=None):
        """Единая точка выхода: журнал, строка в лог, ответ."""
        duration_ms = int((time.monotonic() - start_time) * 1000)
        await _safe_log_audit(
            username=username,
            role=role,
            question=user_input,
            generated_sql=sql,
            executed_sql=executed_sql,
            verdict=verdict,
            reject_reason=reason,
            row_count=len(result.rows) if result is not None else None,
            duration_ms=duration_ms,
            llm_ms=llm_ms,
            model=ai_agent.MODEL_NAME,
        )
        log(
            "chat", user=username, role=role, verdict=verdict,
            rows=len(result.rows) if result is not None else None,
            total_ms=duration_ms, llm_ms=llm_ms, reason=reason,
            q=user_input[:80],
        )
        # Таблицу прикладываем, только если в ней есть что показывать: одна
        # строка с NULL от несовпавшего агрегата нарисовалась бы таблицей из
        # единственной пустой ячейки.
        has_table = result is not None and not result.is_blank
        return ChatResponse(
            answer=answer,
            sql=sql,
            verdict=verdict,
            took_ms=duration_ms,
            columns=result.columns if has_table else None,
            rows=result.rows if has_table else None,
        )

    if not user_input:
        return await finish("Пустой вопрос.", "no_sql")

    llm_start = time.monotonic()
    sql_reply = await ai_agent.acall_gpt(sql_system_prompt, user_input)
    llm_ms += int((time.monotonic() - llm_start) * 1000)
    sql = ai_agent.extract_sql(sql_reply)

    if not sql:
        return await finish(
            "Не удалось сгенерировать SQL-запрос. Попробуйте переформулировать "
            "вопрос.",
            "no_sql",
        )

    # Валидация выполняется РОВНО ОДИН РАЗ: дальше в БД уходит уже проверенный
    # текст, execute_validated_sql() его не перепроверяет.
    try:
        executed_sql = security.validate_sql(sql, role)
    except security.SQLSecurityError as e:
        # Модель отказала словами, завернув фразу в SELECT без FROM. Проверка
        # честно говорит «не удалось определить таблицу», но пользователю это
        # ничего не объясняет — он спрашивал не про таблицы. Отвечаем по
        # существу; текст, сочинённый моделью, наружу не отдаём.
        if security.looks_like_a_spoken_refusal(sql):
            return await finish(
                ai_agent.out_of_scope_answer(role),
                "out_of_scope", sql=sql, reason=str(e),
            )
        # Пользователю — объяснение словами, в audit_log — точная причина.
        return await finish(
            security.explain_rejection(e, role), "rejected", sql=sql, reason=str(e)
        )

    result = await security.aexecute_validated_sql_result(
        executed_sql, session_vars=session_vars
    )

    # Одна попытка самоисправления. СУБД в ошибке называет проблему точно
    # («column "semester" does not exist»), и модель по такой подсказке чинит
    # запрос с первого раза. Без этого пользователь видел бы текст ошибки
    # вместо ответа — при том, что нужная колонка есть в схеме прямо в
    # промпте. Повтор ровно один: если и он не помог, дело не в опечатке.
    if result.error:
        llm_start = time.monotonic()
        retry_reply = await ai_agent.acall_gpt(
            sql_system_prompt,
            ai_agent.build_correction_input(user_input, sql, result.error),
        )
        llm_ms += int((time.monotonic() - llm_start) * 1000)
        retry_sql = ai_agent.extract_sql(retry_reply)
        if retry_sql:
            try:
                retry_executed = security.validate_sql(retry_sql, role)
            except security.SQLSecurityError:
                retry_executed = None
            if retry_executed:
                retry_result = await security.aexecute_validated_sql_result(
                    retry_executed, session_vars=session_vars
                )
                if retry_result.ok:
                    sql, executed_sql, result = (
                        retry_sql, retry_executed, retry_result,
                    )

    # ПУСТАЯ ВЫБОРКА НЕ УХОДИТ МОДЕЛИ.
    #
    # Это тот самый случай, на котором система соврала: ноль строк ушли во
    # вторую фазу «на объяснение», и модель заполнила пустоту заглушками —
    # «учатся следующие группы: [название группы 1], [название группы 2]».
    # Объяснять нечего, когда объяснять нечего. Отвечаем сами, честно и
    # детерминированно — заодно экономим обращение к модели.
    #
    # Отдельно разбирается случай «отвечали только поиском по справке и не
    # нашли»: это не «данных нет», а «вопрос не про наши данные» — так и
    # отвечаем (см. ai_agent.empty_result_answer).
    if result.is_blank:
        out_of_scope = ai_agent.is_out_of_scope(executed_sql)
        return await finish(
            ai_agent.out_of_scope_answer(role) if out_of_scope
            else ai_agent.NOTHING_FOUND_ANSWER,
            "out_of_scope" if out_of_scope else "empty",
            sql=sql, executed_sql=executed_sql, result=result,
        )

    interpret_input = (
        f"Исходный вопрос пользователя:\n{user_input}\n\n"
        f"Выполненный SQL-запрос:\n{sql}\n\n"
        f"Результат из базы данных:\n{result.error or result.text}"
    )
    llm_start = time.monotonic()
    human_answer = await ai_agent.acall_gpt(interpret_system_prompt, interpret_input)
    llm_ms += int((time.monotonic() - llm_start) * 1000)

    if result.error:
        return await finish(
            human_answer, "db_error", sql=sql, executed_sql=executed_sql,
            reason=result.error,
        )

    # Данные есть, а модель всё равно выдала бланк или собственные рассуждения.
    # Подменяем текст, но исход пишем в журнал: молчаливой подмены быть не
    # должно, иначе такие случаи перестанут попадаться на глаза.
    blank = ai_agent.blank_answer_reason(human_answer)
    if blank:
        return await finish(
            "Данные по запросу нашлись — они в таблице ниже. Сформулировать "
            "ответ словами не получилось, попробуйте переспросить иначе.",
            "placeholder", sql=sql, executed_sql=executed_sql,
            reason=blank, result=result,
        )

    return await finish(
        human_answer, "ok", sql=sql, executed_sql=executed_sql, result=result
    )
