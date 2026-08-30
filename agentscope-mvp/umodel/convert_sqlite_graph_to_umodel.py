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


def convert(db: Path = DEFAULT_DB, out_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    nodes = [dict(r) for r in con.execute("SELECT * FROM app_nodes ORDER BY appid").fetchall()]
    edges = [dict(r) for r in con.execute("SELECT * FROM app_edges ORDER BY src,dst,relation").fetchall()]
    con.close()

    entities = []
    id_map = {}
    for n in nodes:
        eid = entity_id_for_appid(n["appid"])
        id_map[n["appid"]] = eid
        entities.append({
            **runtime_base("entity"),
            "__domain__": DOMAIN,
            "__entity_type__": ENTITY_TYPE,
            "__entity_id__": eid,
            "id": eid,
            "appid": n["appid"],
            "display_name": n.get("name") or n["appid"],
            "name": n.get("name") or n["appid"],
            "kind": n.get("kind"),
            "business_domain": n.get("domain"),
            "description": n.get("description"),
            "status": "active",
        })

    relations = []
    for e in edges:
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
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "entities.json").write_text(json.dumps(entities, ensure_ascii=False, indent=2) + "\n")
    (out_dir / "relations.json").write_text(json.dumps(relations, ensure_ascii=False, indent=2) + "\n")
    (out_dir / "appid_entity_id_map.json").write_text(json.dumps(id_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"db": str(db), "out_dir": str(out_dir), "entities": len(entities), "relations": len(relations)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    print(json.dumps(convert(args.db, args.out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
