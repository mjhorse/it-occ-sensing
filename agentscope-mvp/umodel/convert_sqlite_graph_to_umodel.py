#!/usr/bin/env python3
"""Convert the IT OCC SQLite demo graph into UModel runtime payloads.

This keeps the current demo graph as a seed dataset, but emits UModel-compatible
entities/relations so topology facts can be queried through UModel .topo.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE / "graph" / "it_occ_app_graph.sqlite"
DEFAULT_OUT = BASE / "umodel" / "sample-data"
DEFAULT_SIM_STATE = BASE / "runtime" / "simulation-state-v4.json"
DEFAULT_VISIBLE_APPS = BASE.parent / "mvp" / "runtime" / "generated" / "sample-200" / "visible" / "apps.jsonl"
DEFAULT_VISIBLE_DEPS = BASE.parent / "mvp" / "runtime" / "generated" / "sample-200" / "visible" / "app_dependencies.jsonl"
OBSERVED_FIRST = 1704067200
OBSERVED_LAST = 4102444800
KEEP_ALIVE = 3600
DOMAIN = "itocc"
ENTITY_TYPE = "itocc.app"


def entity_id_for_appid(appid: str) -> str:
    """Return a deterministic 128-bit lowercase hex entity id for an appid."""
    return hashlib.md5(appid.encode("utf-8"), usedforsecurity=False).hexdigest()


def runtime_base(category: str) -> dict[str, Any]:
    return {
        "__category__": category,
        "__method__": "Update",
        "__first_observed_time__": OBSERVED_FIRST,
        "__last_observed_time__": OBSERVED_LAST,
        "__keep_alive_seconds__": KEEP_ALIVE,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def relation_type(dep: dict[str, Any]) -> str:
    mapping = {
        "sync_api": "api_call",
        "async_mq": "mq_publish",
        "cache": "cache_read",
        "db": "db_read_write",
        "external": "gateway_route",
    }
    return mapping.get(str(dep.get("dependency_type") or ""), str(dep.get("dependency_type") or "call"))


def load_dataset_catalog() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load the exact service catalog used by the current UI runtime.

    Priority is the persisted simulation state (`payload.data`), because the
    search/app selector and timeline render from that state. The generated
    visible app file is only a cold-start seed when no runtime state exists.
    UModel must not contain additional services absent from the current UI
    catalog.
    """
    apps: dict[str, dict[str, Any]] = {}
    if DEFAULT_SIM_STATE.exists():
        state = json.loads(DEFAULT_SIM_STATE.read_text(errors="ignore"))
        runtime_data = (state.get("payload") or {}).get("data") or {}
        for appid, item in runtime_data.items():
            app = (item or {}).get("app") or {}
            if appid and app:
                apps[appid] = {**app, "appid": appid}
    if not apps:
        for row in read_jsonl(DEFAULT_VISIBLE_APPS):
            if row.get("appid"):
                apps[row["appid"]] = row
    deps = read_jsonl(DEFAULT_VISIBLE_DEPS)
    return apps, deps


def convert(db: Path = DEFAULT_DB, out_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    sqlite_nodes = [dict(r) for r in con.execute("SELECT * FROM app_nodes ORDER BY appid").fetchall()]
    sqlite_edges = [dict(r) for r in con.execute("SELECT * FROM app_edges ORDER BY src,dst,relation").fetchall()]
    con.close()

    dataset_apps, dataset_deps = load_dataset_catalog()
    # Hard consistency rule: UModel entities must be exactly the same service
    # catalog exposed by the frontend search/app selector. Do not retain old
    # SQLite seed nodes or dependency-only endpoints that are absent from the
    # current persisted simulation state. Otherwise topology can show services
    # that the UI cannot search/select (for example stale quote.engine).
    allowed_appids = set(dataset_apps)
    node_by_appid: dict[str, dict[str, Any]] = {
        appid: {**app, "appid": appid, "source": "simulation_dataset_catalog"}
        for appid, app in dataset_apps.items()
    }

    edge_rows: list[dict[str, Any]] = []
    dropped_sqlite_edges = 0
    for e in sqlite_edges:
        if e["src"] not in allowed_appids or e["dst"] not in allowed_appids:
            dropped_sqlite_edges += 1
            continue
        edge_rows.append({
            "src": e["src"],
            "dst": e["dst"],
            "relation": e["relation"],
            "weight": e.get("weight"),
            "description": e.get("description"),
            "source": "sqlite_seed_graph_filtered_to_simulation_catalog",
        })
    dropped_dataset_deps = 0
    for dep in dataset_deps:
        src = dep.get("source_appid")
        dst = dep.get("target_appid")
        if not src or not dst:
            continue
        if src not in allowed_appids or dst not in allowed_appids:
            dropped_dataset_deps += 1
            continue
        edge_rows.append({
            "src": src,
            "dst": dst,
            "relation": relation_type(dep),
            "weight": dep.get("fanout_weight"),
            "description": f"{dep.get('dependency_type') or 'call'} / {dep.get('protocol') or '-'} / {dep.get('criticality') or '-'}",
            "source": "simulation_dataset_dependencies",
            "dependency_id": dep.get("dependency_id"),
        })

    entities = []
    id_map = {}
    for appid in sorted(node_by_appid):
        n = node_by_appid[appid]
        eid = entity_id_for_appid(appid)
        id_map[appid] = eid
        entities.append({
            **runtime_base("entity"),
            "__domain__": DOMAIN,
            "__entity_type__": ENTITY_TYPE,
            "__entity_id__": eid,
            "id": eid,
            "appid": appid,
            "display_name": n.get("app_name") or n.get("name") or appid,
            "name": n.get("app_name") or n.get("name") or appid,
            "kind": n.get("service_type") or n.get("kind") or "service",
            "business_domain": n.get("business_domain") or n.get("domain") or "IT OCC",
            "system_domain": n.get("system_domain"),
            "owner_team": n.get("owner_team"),
            "description": n.get("description") or "来自 IT OCC 持久化模拟数据集的应用实体",
            "status": "active",
            "source_dataset": n.get("source"),
        })

    seen_edges = set()
    relations = []
    for e in edge_rows:
        if e["src"] not in id_map or e["dst"] not in id_map:
            continue
        key = (e["src"], e["dst"], e["relation"], e.get("dependency_id") or e.get("description"))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        src_id = id_map[e["src"]]
        dst_id = id_map[e["dst"]]
        relations.append({
            **runtime_base("entity_link"),
            "__src_domain__": DOMAIN,
            "__src_entity_type__": ENTITY_TYPE,
            "__src_entity_id__": src_id,
            "__dest_domain__": DOMAIN,
            "__dest_entity_type__": ENTITY_TYPE,
            "__dest_entity_id__": dst_id,
            "__relation_type__": e["relation"],
            "display_name": e.get("description") or f"{e['src']} {e['relation']} {e['dst']}",
            "src_appid": e["src"],
            "dst_appid": e["dst"],
            "weight": e.get("weight"),
            "description": e.get("description"),
            "source_dataset": e.get("source"),
            "dependency_id": e.get("dependency_id"),
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "entities.json").write_text(json.dumps(entities, ensure_ascii=False, indent=2) + "\n")
    (out_dir / "relations.json").write_text(json.dumps(relations, ensure_ascii=False, indent=2) + "\n")
    (out_dir / "appid_entity_id_map.json").write_text(json.dumps(id_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "db": str(db),
        "out_dir": str(out_dir),
        "entities": len(entities),
        "relations": len(relations),
        "simulation_dataset_apps": len(dataset_apps),
        "simulation_dataset_dependencies": len(dataset_deps),
        "dropped_sqlite_edges_outside_catalog": dropped_sqlite_edges,
        "dropped_dataset_dependencies_outside_catalog": dropped_dataset_deps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    print(json.dumps(convert(args.db, args.out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
