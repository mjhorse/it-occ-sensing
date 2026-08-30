#!/usr/bin/env python3
"""Backend simulation agents for the IT OCC demo.

The browser is read-only. These agents are the only runtime writer for time
series/events/warnings. They append to the persisted state file and keep all
pages on the same data source; topology remains UModel-backed.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE = Path(__file__).parent
SIM_STATE_FILE = BASE / "runtime" / "simulation-state-v4.json"
SIM_STATE_BACKUP_DIR = BASE / "runtime" / "simulation-state-history"
RULES_FILE = BASE.parent / "mvp" / "ui-prototype-v4-1" / "rules.html"
CURRENT_SCHEMA = "it_occ_sensing_server_simulation_state.v1"
EVENT_KEYS = ["alerts", "sd", "forum", "changes", "event_tickets"]
SERIES_KEYS = ["error_rate", "cpu_usage", "request_count", "p95_latency_ms"]

SD_TEXTS = [
    "用户反馈页面打开很慢，提交后长时间无响应。",
    "客户报障：保存失败并偶发超时。",
    "一线反馈交易链路响应变慢，影响确认操作。",
    "用户咨询是否存在服务异常，多个请求等待时间过长。",
]
FORUM_TEXTS = [
    "合同生成成功了，但回到报价单页面特别慢，客户在电话里等着确认金额。",
    "今天页面卡顿明显，刷新后还是慢。",
    "操作经常转圈，怀疑系统有问题。",
    "提交后返回慢，影响现场沟通效率。",
]
TICKET_TEXTS = [
    "销售报价链路响应时间持续升高，已创建事件单协调排查。",
    "多源信号集中出现，事件单跟进中。",
    "用户影响扩大，需要联动应用 Owner 排查。",
]


def now_ms() -> int:
    return int(time.time() * 1000)


def load_state() -> dict[str, Any]:
    if not SIM_STATE_FILE.exists():
        raise FileNotFoundError(f"simulation state not found: {SIM_STATE_FILE}")
    return json.loads(SIM_STATE_FILE.read_text(errors="ignore"))


def atomic_write_state(state: dict[str, Any]) -> None:
    SIM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SIM_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(SIM_STATE_FILE)


def backup_state(reason: str) -> None:
    if not SIM_STATE_FILE.exists():
        return
    SIM_STATE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(SIM_STATE_FILE, SIM_STATE_BACKUP_DIR / f"simulation-state-v4.{ts}.{reason}.json")


def app_state(state: dict[str, Any], appid: str) -> dict[str, Any]:
    payload = state["payload"]
    return payload["data"][appid]


def last_event_time(d: dict[str, Any], key: str) -> float:
    rows = d.get("events", {}).get(key) or []
    return max([float(x.get("t") or 0) for x in rows] or [0.0])


def count_events(d: dict[str, Any], key: str, t: float, sec: int, pred=lambda e: True) -> int:
    n = 0
    for e in d.get("events", {}).get(key) or []:
        et = float(e.get("t") or 0)
        if 0 <= t - et <= sec and pred(e):
            n += 1
    return n


def points_in_window(series: list[list[float]], t: float, sec: int) -> list[list[float]]:
    return [p for p in series or [] if t - sec <= float(p[0]) <= t]


def evaluate_warning_rules(d: dict[str, Any], t: float) -> list[dict[str, Any]]:
    app = d.get("app") or {}
    tier = app.get("app_tier") or ""
    appid = app.get("appid") or ""
    domain = app.get("business_domain") or ""
    matched: list[dict[str, Any]] = []
    p95pts = [p for p in points_in_window(d.get("series", {}).get("p95_latency_ms") or [], t, 300) if float(p[1]) >= 800]
    if tier in ("T1", "T2") and appid == "com.sale.quote.center" and len(p95pts) >= 3:
        matched.append({"id": "SRC-METRIC-LATENCY-001", "source": "metrics", "evidence": f"P95≥800ms 点数 {len(p95pts)}/5分钟"})
    def sd_pred(e: dict[str, Any]) -> bool:
        txt = str(e.get("title") or e.get("symptom") or e.get("content") or "")
        return str(e.get("severity")) in ["报障", "故障", "投诉", "体验问题"] or bool(re.search(r"报错|失败|慢|打不开|超时", txt))
    sd_cnt = count_events(d, "sd", t, 600, sd_pred)
    if tier in ("T1", "T2", "T3") and ("销售" in domain or domain == "sales_domain") and sd_cnt >= 3:
        matched.append({"id": "SRC-SD-BURST-001", "source": "sd_tickets", "evidence": f"同应用SD/报障 {sd_cnt}单/10分钟"})
    fatal = count_events(d, "alerts", t, 300, lambda e: str(e.get("severity")) in ["严重", "致命"])
    if fatal >= 1:
        matched.append({"id": "SRC-ALERT-SEVERITY-001", "source": "alerts", "evidence": f"严重/致命告警 {fatal}条/5分钟"})
    return matched


@dataclass
class SimulationDirective:
    mode: str = "steady"  # steady | lower | spike | raise | quiet
    target_metric: str = "p95_latency_ms"
    appid: str | None = None
    duration_sec: int = 300
    intensity: float = 1.0
    event_hint: str = ""
    raw: str = ""
    created_at_sim: float = 0.0


@dataclass
class AgentRuntime:
    running: bool = False
    tick_sec: int = 1
    speed: int = 1
    directives: list[SimulationDirective] = field(default_factory=list)
    last_tick_wall: float = 0.0
    last_saved_at: int = 0
    last_summary: str = "未启动"
    tick_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


RUNTIME = AgentRuntime()


def _randwalk(prev: float, floor: float, ceil: float, drift: float, noise: float) -> float:
    return max(floor, min(ceil, prev + drift + random.uniform(-noise, noise)))


def _directive_for(appid: str, sim_time: float | None = None) -> SimulationDirective | None:
    active: list[SimulationDirective] = []
    for d in RUNTIME.directives:
        if sim_time is None or not d.created_at_sim or sim_time - d.created_at_sim <= d.duration_sec:
            active.append(d)
    if len(active) != len(RUNTIME.directives):
        RUNTIME.directives = active
    for d in reversed(active):
        if d.appid in (None, appid):
            return d
    return None


def append_metric_point(d: dict[str, Any], t: float, directive: SimulationDirective | None) -> None:
    series = d.setdefault("series", {})
    for key in SERIES_KEYS:
        arr = series.setdefault(key, [])
        prev = float(arr[-1][1]) if arr else {"error_rate": 0.005, "cpu_usage": 35, "request_count": 1000, "p95_latency_ms": 180}[key]
        drift = 0.0
        noise = {"error_rate": 0.0008, "cpu_usage": 1.2, "request_count": max(5, prev * 0.025), "p95_latency_ms": 28}.get(key, 1)
        floor, ceil = {"error_rate": (0.0005, 0.3), "cpu_usage": (5, 98), "request_count": (1, 50000), "p95_latency_ms": (30, 6000)}[key]
        if directive and (directive.target_metric == key or directive.target_metric == "all"):
            if directive.mode == "lower":
                drift -= noise * 1.6 * directive.intensity
            elif directive.mode == "raise":
                drift += noise * 1.4 * directive.intensity
            elif directive.mode == "spike":
                drift += noise * 4.0 * directive.intensity
            elif directive.mode == "quiet":
                drift -= noise * 0.8
                noise *= 0.35
        val = _randwalk(prev, floor, ceil, drift, noise)
        if key == "error_rate":
            val = round(val, 6)
        elif key in ("cpu_usage", "p95_latency_ms"):
            val = round(val, 2)
        else:
            val = round(val, 2)
        arr.append([t, val])
        if len(arr) > 7200:
            del arr[: len(arr) - 7200]


def enrich_forum(row: dict[str, Any]) -> None:
    row.setdefault("reply_count", random.randint(3, 32))
    row.setdefault("view_count", random.randint(80, 1200))
    row.setdefault("channel", random.choice(["客户论坛", "服务群", "热线转写"]))
    row.setdefault("semantic_label", random.choice(["体验问题", "疑似故障", "响应慢"]))
    row.setdefault("agent_semantic_interpretation", "用户侧反馈与当前应用性能波动同窗出现，需要纳入融合感知输入。")


def event_producer_agent(d: dict[str, Any], t: float, directive: SimulationDirective | None, state: dict[str, Any], force: bool = False) -> int:
    pools = [
        ("alerts", "alert", ["一般", "严重", "致命"], 0.028, 120),
        ("sd", "sd", ["咨询", "报障", "体验问题"], 0.020, 150),
        ("forum", "forum", ["咨询", "体验问题", "故障"], 0.020, 150),
        ("changes", "change", ["执行中", "已完成", "回滚中"], 0.006, 420),
        ("event_tickets", "event_ticket", ["P1", "P2", "P3"], 0.010, 300),
    ]
    last_support = max(last_event_time(d, k) for k in EVENT_KEYS)
    force_one = force or (t - last_support > 210) or (directive and directive.mode == "spike" and t - last_support > 20)
    emitted = 0
    candidates = [random.choice(pools)] if force_one else pools
    app = d.get("app") or {}
    for key, kind, sevs, prob, min_gap in candidates:
        if not force_one and t - last_event_time(d, key) < min_gap:
            continue
        p = prob * (2.5 if directive and directive.mode in ("spike", "raise") else 1.0)
        if force_one or random.random() < p:
            seq = int(state.get("eventSeq") or 900000) + 1
            state["eventSeq"] = seq
            severity = random.choice(sevs)
            if directive and directive.event_hint:
                base = directive.event_hint[:80]
            elif key == "sd":
                base = random.choice(SD_TEXTS)
            elif key == "forum":
                base = random.choice(FORUM_TEXTS)
            elif key == "event_tickets":
                base = random.choice(TICKET_TEXTS)
            elif key == "changes":
                base = f"{app.get('app_name') or app.get('appid')} 发布参数变更"
            else:
                base = f"{app.get('app_name') or app.get('appid')} 告警：接口连续超时"
            row = {"t": t, "kind": kind, "severity": severity, "id": f"SIM-{kind.upper()}-{seq}", "title": base, "generated_by": "EventProducerAgent"}
            if key == "alerts":
                row["status"] = "处理中"
            if key == "sd":
                row["symptom"] = base
            if key == "forum":
                row["content"] = base
                enrich_forum(row)
            if key == "event_tickets":
                row["description"] = base
            d.setdefault("events", {}).setdefault(key, []).append(row)
            emitted += 1
            break
    return emitted


def should_append_warning(d: dict[str, Any], t: float, matched: list[dict[str, Any]]) -> bool:
    gap = t - last_event_time(d, "warnings")
    if gap < 360 or len(matched) < 2:
        return False
    if len({m["source"] for m in matched}) < 2:
        return False
    return random.random() < (0.04 if gap < 900 else 0.12 if gap < 1800 else 0.28)


def warning_fusion_agent(d: dict[str, Any], t: float, state: dict[str, Any]) -> int:
    matched = evaluate_warning_rules(d, t)
    if not should_append_warning(d, t, matched):
        return 0
    seq = int(state.get("eventSeq") or 900000) + 1
    state["eventSeq"] = seq
    severity = "致命" if any(m["source"] == "alerts" for m in matched) and any(m["source"] == "metrics" for m in matched) else "严重"
    d.setdefault("events", {}).setdefault("warnings", []).append({
        "t": t,
        "kind": "warning",
        "severity": severity,
        "id": f"WARN-SIM-{seq}",
        "title": "、".join(m["id"] for m in matched) + " 命中，生成融合预警对象",
        "generated_by": "WarningFusionAgent",
        "matched_rules": matched,
        "trigger_summary": "；".join(m["evidence"] for m in matched),
    })
    return 1


def choose_appids(state: dict[str, Any]) -> list[str]:
    payload = state["payload"]
    selected = [state.get("appid") or payload.get("appids", [None])[0]]
    # Keep runtime lightweight: selected app plus a few core/high-signal apps.
    for appid in payload.get("appids", [])[:8]:
        if appid not in selected:
            selected.append(appid)
    return [a for a in selected if a in payload.get("data", {})]


def tick_once(state: dict[str, Any], seconds: int | None = None) -> dict[str, Any]:
    payload = state["payload"]
    step = int(seconds or RUNTIME.speed or 1)
    # Keep demo simulation time aligned with local wall-clock time.
    # Never move backwards; if the persisted snapshot lags behind real time,
    # catch up on the next backend tick instead of drifting from an old seed.
    wall_now = time.time()
    prev_time = float(payload.get("maxTime") or wall_now)
    t = max(prev_time + step, wall_now)
    metric_points = events = warnings = 0
    for appid in choose_appids(state):
        d = app_state(state, appid)
        directive = _directive_for(appid, t)
        append_metric_point(d, t, directive)
        metric_points += len(SERIES_KEYS)
        events += event_producer_agent(d, t, directive, state)
        warnings += warning_fusion_agent(d, t, state)
    payload["maxTime"] = t
    payload["minTime"] = max(float(payload.get("minTime") or t), t - 24 * 3600)
    state.setdefault("schema_version", CURRENT_SCHEMA)
    state["generated_by"] = "BackendSimulationAgents"
    state["server_saved_at"] = now_ms()
    state["agent_runtime"] = {
        "simulation_agent": "running" if RUNTIME.running else "manual_tick",
        "warning_agent": "running" if RUNTIME.running else "manual_tick",
        "topology_agent": "umodel-managed",
        "last_tick_time": t,
        "last_directive": RUNTIME.directives[-1].__dict__ if RUNTIME.directives else None,
    }
    RUNTIME.tick_count += 1
    RUNTIME.last_saved_at = state["server_saved_at"]
    RUNTIME.last_summary = f"推进 {step}s，写入指标点 {metric_points}，event {events}，预警 {warnings}"
    return {"t": t, "metric_points": metric_points, "events": events, "warnings": warnings}


async def agent_loop() -> None:
    while True:
        await asyncio.sleep(max(0.5, RUNTIME.tick_sec))
        if not RUNTIME.running:
            continue
        async with RUNTIME.lock:
            try:
                state = load_state()
                tick_once(state)
                atomic_write_state(state)
            except Exception as exc:
                RUNTIME.last_summary = f"agent loop error: {type(exc).__name__}: {exc}"


async def llm_parse_directive(message: str, appids: list[str]) -> SimulationDirective | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-6"
    prompt = {
        "task": "Parse user's instruction into JSON for an IT OCC simulation data agent. Return JSON only.",
        "schema": {"mode": "steady|lower|raise|spike|quiet", "target_metric": "p95_latency_ms|error_rate|request_count|cpu_usage|all", "appid": "one appid or null", "duration_sec": "integer", "intensity": "0.2-3.0", "event_hint": "short zh-CN text"},
        "known_appids_sample": appids[:30],
        "user_message": message,
    }
    payload = {"model": model, "max_tokens": 300, "temperature": 0.0, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]}
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": os.environ["ANTHROPIC_API_KEY"]}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url}/v1/messages", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        obj = json.loads(re.search(r"\{.*\}", text, flags=re.S).group(0) if "{" in text else text)
        return SimulationDirective(
            mode=str(obj.get("mode") or "steady"),
            target_metric=str(obj.get("target_metric") or "p95_latency_ms"),
            appid=obj.get("appid") if obj.get("appid") in appids else None,
            duration_sec=max(10, min(3600, int(obj.get("duration_sec") or 300))),
            intensity=max(0.2, min(3.0, float(obj.get("intensity") or 1.0))),
            event_hint=str(obj.get("event_hint") or "")[:120],
            raw=message,
        )
    except Exception:
        return None


def rule_parse_directive(message: str, appids: list[str]) -> SimulationDirective:
    m = message.lower()
    mode = "steady"
    if any(w in message for w in ["降", "降低", "降下去", "恢复", "回落"]):
        mode = "lower"
    if any(w in message for w in ["升", "升高", "增加", "拉高"]):
        mode = "raise"
    if any(w in message for w in ["突增", "尖峰", "短期", "暴涨", "激增", "spike"]):
        mode = "spike"
    if any(w in message for w in ["平稳", "安静", "减少事件", "不要预警"]):
        mode = "quiet"
    metric = "p95_latency_ms"
    if any(w in message for w in ["错误率", "error"]):
        metric = "error_rate"
    elif any(w in message for w in ["请求", "访问", "流量", "qps"]):
        metric = "request_count"
    elif any(w in message for w in ["cpu", "CPU"]):
        metric = "cpu_usage"
    elif any(w in message for w in ["全部", "整体", "所有"]):
        metric = "all"
    appid = next((a for a in appids if a in message), None)
    return SimulationDirective(mode=mode, target_metric=metric, appid=appid, duration_sec=300, intensity=1.2 if mode == "spike" else 1.0, event_hint=message[:100], raw=message)


async def apply_user_directive(message: str, default_appid: str | None = None) -> dict[str, Any]:
    state = load_state()
    appids = state.get("payload", {}).get("appids", [])
    # Keep the control loop interactive: apply a deterministic directive immediately.
    # A future enhancement can run llm_parse_directive asynchronously to refine this,
    # but the UI must not wait on model latency before the backend generator reacts.
    directive = rule_parse_directive(message, appids)
    if directive.appid is None and default_appid in appids:
        # UI commands are normally issued from the currently selected app context.
        directive.appid = default_appid
    directive.created_at_sim = float(state.get("payload", {}).get("maxTime") or 0)
    RUNTIME.directives.append(directive)
    if len(RUNTIME.directives) > 20:
        RUNTIME.directives = RUNTIME.directives[-20:]
    return {"ok": True, "directive": directive.__dict__, "message": f"已接收：{directive.mode} {directive.target_metric}，作用 appid={directive.appid or '当前运行集合'}"}


def runtime_status() -> dict[str, Any]:
    try:
        state = load_state()
        payload = state.get("payload") or {}
        max_time = payload.get("maxTime")
        counts = {k: 0 for k in ["warnings", *EVENT_KEYS]}
        for item in (payload.get("data") or {}).values():
            for k in counts:
                counts[k] += len((item.get("events") or {}).get(k) or [])
    except Exception as exc:
        max_time = None
        counts = {}
        err = str(exc)
    else:
        err = None
    return {
        "ok": err is None,
        "running": RUNTIME.running,
        "tick_sec": RUNTIME.tick_sec,
        "speed": RUNTIME.speed,
        "tick_count": RUNTIME.tick_count,
        "last_saved_at": RUNTIME.last_saved_at,
        "last_summary": RUNTIME.last_summary,
        "maxTime": max_time,
        "counts": counts,
        "directives": [d.__dict__ for d in RUNTIME.directives[-5:]],
        "agents": {
            "SimulationDataAgent": "writes time-series/support events append-only",
            "WarningCalculationAgent": "reads rules and writes warnings per appid",
            "TopologyManagementAgent": "UModel topology source of truth",
        },
        "error": err,
    }
