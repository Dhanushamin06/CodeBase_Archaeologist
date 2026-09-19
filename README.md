# Codebase Archaeologist
```
pip install -r requirements.txt
export NVIDIA_API_KEY=nvapi-...     # from build.nvidia.com
# to use OpenAI instead: export ARCH_BASE_URL=https://api.openai.com/v1 ARCH_MODEL=gpt-4o OPENAI_API_KEY=sk-...
streamlit run app.py                # UI
python agent.py https://github.com/owner/repo   # CLI, writes ONBOARDING.md
```
Requires `git` on PATH. Public GitHub repos only.

## Features
- Autonomous exploration (LLM picks which files to read), parallel diagram + bug review
- **Archaeology tab**: LOC by language, contributors, bus factor, churn hotspots (no LLM needed)
- **Ask the repo**: chat; the agent picks and reads the relevant files per question
- **Flow tracer**: describe a flow, get a Mermaid sequence diagram
- **Cache**: results stored in `.cache/` per commit SHA, so re-running an unchanged repo is instant
- Downloads: onboarding guide, diagram (.mmd), bug review
