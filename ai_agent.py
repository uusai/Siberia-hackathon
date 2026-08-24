import os
import sys
import json
import urllib.request
import urllib.error

def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


load_dotenv()

API_KEY = os.getenv("API_KEY")
FOLDER_ID = os.getenv("FOLDER_ID")
MODEL_NAME = os.getenv("MODEL_NAME")
SYSTEM_PROMPT = os.getenv("AGENT_SYSTEM_PROMPT")
API_URL = os.getenv("API_URL")


def build_model_uri() -> str:
    return f"gpt://{FOLDER_ID}/{MODEL_NAME}"


def ask_agent(history: list) -> str:
    messages = [{"role": "system", "text": SYSTEM_PROMPT}]
    for role, text in history:
        messages.append({"role": role, "text": text})

    body = {
        "modelUri": build_model_uri(),
        "completionOptions": {
            "stream": False,
            "temperature": 0.6,
            "maxTokens": 2000,
        },
        "messages": messages,
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return f"[Ошибка API {e.code}] {e.read().decode('utf-8', errors='replace')}"
    except urllib.error.URLError as e:
        return f"[Ошибка сети] {e.reason}"
    except (KeyError, IndexError, ValueError) as e:
        return f"[Ошибка разбора ответа] {e}"

    try:
        return data["result"]["alternatives"][0]["message"]["text"]
    except (KeyError, IndexError) as e:
        return f"[Неожиданный ответ] {data}"


def main() -> None:
    global FOLDER_ID
    if not FOLDER_ID:
        FOLDER_ID = input("Введите YANDEX FOLDER ID: ").strip()
        if not FOLDER_ID:
            print("Folder ID обязателен для работы Yandex GPT. Выход.")
            sys.exit(1)

    print("=" * 50)
    print("  ИИ-агент на базе Yandex GPT")
    print(f"  Модель: {MODEL_NAME}  |  Folder: {FOLDER_ID}")
    print("  Введите 'exit' или 'quit' для выхода")
    print("=" * 50)

    history: list = []

    while True:
        try:
            user_input = input("\nВы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nЗавершение работы.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "выход"):
            print("До свидания!")
            break

        history.append(("user", user_input))
        print("Агент думает...")
        reply = ask_agent(history)
        print(f"Агент: {reply}")
        history.append(("assistant", reply))

        if len(history) > 10:
            history = history[-10:]


if __name__ == "__main__":
    main()