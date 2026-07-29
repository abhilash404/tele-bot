import os, json, uuid, logging
import httpx
from fastapi import FastAPI, Request

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
GH_USER = os.environ.get("GH_USER", "abhilash404")
GH_REPO = os.environ.get("GH_REPO", "tele-bot")

logging.basicConfig(level=logging.INFO)
app = FastAPI()


async def send(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20) as c:
        await c.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})


async def solve(question: str, run_id: str) -> dict:
    # TODO: real agent goes here. For now, echo.
    return {"received": question[:200]}


@app.get("/")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()
    logging.info("update: %s", json.dumps(update)[:500])

    msg = update.get("message") or update.get("edited_message") or {}
    text = msg.get("text")
    chat_id = (msg.get("chat") or {}).get("id")
    if not text or chat_id is None:
        return {"ok": True}

    run_id = uuid.uuid4().hex
    log_url = (
        f"https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}"
        f"/main/runs/{run_id}.jsonl"
    )

    try:
        answer = await solve(text, run_id)
    except Exception:
        logging.exception("solve failed")
        answer = None

    await send(chat_id, json.dumps({"answer": answer, "log_url": log_url},
                                   ensure_ascii=False))
    return {"ok": True}