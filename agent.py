import os, json, base64, subprocess, tempfile, textwrap, httpx
from openai import OpenAI
from bs4 import BeautifulSoup


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
    except Exception as e:
        return f"ERROR: {e}"

    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        return r.text[:8000]          # csv/json/xml - return raw

    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = " ".join(soup.get_text(" ").split())

    if len(text) < 300:
        links = [a.get("href") for a in soup.find_all("a", href=True)][:30]
        return ("EMPTY_PAGE: This is a JavaScript-rendered page with no static "
                "content. Fetching more paths on this site will NOT work. "
                "Use web_search to find a direct data file (.csv/.xlsx/.json) "
                f"or an alternative source. Links found: {links}")
    return text[:8000]

def web_search(query: str) -> str:
    try:
        r = httpx.post("https://html.duckduckgo.com/html/",
                       data={"q": query}, timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for res in soup.select(".result")[:8]:
            a = res.select_one(".result__a")
            s = res.select_one(".result__snippet")
            if a:
                out.append(f"{a.get_text()} | {a.get('href')} | "
                           f"{s.get_text() if s else ''}")
        return "\n".join(out) or "No results."
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
    You are a data analyst agent.

    OUTPUT CONTRACT (critical):
    The user's message shows a JSON template containing "answer" and
    "log_url" keys. You must output ONLY the value that belongs inside
    "answer". Never output the outer object. Never output a log_url -
    that is filled in by the system.
    Example: if asked for {"answer": {"state": "<name>"}, "log_url": "..."}
    you output exactly: {"state": "Kerala"}

    METHOD:
    - Use web_search first to locate a real data source. Prefer direct
      .csv / .xlsx / .json files and government PDFs over landing pages.
    - Many government sites (including mospi.gov.in) are JavaScript SPAs
      that return an empty shell. If fetch_url returns EMPTY_PAGE, do NOT
      try more paths on that domain - search for the data file instead.
    - Use run_python for ALL computation. Never do arithmetic mentally.
    - If after your attempts you cannot find real data, still return a
      best-effort answer in the required shape, but call run_python once
      to print your reasoning so the log records what you tried.

    Output only the answer value. No prose, no markdown fences.
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

    if isinstance(answer, dict) and "log_url" in answer and "answer" in answer:
        answer = answer["answer"]

    log.append({"step": "final", "answer": answer})
    try:
        commit_log(run_id, log)
    except Exception as e:
        log.append({"commit_error": str(e)})
    return answer