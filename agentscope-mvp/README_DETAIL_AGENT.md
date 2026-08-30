# Detail Agent Unified Demo

Use the unified same-origin server to avoid browser `Load failed` caused by calling `127.0.0.1:8776` from a different browser/device origin.

```bash
cd artifacts/it-occ-sensing-agent/agentscope-mvp
./start_unified_demo.sh
```

Open:

```text
http://127.0.0.1:8780/index.html
```

The same server serves:

- `GET /index.html` — UI prototype
- `GET /health` — model/service status
- `POST /agent/detail-analysis/stream` — NDJSON Agent streaming endpoint

## Model config

The server reads Claude Code-style preference from local settings conceptually:

- model alias: `opusplan`
- default Anthropic model env fallback: `claude-opus-4-1-20250805`

Actual Anthropic streaming refine requires `ANTHROPIC_API_KEY` in the server environment. If absent, `LLMRefineAgent` returns a structured fallback status and keeps deterministic AgentScope output. The server does not read or expose Claude Code credential files.
