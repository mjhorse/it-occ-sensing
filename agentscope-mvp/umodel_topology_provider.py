#!/usr/bin/env python3
"""UModel-backed topology provider for IT OCC AgentScope MVP.

The provider preserves the existing graph_analysis.py output contract while
reading topology facts from UModel Query Service. UModel is the single topology
source of truth for this MVP: callers must fail fast when UModel is unavailable
or an appid is missing. Do not add SQLite/dynamic fallback here.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List

from graph_analysis import relation_label

DEFAULT_ADDR = os.environ.get("UMODEL_ADDR", "http://localhost:8080")
DEFAULT_WORKSPACE = os.environ.get("UMODEL_WORKSPACE", "itocc-demo")
DOMAIN = os.environ.get("UMODEL_ITOCC_DOMAIN", "itocc")
ENTITY_TYPE = os.environ.get("UMODEL_ITOCC_ENTITY_TYPE", "itocc.app")


def entity_id_for_appid(appid: str) -> str:
    return hashlib.md5(appid.encode("utf-8"), usedforsecurity=False).hexdigest()


class UModelTopologyProvider:
    def __init__(self, addr: str = DEFAULT_ADDR, workspace: str = DEFAULT_WORKSPACE, timeout: float = 5.0):
        self.addr = addr.rstrip("/")
        self.workspace = workspace
        self.timeout = timeout

    def _execute(self, query: str) -> List[Dict[str, Any]]:
        payload = json.dumps({"query": query}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.addr}/api/v1/query/{self.workspace}/execute",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"UModel query failed: {e}") from e
        if not body.get("success", True):
            raise RuntimeError(f"UModel query failed: {body}")
        data = (body.get("data") or {})
        header = data.get("header") or []
        rows = data.get("data") or []
        return [dict(zip(header, row)) for row in rows]

    def _node_to_focus(self, node: Dict[str, Any], appid: str, app_name: str | None = None) -> Dict[str, Any]:
        return {
            "appid": node.get("appid") or appid,
            "name": app_name or node.get("display_name") or node.get("name") or appid,
            "kind": node.get("kind") or "service",
            "domain": node.get("business_domain") or node.get("__domain__") or DOMAIN,
            "description": node.get("description") or "来自 UModel 的 IT OCC 应用实体",
            "entity_id": node.get("__entity_id__"),
        }

    def _edge_row(self, src: Dict[str, Any], rel: Dict[str, Any], dest: Dict[str, Any], depth: int = 1) -> Dict[str, Any]:
        relation = rel.get("__relation_type__") or rel.get("relation_type") or rel.get("type") or "call"
        label = relation_label(relation)
        src_appid = src.get("appid") or rel.get("src_appid") or src.get("__entity_id__")
        dst_appid = dest.get("appid") or rel.get("dst_appid") or dest.get("__entity_id__")
        return {
            "src": src_appid,
            "dst": dst_appid,
            "relation": label,
            "relation_label": label,
            "relation_code": relation,
            "weight": rel.get("weight") if rel.get("weight") is not None else 0.5,
            "description": rel.get("description") or rel.get("display_name") or "UModel 拓扑关系",
            "depth": depth,
            "path": f"{src_appid}->{dst_appid}",
            "src_name": src.get("display_name") or src.get("name") or src_appid,
            "dst_name": dest.get("display_name") or dest.get("name") or dst_appid,
            "src_kind": src.get("kind"),
            "dst_kind": dest.get("kind"),
            "src_entity_id": src.get("__entity_id__"),
            "dst_entity_id": dest.get("__entity_id__"),
        }

    def dependency_neighborhood(self, appid: str, max_depth: int = 2, app_name: str | None = None) -> Dict[str, Any]:
        entity_id = entity_id_for_appid(appid)
        label = f"{DOMAIN}@{ENTITY_TYPE}"
        # Use controlled Cypher so edge direction is explicit:
        # inbound = caller/dependent src -> current warning appid dest.
        inbound_query = (
            ".topo | graph-call cypher(`"
            f"MATCH (src)-[r]->(dest:``{label}`` {{__entity_id__: '{entity_id}'}}) "
            "RETURN properties(src) AS src, properties(r) AS relation, properties(dest) AS dest LIMIT 100"
            "`)"
        )
        downstream_query = (
            ".topo | graph-call cypher(`"
            f"MATCH (src:``{label}`` {{__entity_id__: '{entity_id}'}})-[r]->(dest) "
            "RETURN properties(src) AS src, properties(r) AS relation, properties(dest) AS dest LIMIT 100"
            "`)"
        )
        focus_query = (
            f".entity with(domain='{DOMAIN}', name='{ENTITY_TYPE}', ids=['{entity_id}']) "
            "| project __entity_id__,appid,display_name,name,kind,business_domain,description | limit 1"
        )

        focus_rows = self._execute(focus_query)
        if not focus_rows:
            raise KeyError(f"appid {appid} not found in UModel workspace {self.workspace}")
        focus = self._node_to_focus(focus_rows[0], appid, app_name)
        inbound_rows = self._execute(inbound_query)
        downstream_rows = self._execute(downstream_query)
        inbound = [self._edge_row(r["src"], r["relation"], r["dest"]) for r in inbound_rows]
        downstream = [self._edge_row(r["src"], r["relation"], r["dest"]) for r in downstream_rows]
        return {
            "focus_node": focus,
            "downstream_paths": downstream,
            "upstream_edges": inbound,
            "inbound_paths": inbound,
            "graph_view": "inbound_callers_centered_on_warning_appid",
            "graph_store": f"umodel:{self.addr}/workspace/{self.workspace}",
        }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("appid", nargs="?", default="com.sale.quote.center")
    ap.add_argument("--addr", default=DEFAULT_ADDR)
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    args = ap.parse_args()
    provider = UModelTopologyProvider(args.addr, args.workspace)
    print(json.dumps(provider.dependency_neighborhood(args.appid), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
