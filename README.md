# IT OCC Sensing Agent Entry Page v4.1 Snapshot

This repository is a preserved, standalone snapshot of the working entry page and its supporting demo/API system code.

## Entry page

- HTML entry: `mvp/ui-prototype-v4-1/index.html`
- Rules page: `mvp/ui-prototype-v4-1/rules.html`
- Unified server: `agentscope-mvp/run_demo_server.py`

`run_demo_server.py` serves:

- `/` and `/index.html` → `mvp/ui-prototype-v4-1/index.html`
- `/rules.html` → `mvp/ui-prototype-v4-1/rules.html`
- `/static/*` → UI directory static files
- `/health` → backend health check
- `/agent/detail-analysis/stream` → streaming detail analysis API

## Run locally

```bash
cd agentscope-mvp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run_demo_server.py
```

Then open:

```text
http://127.0.0.1:8780/index.html
```

If port `8780` is already occupied, stop the existing process or edit the port in `agentscope-mvp/run_demo_server.py` for local testing.

## Verification performed for this snapshot

A clean copy should be verified by:

1. installing dependencies from `agentscope-mvp/requirements.txt`;
2. starting `python run_demo_server.py` from `agentscope-mvp`;
3. checking `/health` returns HTTP 200;
4. checking `/index.html` returns HTML containing the UI shell;
5. checking `/rules.html` returns HTTP 200;
6. posting a sample request to `/agent/detail-analysis/stream` and confirming a streamed completion event is returned.

## Security note

Local `.env.local`, virtual environments, Python caches, runtime logs/PIDs, and temporary request files are intentionally excluded. `requirements.txt` is kept minimal for standalone reproducibility; `agentscope-mvp/requirements-full-optional.txt` preserves the optional full AgentScope dependency path.
