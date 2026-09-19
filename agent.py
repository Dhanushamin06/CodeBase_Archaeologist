"""Codebase Archaeologist: a LangGraph agent that digs through a GitHub repo
and produces an architecture diagram, a bug review and an onboarding guide."""
import json, os, re, shutil, subprocess, sys, tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"), override=True)  # .env next to agent.py
load_dotenv()  # or a .env in the folder you launch from

MODEL = os.getenv("ARCH_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
BASE_URL = os.getenv("ARCH_BASE_URL", "https://integrate.api.nvidia.com/v1")  # NVIDIA's OpenAI-compatible endpoint
_KEY_VAR = "NVIDIA_API_KEY" if "nvidia.com" in BASE_URL else "OPENAI_API_KEY"
API_KEY = (os.getenv(_KEY_VAR) or "").strip().strip("\"'")
if not API_KEY:
    raise RuntimeError(f"{_KEY_VAR} is not set in the terminal running this app. "
                       f"Run `export {_KEY_VAR}=...` (Windows: `set {_KEY_VAR}=...`) in the same window, then restart.")


def make_llm(thinking, max_tokens):
    extra = {"chat_template_kwargs": {"enable_thinking": thinking}}
    return ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0.6, top_p=0.95,
                      max_tokens=max_tokens, max_retries=4, timeout=180, extra_body=extra)


fast = make_llm(False, 1024)    # explorer loop: reasoning off for speed
llm = make_llm(os.getenv("ARCH_THINKING", "1") == "1", 16384)  # diagram / review / doc; ARCH_THINKING=0 = faster


def clean(text):
    """Drop any <think>...</think> block a reasoning model may leave in the content."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

IGNORE = {".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv", ".next", "target", "vendor", ".idea"}
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".lock", ".woff", ".woff2", ".ttf", ".mp4", ".jar", ".pyc"}
SRC = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php", ".c", ".cpp", ".cs", ".kt"}
PATTERNS = {
    "bare except": r"except\s*:",
    "eval/exec": r"\b(eval|exec)\(",
    "hardcoded secret?": r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"][^'\"]{8,}['\"]",
    "SQL string building?": r"(?i)\b(select|insert|update|delete)\b.*(\+\s*\w|\.format\(|%s|f['\"])",
    "TODO/FIXME": r"\b(TODO|FIXME|HACK)\b",
}


class State(TypedDict, total=False):
    repo_url: str
    path: str
    tree: str
    stats: Dict[str, int]
    hotspots: List[str]
    facts: dict
    files_read: Dict[str, str]
    mermaid: str
    bugs: str
    onboarding: str


def ctx(s, n=3500):
    files = "\n\n".join(f"### {k}\n{v[:n]}" for k, v in s["files_read"].items())
    return f"TREE:\n{s['tree']}\n\nLANGUAGES: {s['stats']}\nGIT HOTSPOTS: {s['hotspots']}\n\nFILES:\n{files}"


def check_url(url):
    url = url.strip()
    if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+?(\.git)?/?$", url):
        raise ValueError("Enter a public URL like https://github.com/owner/repo")
    return url


def clone(s):
    url = check_url(s["repo_url"])
    d = tempfile.mkdtemp(prefix="arch_")
    r = subprocess.run(["git", "clone", "--depth", "300", url, d], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(r.stderr[-300:])
    return {"path": d}


def repo_facts(root, churn):
    """Deterministic 'archaeology' stats: LOC by language, contributors, bus factor, churn."""
    loc = Counter()
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for f in files:
            p = Path(dp) / f
            if p.suffix in SRC and p.stat().st_size < 500_000:
                with open(p, errors="ignore") as fh:
                    loc[p.suffix] += sum(1 for _ in fh)

    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True).stdout

    people = []
    for line in git("shortlog", "-sn", "--no-merges", "HEAD").splitlines():
        if line.strip():
            n, _, name = line.strip().partition("\t")
            people.append((name, int(n)))
    total, acc, bus = sum(n for _, n in people) or 1, 0, 0
    for _, n in people:
        acc, bus = acc + n, bus + 1
        if acc >= total / 2:
            break
    dates = git("log", "--format=%cs").split()
    return {"loc": dict(loc.most_common()), "total_loc": sum(loc.values()), "commits": total,
            "people": people[:8], "n_people": len(people), "bus_factor": bus,
            "first": dates[-1] if dates else "?", "last": dates[0] if dates else "?",
            "churn": churn.most_common(10)}


def scan(s):
    root, lines, stats = Path(s["path"]), [], Counter()
    for dp, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE)
        rel = Path(dp).relative_to(root)
        depth = len(rel.parts)
        for f in files:
            stats[Path(f).suffix or f] += 1
        if depth <= 2 and len(lines) < 350:
            if depth:
                lines.append("  " * (depth - 1) + rel.name + "/")
            lines += ["  " * depth + f for f in sorted(files)[:25]]
    log = subprocess.run(["git", "-C", str(root), "log", "--name-only", "--pretty=format:"],
                         capture_output=True, text=True).stdout
    churn = Counter(l for l in log.splitlines() if l and not any(p in IGNORE for p in Path(l).parts))
    return {"tree": "\n".join(lines), "stats": dict(stats.most_common(8)), "files_read": {}, "facts": repo_facts(root, churn),
            "hotspots": [f"{f} ({n} commits)" for f, n in churn.most_common(10)]}


def read_file(root, rel, limit=6000):
    base = Path(root).resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file() or p.suffix.lower() in BINARY:
        return None
    return p.read_text(errors="ignore")[:limit]


MAX_STEPS = 8


def explore(s):
    """The autonomous part: the LLM decides which file to open next."""
    read, tried = {}, set()
    for _ in range(MAX_STEPS):
        seen = "\n".join(f"- {k}: {v[:300]!r}" for k, v in read.items()) or "(none)"
        prompt = (
            "You are a senior engineer exploring an unfamiliar repo to learn its architecture.\n"
            f"TREE:\n{s['tree']}\nLANGUAGES: {s['stats']}\nMOST-CHANGED FILES: {s['hotspots']}\n"
            f"ALREADY READ:\n{seen}\n\n"
            "Pick the single most informative unread file (README, manifests, entrypoints, routers, core modules, config). "
            'Reply JSON: {"action":"read","path":"relative/path"} or {"action":"done"} if you understand the architecture.'
        )
        try:
            a = json.loads(re.search(r"\{.*\}", clean(fast.invoke(prompt).content), re.S).group())
        except Exception:
            break
        if a.get("action") != "read" or a.get("path") in tried:
            break
        tried.add(a["path"])
        text = read_file(s["path"], a["path"])
        if text:
            read[a["path"]] = text
    return {"files_read": read}


plain = make_llm(False, 4096)  # no reasoning: reliable, fast JSON


def safe_label(t):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s./+-]", " ", str(t))).strip()[:40] or "component"


def build_mermaid(spec):
    """Python (not the LLM) writes the Mermaid syntax, so it is always valid."""
    ids, lines = {}, ["flowchart TD"]
    for i, n in enumerate(spec["nodes"][:14]):
        ids[str(n["id"])] = f"n{i}"
        lines.append(f'    n{i}["{safe_label(n["label"])}"]')
    for e in spec["edges"][:30]:
        x, y = ids.get(str(e["from"])), ids.get(str(e["to"]))
        if x and y:
            lab = safe_label(e["label"]) if e.get("label") else ""
            lines.append(f'    {x} -->|"{lab}"| {y}' if lab else f"    {x} --> {y}")
    return "\n".join(lines)


def fallback_mermaid(s):
    """No LLM needed: repository -> its top-level folders."""
    top = [l.strip("/ ") for l in s["tree"].splitlines() if l.endswith("/") and not l.startswith(" ")][:10]
    lines = ["flowchart TD", '    r["Repository"]']
    for i, d in enumerate(top or ["source"]):
        lines += [f'    d{i}["{safe_label(d)}"]', f"    r --> d{i}"]
    return "\n".join(lines)


def extract_mermaid(text):
    """Pull diagram code out of a reply even if it has chatter or code fences around it."""
    m = re.search(r"```(?:mermaid)?\s*(.*?)```", text, re.S)
    text = m.group(1) if m else text
    m = re.search(r"^\s*(flowchart|graph|sequenceDiagram)\b.*", text, re.S | re.M)
    return (m.group(0) if m else text).strip()


def draw(s):
    prompt = ('Describe this codebase\'s architecture as JSON only, in this shape: '
              '{"nodes":[{"id":"api","label":"REST API"}],"edges":[{"from":"api","to":"db","label":"queries"}]}. '
              "Use 6-14 nodes (components, data stores, external services) and edges for data flow or calls. "
              "Labels under 5 words, using real names from the code.\n\n" + ctx(s))
    for _ in range(2):
        try:
            spec = json.loads(re.search(r"\{.*\}", clean(plain.invoke(prompt).content), re.S).group())
            if spec["nodes"]:
                return {"mermaid": build_mermaid(spec)}
        except Exception:
            pass
    return {"mermaid": fallback_mermaid(s)}


def find_bugs(s):
    root, hits = Path(s["path"]), []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for f in files:
            p = Path(dp) / f
            if p.suffix not in SRC or p.stat().st_size > 200_000:
                continue
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                for name, rx in PATTERNS.items():
                    if re.search(rx, line):
                        hits.append(f"{p.relative_to(root)}:{i} [{name}] {line.strip()[:120]}")
    out = llm.invoke(
        "You are a careful code reviewer. Using ONLY the evidence below (static-scan hits and file contents), list the "
        "5-8 most likely bugs, security risks or fragile spots. For each give: severity (High/Med/Low), file:line, why it "
        "matters, suggested fix. Say 'likely' or 'verify' when unsure; never invent locations.\n\n"
        f"STATIC HITS:\n" + "\n".join(hits[:60]) + "\n\n" + ctx(s)).content
    return {"bugs": clean(out)}


def write_doc(s):
    doc = llm.invoke(
        "Write a 'First-Day Onboarding Guide' in Markdown for a developer joining this project. Sections: "
        "1. What this project does (3 sentences) 2. Tech stack 3. Architecture (write the single line {{DIAGRAM}} then a "
        "short walkthrough) 4. Directory map 5. Run it locally (only steps supported by the files, otherwise say 'verify') "
        "6. Key flows to trace first 7. Where to start: 3 good first tasks 8. Known risks (from the review) "
        "9. Git hotspots: which files change most and why that matters. Be specific, cite real paths, don't invent.\n\n"
        f"REVIEW:\n{s['bugs']}\n\n{ctx(s)}").content
    return {"onboarding": clean(doc).replace("{{DIAGRAM}}", "```mermaid\n" + s["mermaid"] + "\n```")}


def cleanup(s):
    shutil.rmtree(s["path"], ignore_errors=True)
    return {"path": ""}


g = StateGraph(State)
for name, fn in [("clone", clone), ("scan", scan), ("explore", explore), ("draw", draw),
                 ("find_bugs", find_bugs), ("write_doc", write_doc)]:
    g.add_node(name, fn)
g.set_entry_point("clone")
g.add_edge("clone", "scan")
g.add_edge("scan", "explore")
g.add_edge("explore", "draw")        # fan-out: diagram and bug hunt run in parallel
g.add_edge("explore", "find_bugs")
g.add_edge(["draw", "find_bugs"], "write_doc")  # fan-in
g.add_edge("write_doc", END)
graph = g.compile()

if __name__ == "__main__":  # CLI backup for demos: python agent.py <url>
    final = {}
    for upd in graph.stream({"repo_url": sys.argv[1]}, stream_mode="updates"):
        for node, out in upd.items():
            print("✓", node)
            final.update(out)
    Path("ONBOARDING.md").write_text(final["onboarding"])
    print("Wrote ONBOARDING.md")
    cleanup(final)