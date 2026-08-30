#!/usr/bin/env python3
"""Build local embedded graph DB for IT OCC app dependency analysis.

Uses SQLite as a zero-install embedded graph store:
- app_nodes: appid/service nodes
- app_edges: directed dependency edges from source -> target
- historical_metrics: same-time-window historical baseline samples
"""
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "graph" / "it_occ_app_graph.sqlite"

NODES = [
    ("com.sale.quote.center", "报价中心", "frontend_app", "销售域", "报价页面/合同回跳入口"),
    ("com.sale.quote.core", "报价核心服务", "service", "销售域", "报价单保存、金额计算"),
    ("cache.quote.rule", "报价规则缓存", "cache", "平台域", "报价规则缓存命中"),
    ("com.sale.contract.center", "合同中心", "service", "销售域", "合同生成与回跳"),
    ("db.quote.mysql", "报价库", "database", "数据域", "报价单持久化"),
    ("mq.sale.event", "销售事件MQ", "mq", "平台域", "异步事件通知"),
    ("com.crm.opportunity", "商机中心", "service", "销售域", "商机信息读取"),
    ("iam.auth.gateway", "统一认证网关", "gateway", "平台域", "登录态与权限校验"),
    ("com.sale.order.portal", "订单门户", "frontend_app", "销售域", "订单侧报价入口"),
    ("com.sale.mobile.app", "销售移动端", "mobile_app", "销售域", "移动端报价入口"),
    ("com.partner.api.gateway", "伙伴开放网关", "gateway", "生态域", "伙伴报价API入口"),
]

EDGES = [
    ("com.sale.quote.center", "com.sale.quote.core", "sync_call", 0.92, "页面提交/保存报价单"),
    ("com.sale.quote.center", "com.sale.contract.center", "sync_call", 0.65, "合同生成后回跳报价页面"),
    ("com.sale.quote.center", "iam.auth.gateway", "sync_call", 0.35, "权限校验"),
    ("com.sale.quote.core", "cache.quote.rule", "cache_read", 0.88, "读取报价规则缓存"),
    ("com.sale.quote.core", "db.quote.mysql", "db_read_write", 0.74, "报价数据读写"),
    ("com.sale.quote.core", "com.crm.opportunity", "sync_call", 0.52, "读取商机上下文"),
    ("com.sale.quote.core", "mq.sale.event", "async_publish", 0.31, "发布报价事件"),
    ("com.sale.contract.center", "com.sale.quote.core", "sync_call", 0.48, "合同金额校验"),
    ("com.sale.order.portal", "com.sale.quote.center", "sync_call", 0.76, "订单页面嵌入报价组件"),
    ("com.sale.mobile.app", "com.sale.quote.center", "sync_call", 0.68, "移动端发起报价保存"),
    ("com.partner.api.gateway", "com.sale.quote.center", "api_call", 0.57, "伙伴渠道调用报价能力"),
    ("com.sale.contract.center", "com.sale.quote.center", "callback", 0.52, "合同生成后回跳报价页面"),
]

# appid, day_offset, time_bucket, p95, error, req, note
HIST = []
for day, p95, err, req, note in [
    (1, 690, 0.011, 2420, "正常同时间段"),
    (2, 720, 0.012, 2390, "正常同时间段"),
    (3, 660, 0.010, 2510, "正常同时间段"),
    (4, 810, 0.018, 2488, "轻微升高但无用户反馈"),
    (5, 645, 0.011, 2375, "正常同时间段"),
    (6, 760, 0.015, 2460, "轻微波动"),
    (7, 700, 0.012, 2401, "正常同时间段"),
]:
    HIST.append(("com.sale.quote.center", day, "10:05-10:20", p95, err, req, note))
for app in ["com.sale.quote.core", "cache.quote.rule", "com.sale.contract.center"]:
    for day, p95, err, req, note in [
        (1, 520, 0.007, 3100, "依赖正常"),
        (2, 560, 0.008, 3060, "依赖正常"),
        (3, 540, 0.007, 3190, "依赖正常"),
        (4, 610, 0.009, 3125, "依赖轻微波动"),
        (5, 515, 0.007, 3010, "依赖正常"),
        (6, 580, 0.008, 3090, "依赖正常"),
        (7, 545, 0.007, 3077, "依赖正常"),
    ]:
        adj = 80 if app == "cache.quote.rule" else 0
        HIST.append((app, day, "10:05-10:20", p95 + adj, err, req, note))


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE app_nodes(appid TEXT PRIMARY KEY, name TEXT, kind TEXT, domain TEXT, description TEXT);
    CREATE TABLE app_edges(src TEXT, dst TEXT, relation TEXT, weight REAL, description TEXT,
      PRIMARY KEY(src,dst,relation));
    CREATE TABLE historical_metrics(appid TEXT, day_offset INTEGER, time_bucket TEXT,
      p95_latency_ms REAL, error_rate REAL, request_count REAL, note TEXT);
    CREATE INDEX idx_edges_src ON app_edges(src);
    CREATE INDEX idx_edges_dst ON app_edges(dst);
    CREATE INDEX idx_hist_app_bucket ON historical_metrics(appid,time_bucket);
    """)
    cur.executemany("INSERT INTO app_nodes VALUES (?,?,?,?,?)", NODES)
    cur.executemany("INSERT INTO app_edges VALUES (?,?,?,?,?)", EDGES)
    cur.executemany("INSERT INTO historical_metrics VALUES (?,?,?,?,?,?,?)", HIST)
    con.commit()
    con.close()
    print(DB)

if __name__ == "__main__":
    main()
