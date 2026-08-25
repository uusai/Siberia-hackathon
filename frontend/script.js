const API_URL = "http://localhost:8000/chat";

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");

function addMessage(text, role) {
    const div = document.createElement("div");
    div.className = `message ${role}`;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

async function sendMessage() {
    const question = inputEl.value.trim();
    if (!question) return;

    addMessage(question, "user");
    inputEl.value = "";
    sendBtn.disabled = true;

    const loadingEl = addMessage("Агент думает...", "loading");

    try {
        const response = await fetch(API_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question }),
        });

        if (!response.ok) {
            throw new Error(`Сервер ответил с ошибкой: ${response.status}`);
        }

        const data = await response.json();
        loadingEl.remove();
        addMessage(data.answer ?? "Пустой ответ от сервера.", "agent");
    } catch (err) {
        loadingEl.remove();
        addMessage(`Ошибка соединения с сервером: ${err.message}`, "agent");
    } finally {
        sendBtn.disabled = false;
        inputEl.focus();
    }
}

sendBtn.addEventListener("click", sendMessage);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage();
});
