"""Ограничение частоты обращений.

Зачем это появилось. На стенде четыре учётки, и пароль у каждой совпадает с
логином (backend/scripts/seed_auth_users.py). Проверка пароля стоит сотни
миллисекунд (bcrypt, cost 12), но это единственное, что мешало перебору:
счётчика попыток не было вообще, неудачные входы нигде не отмечались, и
подобрать пароль можно было молча и без ограничений.

Второй потребитель — публичный виджет. Он выдаёт гостевые токены без пароля,
то есть без ограничения любой посетитель мог бы жечь квоту Yandex GPT.

Счётчики живут В ПАМЯТИ ПРОЦЕССА и это осознанно. Писать в БД на каждую
неудачную попытку значило бы отдать тому же перебору ещё и запись. Для одного
бэкенд-контейнера памяти достаточно; при нескольких репликах ограничение
станет «N попыток на реплику», что для стенда приемлемо, а для реального
развёртывания решается общим хранилищем (Redis).
"""

import os
import threading
import time


class SlidingWindow:
    """Счётчик обращений в скользящем окне.

    Одна механика на всех потребителей: у входа ключ — имя пользователя, у
    гостевых токенов — адрес. Держать под это две почти одинаковые копии
    кода не стоит, они разъедутся при первой же правке окна.
    """

    # Сколько ключей помним. Защита от роста памяти, если перебирают ещё и
    # логины: без потолка словарь рос бы на каждое выдуманное имя.
    MAX_TRACKED = 4096

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def _fresh(self, hits: list[float], now: float) -> list[float]:
        return [moment for moment in hits if now - moment < self.window_s]

    def retry_after(self, key: str) -> int:
        """Сколько секунд ждать. 0 — можно."""
        now = time.monotonic()
        with self._lock:
            hits = self._fresh(self._hits.get(key, []), now)
            if not hits:
                self._hits.pop(key, None)
                return 0
            self._hits[key] = hits
            if len(hits) < self.limit:
                return 0
            # Ждать до истечения самого старого обращения в окне.
            return max(1, int(self.window_s - (now - hits[0])) + 1)

    def hit(self, key: str) -> int:
        """Отмечает обращение. Возвращает их число в текущем окне."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= self.MAX_TRACKED and key not in self._hits:
                # Переполнение: выкидываем ключи с истёкшим окном.
                for stale in [k for k, v in self._hits.items()
                              if not self._fresh(v, now)]:
                    del self._hits[stale]
            hits = self._fresh(self._hits.get(key, []), now)
            hits.append(now)
            self._hits[key] = hits
            return len(hits)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


# --------------------------------------------------------------------------
# Вход по паролю
# --------------------------------------------------------------------------
#
# Порог подобран под демо: человек ошибается два-три раза, перебор — тысячи.
#
# Ключ — имя пользователя, а не адрес. Адрес в докер-раскладке один на всех
# (запросы идут через контейнер фронтенда), поэтому блокировка по нему
# выключила бы вход сразу всем.
MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
WINDOW_S = float(os.getenv("LOGIN_WINDOW_S", "300"))

_login = SlidingWindow(MAX_ATTEMPTS, WINDOW_S)


def retry_after(username: str) -> int:
    """Сколько секунд ждать перед следующей попыткой входа. 0 — можно.

    Вызывается ДО проверки пароля: смысл ограничения в том, чтобы не тратить
    на перебор ни bcrypt, ни соединение с БД.
    """
    return _login.retry_after(username)


def register_failure(username: str) -> int:
    """Отмечает неудачный вход. Возвращает число попыток в текущем окне."""
    return _login.hit(username)


def reset(username: str) -> None:
    """Успешный вход обнуляет счётчик."""
    _login.reset(username)


# --------------------------------------------------------------------------
# Гостевые обращения из публичного виджета
# --------------------------------------------------------------------------
#
# Здесь ключ — адрес: пользователя как такового нет, токен выдаётся без
# пароля. Лимит заметно выше «человеческого» темпа разговора, но заведомо
# ниже темпа скрипта.
GUEST_MAX = int(os.getenv("GUEST_MAX_REQUESTS", "30"))
GUEST_WINDOW_S = float(os.getenv("GUEST_WINDOW_S", "600"))

_guest = SlidingWindow(GUEST_MAX, GUEST_WINDOW_S)


def guest_retry_after(address: str) -> int:
    return _guest.retry_after(address)


def note_guest_request(address: str) -> int:
    return _guest.hit(address)


def clear() -> None:
    """Полный сброс всех счётчиков — для тестов."""
    _login.clear()
    _guest.clear()
