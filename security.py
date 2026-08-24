# Переменная, в которую передаётся текст из другого Python-кода
# Использование из другого модуля:
#   import security
#   security.received_text = "какой-то текст"
received_text = ""

# (опционально) функция-помощник для установки текста
def set_text(text: str) -> None:
    global received_text
    received_text = text