"""Extra features: disk cache, chat with the repo, and flow tracing."""
import json, re
from pathlib import Path

from agent import MODEL, check_url, clean, clone, fast, graph, make_llm, read_file, subprocess

CACHE = Path(".cache")
mid = make_llm(False, 4096)  # non-reasoning model for quick grounded answers


def remote_sha(url):
    r = subprocess.run(["git", "ls-remote", url, "HEAD"], capture_output=True, text=True, timeout=30)
    return r.stdout.split()[0] if r.stdout.strip() else None


def run(url):
    """Yield (node, update) pairs. Serves from a disk cache while the repo's HEAD is unchanged."""
    url = check_url(url)
    sha = remote_sha(url)
    f = CACHE / f"{sha}-{MODEL.replace('/', '_')}.json" if sha else None
    if f and f.exists():
        yield "cache", json.loads(f.read_text())
        return
    final = {}
    for upd in graph.stream({"repo_url": url}, stream_mode="updates"):
        for node, out in upd.items():
            final.update(out)
            yield node, out
    if f:
        CACHE.mkdir(exist_ok=True)
        f.write_text(json.dumps({k: v for k, v in final.items() if k != "path"}))


def grounded(res, task, instruction):
    """Agent picks the files relevant to `task`, reads them, then answers per `instruction`."""
    path = res.get("path")
    if not path or not Path(path).exists():  # e.g. results came from cache: re-clone on demand
        path = res["path"] = clone({"repo_url": res["repo_url"]})["path"]
    try:
        pick = clean(fast.invoke(
            f'File tree:\n{res["tree"]}\n\nTask: {task}\n'
            'Reply JSON only: {"files": ["up to 4 most relevant relative paths"]}').content)
        files = json.loads(re.search(r"\{.*\}", pick, re.S).group())["files"][:4]
    except Exception:
        files = []
    read = {f: t for f in files if isinstance(f, str) and (t := read_file(path, f, 5000))}
    src = "\n\n".join(f"### {k}\n{v}" for k, v in read.items())
    out = mid.invoke(f"{instruction}\n\nTASK: {task}\n\nPROJECT SUMMARY:\n{res['onboarding'][:3000]}\n\nSOURCE:\n{src}")
    return clean(out.content), list(read)


def answer(res, question):
    return grounded(res, question, "Answer a new developer's question about this codebase. Be concise, cite file "
                    "paths, and say so plainly if the source doesn't show the answer.")


def trace_flow(res, flow):
    code, files = grounded(
        res, f"Trace this flow end to end: {flow}",
        "Return ONLY a Mermaid sequenceDiagram (max 10 participants) of this flow, based only on the source and summary. "
        'Declare participants like: participant A as "Label". Ids alphanumeric; no parentheses or special characters in messages.')
    return re.sub(r"^```(?:mermaid)?\s*|```\s*$", "", code).strip(), files
