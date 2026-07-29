import os, json, uuid, logging
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from agent import solve

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO)
app = FastAPI()
HISTORY = {}


def send(chat_id: int, text: str):
    httpx.post(f"{API}/sendMessage",
               json={"chat_id": chat_id, "text": text}, timeout=30)


def handle(chat_id: int, text: str):
    run_id = uuid.uuid4().hex
    log_url = (f"https://raw.githubusercontent.com/abhilash404/tele-bot"
               f"/main/runs/{run_id}.jsonl")
    hist = HISTORY.setdefault(chat_id, [])
    hist.append({"role": "user", "content": text})
    del hist[:-6]
    try:
        answer = solve(hist, run_id)
    except Exception:
        logging.exception("solve failed")
        answer = None
    send(chat_id, json.dumps({"answer": answer, "log_url": log_url},
                             ensure_ascii=False))


@app.get("/")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    update = await request.json()
    msg = update.get("message") or update.get("edited_message") or {}
    text, chat = msg.get("text"), (msg.get("chat") or {}).get("id")
    if text and chat is not None:
        bg.add_task(handle, chat, text)
    return {"ok": True}