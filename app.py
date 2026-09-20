import json
import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from agent import cleanup
from features import answer, run, trace_flow

st.set_page_config(page_title="Codebase Archaeologist", page_icon="🏺", layout="wide")
st.title("🏺 Codebase Archaeologist")
st.caption("Paste a public GitHub repo. The agent explores it on its own, then you can interrogate it.")

LABELS = {"clone": "Cloned repository", "scan": "Mapped file tree, git history and stats",
          "explore": "Agent chose and read key files", "draw": "Drew architecture diagram",
          "find_bugs": "Reviewed code for likely bugs", "write_doc": "Wrote onboarding guide",
          "cache": "Loaded cached analysis (repo unchanged since last run)"}


def mermaid(code, height=600):
    """Render once the frame is visible (hidden tabs break layout); retry with safer settings on failure."""
    plain_code = re.sub(r'\|"[^"]*"\|', "", code)  # same diagram without edge labels
    payload = json.dumps([code, plain_code]).replace("</", "<\\/")
    components.html(
        '<div id="out"></div><script type="module">'
        'import m from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";'
        f"const codes = {payload}; const out = document.getElementById('out');"
        "const cfgs = [{}, {flowchart:{curve:'linear', htmlLabels:false}}, {flowchart:{curve:'linear', htmlLabels:false}}];"
        "async function draw(){ let err;"
        " for (let i = 0; i < 3; i++) {"
        "  try { m.initialize({startOnLoad:false, ...cfgs[i]}); const c = codes[i === 2 ? 1 : 0];"
        "   await m.parse(c); const r = await m.render('g' + Date.now(), c); out.innerHTML = r.svg; return; }"
        "  catch (e) { err = e; document.querySelectorAll('[id^=\"dg\"]').forEach(x => x.remove()); } }"
        " out.textContent = 'Diagram error: ' + (err.message || err);"
        " out.style.cssText = 'color:#b00020;font:13px monospace;white-space:pre-wrap'; }"
        "if (document.body.clientWidth > 0) draw();"
        "else new ResizeObserver((_, ob) => { if (document.body.clientWidth > 0) { ob.disconnect(); draw(); } })"
        ".observe(document.body);"
        "</script>", height=height, scrolling=True)


url = st.text_input("GitHub repo URL", "https://github.com/pallets/click")
if st.button("Dig in", type="primary"):
    old = st.session_state.get("res")
    if old and old.get("path"):
        cleanup(old)
    res = {"repo_url": url.strip()}
    st.session_state.update(chat=[], flow=None)
    try:
        with st.status("Excavating...", expanded=True) as status:
            for node, out in run(url):
                st.write(f"✅ {LABELS[node]}")
                res.update(out)
                if node == "explore":
                    st.write("📂 Read: " + ", ".join(f"`{f}`" for f in out["files_read"]))
                    if not out["files_read"]:
                        st.warning("The agent read no files, so the diagram will be generic.")
            status.update(label="Done", state="complete")
        st.session_state["res"] = res
    except Exception as e:
        st.error(str(e))

res = st.session_state.get("res")
if res:
    t1, t2, t3, t4, t5 = st.tabs(["📘 Onboarding", "🗺️ Architecture", "🐞 Likely bugs", "⛏️ Archaeology", "💬 Ask the repo"])
    with t1:
        st.download_button("Download ONBOARDING.md", res["onboarding"], "ONBOARDING.md")
        st.markdown(res["onboarding"].split("```mermaid")[0] + "\n*(diagram in the Architecture tab)*")
    with t2:
        mermaid(res["mermaid"])
        st.download_button("Download diagram (.mmd)", res["mermaid"], "architecture.mmd")
        with st.expander("Diagram source (paste into mermaid.live to debug)"):
            st.code(res["mermaid"])
        st.subheader("Trace a flow")
        flow = st.text_input("Describe a flow", placeholder="e.g. what happens when a user logs in?")
        if st.button("Trace it") and flow:
            try:
                with st.spinner("Reading the relevant files..."):
                    st.session_state["flow"] = trace_flow(res, flow)
            except Exception as e:
                st.error(str(e))
        if st.session_state.get("flow"):
            code, files = st.session_state["flow"]
            mermaid(code, 500)
            st.caption("Based on: " + ", ".join(f"`{f}`" for f in files))
    with t3:
        st.download_button("Download review", res["bugs"], "bug-review.md")
        st.markdown(res["bugs"])
    with t4:
        f = res["facts"]
        c = st.columns(4)
        c[0].metric("Lines of code", f"{f['total_loc']:,}")
        c[1].metric("Commits analysed", f["commits"])
        c[2].metric("Contributors", f["n_people"])
        c[3].metric("Bus factor", f["bus_factor"], help="People behind 50% of recent commits")
        st.caption(f"History window: {f['first']} → {f['last']} (most recent ≤300 commits)")
        a, b = st.columns(2)
        a.subheader("Code by language")
        if f["loc"]:
            a.bar_chart(pd.Series(f["loc"]))
        b.subheader("Churn hotspots")
        if f["churn"]:
            b.bar_chart(pd.DataFrame(f["churn"], columns=["file", "commits"]).set_index("file"))
        st.subheader("Top contributors")
        st.dataframe(pd.DataFrame(f["people"], columns=["name", "commits"]), hide_index=True)
    with t5:
        for m in st.session_state.get("chat", []):
            with st.chat_message(m["role"]):
                st.markdown(m["text"])
        if q := st.chat_input("Ask anything about this codebase"):
            st.session_state["chat"].append({"role": "user", "text": q})
            try:
                with st.spinner("Reading the relevant files..."):
                    a, files = answer(res, q)
                a += ("\n\n*Read: " + ", ".join(f"`{x}`" for x in files) + "*") if files else ""
            except Exception as e:
                a = f"Something went wrong: {e}"
            st.session_state["chat"].append({"role": "assistant", "text": a})
            st.rerun()