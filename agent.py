import os, json, base64, subprocess, tempfile, textwrap, httpx
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
)
MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")

GH_TOKEN = os.environ["GH_TOKEN"]
REPO = "abhilash404/tele-bot"

TOOLS = [
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "HTTP GET a URL. Returns up to 20000 chars of the body.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": ("Run Python 3. pandas, httpx, bs4, lxml available. "
                        "Print results to stdout. 60s timeout."),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}}, "required": ["code"]}}},
]


def fetch_url(url: str) -> str:
    try:
        r = httpx.get(url, timeout=45, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        return r.text[:20000]
    except Exception as e:
        return f"ERROR: {e}"


def run_python(code: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run(["python", path], capture_output=True,
                           text=True, timeout=60)
        return (p.stdout + p.stderr)[:20000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: timed out after 60s"


def commit_log(run_id: str, lines: list) -> None:
    body = "\n".join(json.dumps(x, default=str) for x in lines)
    httpx.put(
        f"https://api.github.com/repos/{REPO}/contents/runs/{run_id}.jsonl",
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        json={"message": f"run {run_id}",
              "content": base64.b64encode(body.encode()).decode(),
              "branch": "main"},
        timeout=30,
    )


SYSTEM = textwrap.dedent("""
    You are a data analyst. Answer the user's final question using the tools.
    Prefer run_python for any computation; never do arithmetic in your head.
    When you have the answer, reply with ONLY the JSON value the question
    asked for - no prose, no markdown fences, no explanation.
""").strip()


def solve(history: list, run_id: str) -> tuple:
    log = []
    messages = [{"role": "system", "content": SYSTEM}] + history

    for i in range(8):
        r = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0)
        m = r.choices[0].message
        messages.append(m.model_dump(exclude_none=True))
        log.append({"step": i, "role": "assistant", "content": m.content,
                    "tool_calls": [t.function.name for t in (m.tool_calls or [])]})

        if not m.tool_calls:
            break

        for tc in m.tool_calls:
            args = json.loads(tc.function.arguments)
            fn = {"fetch_url": fetch_url, "run_python": run_python}[tc.function.name]
            out = fn(**args)
            log.append({"step": i, "role": "tool", "name": tc.function.name,
                        "args": args, "output": out[:4000]})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out})
    else:
        messages.append({"role": "user",
                         "content": "Stop using tools. Give the final JSON answer now."})
        m = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=0).choices[0].message

    raw = (m.content or "").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        answer = json.loads(raw)
    except Exception:
        answer = raw

    log.append({"step": "final", "answer": answer})
    try:
        commit_log(run_id, log)
    except Exception as e:
        log.append({"commit_error": str(e)})
    return answer