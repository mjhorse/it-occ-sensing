#!/usr/bin/env python3
import os
for k in ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CLAUDE_MODEL"]:
    v = os.environ.get(k, "")
    print(f"{k}: present={bool(v)} len={len(v)}")
