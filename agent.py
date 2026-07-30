import os, io, json, base64, subprocess, tempfile, textwrap, pathlib
import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ.get("LLM_BASE_URL", "https://aipipe.org/openai/v1"),
)
MODEL = os.environ.get("LLM_MODEL", "gpt-4.1-mini")

GH_TOKEN = os.environ["GH_TOKEN"]
TAVILY_KEY = os.environ.get("TAVILY_KEY", "")
REPO = "abhilash404/tele-bot"

MAX_TOOL_CHARS = 6000
MAX_STEPS = 8

WORKDIR = pathlib.Path("/tmp/work")
WORKDIR.mkdir(parents=True, exist_ok=True)

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
    body = r.content
    name = (url.split("/")[-1].split("?")[0] or "file")[:80]
    saved = WORKDIR / name
    try:
        saved.write_bytes(body)
    except Exception:
        saved = None

    # --- PDF ---
    if body[:4] == b"%PDF" or "pdf" in ctype:
        try:
            reader = PdfReader(io.BytesIO(body))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
            out = (f"[PDF saved to {saved} - {len(reader.pages)} pages. "
                   f"For full text use run_python with pypdf on that path.]\n\n"
                   f"{text[:8000]}")
        except Exception as e:
            out = f"[PDF saved to {saved} but extraction failed: {e}]"
        _seen[url] = out
        return out

    # --- binary spreadsheets ---
    if any(k in ctype for k in ("excel", "spreadsheet", "officedocument")) \
            or name.lower().endswith((".xls", ".xlsx")):
        out = (f"[Excel file saved to {saved}. Use run_python with "
               f"pandas.read_excel('{saved}', sheet_name=None) to inspect.]")
        _seen[url] = out
        return out

    # --- text-ish data files ---
    if "html" not in ctype:
        try:
            preview = body.decode("utf-8", errors="replace")[:8000]
        except Exception:
            preview = "(undecodable binary)"
        out = f"[Saved to {saved}]\n\n{preview}"
        _seen[url] = out
        return out

    # --- HTML ---
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = " ".join(soup.get_text(" ").split())

    if len(text) < 300:
        links = [a.get("href") for a in soup.find_all("a", href=True)][:30]
        out = ("EMPTY_PAGE: JavaScript-rendered page with no static content. "
               "Fetching more paths on this domain will NOT work. Use "
               "web_search to find a direct data file (.csv/.xlsx/.pdf) or "
               f"another source. Links found: {links}")
    else:
        out = text[:8000]

    _seen[url] = out
    return out


def web_search(query: str) -> str:
    if TAVILY_KEY:
        try:
            r = httpx.post("https://api.tavily.com/search", timeout=30, json={
                "api_key": TAVILY_KEY, "query": query,
                "max_results": 8, "search_depth": "basic"})
            rows = [f"{x.get('title','')} | {x.get('url','')} | "
                    f"{(x.get('content') or '')[:300]}"
                    for x in r.json().get("results", [])]
            if rows:
                return "\n".join(rows)
        except Exception as e:
            return f"ERROR (tavily): {e}"

    try:
        r = httpx.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                       timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "lxml")
        rows = [f"{a.get_text(strip=True)} | {a.get('href')}"
                for a in soup.select("a.result-link")[:8]]
        return "\n".join(rows) or "No results."
    except Exception as e:
        return f"ERROR: {e}"


def run_python(code: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     dir=str(WORKDIR)) as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run(["python", path], capture_output=True, text=True,
                           timeout=60, cwd=str(WORKDIR))
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
        "description": ("HTTP GET a URL. HTML is stripped to text, PDFs are "
                        "text-extracted, and every file is saved to /tmp/work/ "
                        "for use with run_python."),
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": ("Run Python 3 in /tmp/work. pandas, numpy, pypdf, "
                        "openpyxl, httpx, requests, bs4, lxml available. "
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

    ENVIRONMENT:
    - Every file you fetch is saved to /tmp/work/<filename>. Use that exact
      path in run_python. NEVER invent paths like /mnt/data/.
    - run_python executes with /tmp/work as the working directory.
    - Installed: pandas, numpy, pypdf, openpyxl, httpx, requests, bs4, lxml.
    - NOT installed: fitz/PyMuPDF, PyPDF2, tabula, camelot. Use pypdf.
    - Extract PDF text like this:
        from pypdf import PdfReader
        text = "\\n".join(p.extract_text() or "" for p in PdfReader(path).pages)

    METHOD:
    - Start from the search results provided to you. Use web_search again
      if they are not useful.
    - Prefer direct .csv / .xlsx / .pdf files over landing pages.
    - Many Indian government sites (mospi.gov.in, niti.gov.in) are
      JavaScript SPAs returning an empty shell. If fetch_url returns
      EMPTY_PAGE, do NOT try more paths on that domain.
    - Never fetch the same URL twice. If it failed, change source.
    - Use run_python for ALL computation. Never do arithmetic mentally.
    - If the question embeds the data inline, skip searching and go
      straight to run_python.
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

    if hits.startswith("ERROR"):
        messages.append({"role": "user", "content":
            "NOTE: web search is unavailable. Do not call web_search. "
            "Go directly to fetch_url on likely data URLs, or answer from "
            "your own knowledge if no data is reachable."})

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

    while (isinstance(answer, dict) and "answer" in answer
           and set(answer.keys()) <= {"answer", "log_url"}):
        answer = answer["answer"]

    log.append({"step": "final", "answer": answer})
    try:
        commit_log(run_id, log)
    except Exception as e:
        log.append({"commit_error": str(e)})
    return answer