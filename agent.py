import os, json, base64, subprocess, tempfile, textwrap
import httpx
from bs4 import BeautifulSoup
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ.get("LLM_BASE_URL", "https://aipipe.org/openai/v1"),
)
MODEL = os.environ.get("LLM_MODEL", "gpt-4.1-mini")

GH_TOKEN = os.environ["GH_TOKEN"]
REPO = "abhilash404/tele-bot"

MAX_TOOL_CHARS = 6000
MAX_STEPS = 8

_seen = {}


# ---------------------------------------------------------------- tools

def fetch_url(url: str) -> str:
    if url in _seen:
        return (f"ALREADY TRIED this exact URL. Previous result: "
                f"{_seen[url][:200]} -- try a DIFFERENT source.")

    def _get(verify=True):
        return httpx.get(url, timeout=45, follow_redirects=True, verify=verify,
                         headers={"User-Agent": "Mozilla/5.0"})

    try:
        r = _get()
    except Exception:
        try:
            r = _get(verify=False)          # Indian gov sites: broken cert chains
        except Exception as e:
            out = f"ERROR: {e}"
            _seen[url] = out
            return out

    ctype = r.headers.get("content-type", "").lower()

    if "html" not in ctype:
        out = r.text[:8000]                 # csv / json / xml / plain
        _seen[url] = out
        return out

    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = " ".join(soup.get_text(" ").split())

    if len(text) < 300:
        links = [a.get("href") for a in soup.find_all("a", href=True)][:30]
        out = ("EMPTY_PAGE: JavaScript-rendered page with no static content. "
               "Fetching more paths on this domain will NOT work. Use "
               "web_search to find a direct data file (.csv/.xlsx/.json) or "
               f"another source. Links found: {links}")
    else:
        out = text[:8000]

    _seen[url] = out
    return out


def web_search(query: str) -> str:
    try:
        r = httpx.post("https://html.duckduckgo.com/html/",
                       data={"q": query}, timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "lxml")
        rows = []
        for res in soup.select(".result")[:8]:
            a = res.select_one(".result__a")
            s = res.select_one(".result__snippet")
            if a:
                rows.append(f"{a.get_text(strip=True)} | {a.get('href')} | "
                            f"{s.get_text(strip=True) if s else ''}")
        return "\n".join(rows) or "No results."
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
    except Exception as e:
        return f"ERROR: {e}"


DISPATCH = {"fetch_url": fetch_url,
            "web_search": web_search,
            "run_python": run_python}

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Returns 'title | url | snippet' lines.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": ("HTTP GET a URL. HTML is stripped to text. "
                        "CSV/JSON returned raw."),
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": ("Run Python 3. pandas, httpx, bs4, lxml available. "
                        "Print results to stdout. 60s timeout."),
        "parameters": {"type": "object",
                       "properties": {"code": {"type": "string"}},
                       "required": ["code"]}}},
]


# ---------------------------------------------------------------- logging

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


# ---------------------------------------------------------------- agent

SYSTEM = textwrap.dedent("""
    You are a data analyst agent.

    OUTPUT CONTRACT (critical):
    The user's message shows a JSON template containing "answer" and
    "log_url" keys. Output ONLY the value that belongs inside "answer".
    Never output the outer object. Never output a log_url - the system
    fills that in.
    Example: asked for {"answer": {"state": "<name>"}, "log_url": "..."}
    you output exactly: {"state": "Kerala"}

    METHOD:
    - Start from the search results provided to you. Use web_search again
      if they are not useful.
    - Prefer direct .csv / .xlsx / .json files over landing pages.
    - Many Indian government sites (mospi.gov.in, niti.gov.in) are
      JavaScript SPAs returning an empty shell. If fetch_url returns
      EMPTY_PAGE, do NOT try more paths on that domain.
    - Never fetch the same URL twice. If it failed, change source.
    - Use run_python for ALL computation. Never do arithmetic mentally.
    - If the question embeds the data inline, skip searching entirely and
      go straight to run_python.
    - If you cannot find real data, still return a best-effort answer in
      the required shape, but first call run_python to print what you
      tried, so the log records your reasoning.

    Output only the answer value. No prose, no markdown fences.
""").strip()


def solve(history: list, run_id: str):
    _seen.clear()
    log = []
    question = history[-1]["content"]

    hits = web_search(f"{question} data csv xlsx site:data.gov.in OR site:gov.in")
    log.append({"step": "preseed", "query": question[:300], "results": hits})

    messages = ([{"role": "system", "content": SYSTEM}] + history +
                [{"role": "user",
                  "content": f"Search results that may help:\n{hits}\n\n"
                             "Use these before inventing URLs."}])

    m = None
    for i in range(MAX_STEPS):
        r = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0)
        m = r.choices[0].message
        messages.append(m.model_dump(exclude_none=True))
        log.append({"step": i, "role": "assistant", "content": m.content,
                    "tool_calls": [t.function.name for t in (m.tool_calls or [])],
                    "usage": r.usage.model_dump() if r.usage else None})

        if not m.tool_calls:
            break

        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
                out = DISPATCH[tc.function.name](**args)
            except Exception as e:
                args, out = {"raw": tc.function.arguments}, f"ERROR: {e}"
            log.append({"step": i, "role": "tool", "name": tc.function.name,
                        "args": args, "output": out[:4000]})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out[:MAX_TOOL_CHARS]})
    else:
        messages.append({"role": "user",
                         "content": "Stop using tools. Give the final answer "
                                    "value now, JSON only."})
        r = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=0)
        m = r.choices[0].message
        log.append({"step": "forced_final", "content": m.content,
                    "usage": r.usage.model_dump() if r.usage else None})

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