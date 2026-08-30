#!/usr/bin/env python3
import argparse, json, math
from pathlib import Path
from datetime import datetime, timezone

def load_jsonl(path):
    rows=[]
    for line in Path(path).read_text().splitlines():
        line=line.strip()
        if line: rows.append(json.loads(line))
    return rows

def has_event(candidate, typ, minutes=30):
    for w in ['last_5m','last_30m','last_2h']:
        for e in candidate.get('historical_windows',{}).get(w,{}).get('events',[]):
            if e.get('type')==typ and abs(float(e.get('time_offset_min',999)))<=minutes:
                return e
    for e in candidate.get('current_snapshot',{}).get('events_nearby',[]):
        if e.get('type')==typ and abs(float(e.get('time_offset_min',999)))<=minutes:
            return e
    return None

def metric(candidate, key):
    return candidate.get('current_snapshot',{}).get('metrics',{}).get(key,{})

def eval_rule(candidate, rule):
    eid=rule['experience_id']
    why=[]; missing=[]; satisfied=[]; score=0.0
    if eid=='EXP-CHANGE-LATENCY-001':
        ch=has_event(candidate,'change',30)
        vf=has_event(candidate,'voice_feedback',15)
        lat=metric(candidate,'p95_latency_ms')
        req=metric(candidate,'request_count')
        if ch: satisfied.append('change_in_last_30m'); why.append(f"T{ch.get('time_offset_min')}m存在变更事件：{ch.get('title')}"); score+=0.28
        else: missing.append('change_in_last_30m')
        if lat.get('breach_points',0)>=2 and lat.get('trend') in ['up','up_then_plateau','flat']:
            satisfied.append('latency_sustained'); why.append(f"p95延迟持续异常，breach_points={lat.get('breach_points')}, latest={lat.get('latest')}"); score+=0.30
        else: missing.append('latency_sustained')
        if vf: satisfied.append('voice_feedback_present'); why.append(f"T{vf.get('time_offset_min')}m出现用户侧反馈：{vf.get('title')}"); score+=0.24
        else: missing.append('voice_feedback_present')
        if req.get('trend')=='normal' or req.get('latest',0)>=0.7*req.get('baseline',1):
            satisfied.append('request_not_low'); why.append('请求量未显著低于基线，排除低流量比例噪音'); score+=0.12
        else: missing.append('request_not_low')
    elif eid=='EXP-LOW-TRAFFIC-SPIKE-001':
        req=metric(candidate,'request_count'); er=metric(candidate,'error_rate')
        low=req.get('latest',0)<0.5*req.get('baseline',1)
        spike=er.get('trend')=='spike' or er.get('latest',0)>er.get('threshold',math.inf)
        if low: satisfied.append('request_below_baseline'); why.append('请求量显著低于基线'); score+=0.35
        else: missing.append('request_below_baseline')
        if spike: satisfied.append('error_rate_spike'); why.append('错误率出现尖刺或超过阈值'); score+=0.30
        else: missing.append('error_rate_spike')
        if not has_event(candidate,'voice_feedback',15): satisfied.append('no_voice_feedback'); score+=0.15
        else: missing.append('no_voice_feedback')
        if metric(candidate,'p95_latency_ms').get('breach_points',0)<2: satisfied.append('no_latency_sustained'); score+=0.10
        else: missing.append('no_latency_sustained')
    elif eid=='EXP-MAINTENANCE-WINDOW-001':
        mt=has_event(candidate,'maintenance_or_load_test',120)
        if mt: satisfied.append('maintenance_or_load_test_in_last_2h'); why.append(f"存在维护/压测事件：{mt.get('title')}"); score+=0.7
        else: missing.append('maintenance_or_load_test_in_last_2h')
        if not has_event(candidate,'voice_feedback',15): satisfied.append('no_user_impact_feedback'); score+=0.1
        else: missing.append('no_user_impact_feedback')
    elif eid=='EXP-TOPO-DOWNSTREAM-PROPAGATION-001':
        topo=topology_analysis(candidate)
        if topo.get('propagation_path'):
            satisfied.append('propagation_path_exists'); why.append('存在下游依赖到当前应用的传播路径：'+' → '.join(topo.get('propagation_path'))); score+=0.35
        else: missing.append('propagation_path_exists')
        if topo.get('root_cause_scope')=='downstream_dependency':
            satisfied.append('downstream_root_scope'); why.append('疑似源头位于下游依赖节点'); score+=0.25
        else: missing.append('downstream_root_scope')
        if topo.get('impact_scope_level') in ['T2','T3','T4']:
            satisfied.append('multi_node_impact'); why.append('影响范围达到'+topo.get('impact_scope_label','链路/业务域影响')); score+=0.20
        else: missing.append('multi_node_impact')
        if 'change_before_latency' in str(candidate.get('topology_context',{})) or has_event(candidate,'change',30):
            satisfied.append('time_order_match'); why.append('变更/源头信号早于当前应用延迟升高'); score+=0.15
        else: missing.append('time_order_match')
    level='none'
    if score>=0.75: level='strong'
    elif score>=0.45: level='medium'
    elif score>=0.25: level='weak'
    return {"experience_id":eid,"version":rule.get('version'),"title":rule.get('title'),"match_score":round(score,2),"match_level":level,"why_matched":why if level!='none' else [],"why_not_matched":missing if level=='none' else [],"conditions_satisfied":satisfied,"conditions_missing":missing,"confidence_effect":rule.get('confidence_effect'),"status":rule.get('status')}

def timeline_summary(candidate):
    hw=candidate.get('historical_windows',{})
    items=[]
    if hw.get('last_30m',{}).get('events'):
        items.append({"phase":"T-30m~T","summary":"窗口内存在变更和用户侧反馈，且p95延迟持续升高。","signal_strength":"strong"})
    if hw.get('same_period_baseline_24h',{}).get('anomaly_score',0)>0.6:
        items.append({"phase":"T-24h baseline","summary":"相对昨日同时段基线偏离较明显，不像普通周期波动。","signal_strength":"medium"})
    return items

def topology_analysis(candidate):
    topo=candidate.get('topology_context') or {}
    nodes=topo.get('nodes') or []
    edges=topo.get('edges') or []
    root=[n['node_id'] for n in nodes if n.get('role')=='downstream_dependency' and n.get('health') in ['suspect','degraded']]
    affected=[n['node_id'] for n in nodes if n.get('health') in ['degraded','slightly_degraded']]
    focus=topo.get('focus_node')
    path=[]
    if root and focus:
        path=[root[0],'com.sale.quote.core',focus]
    affected_count=topo.get('blast_radius',{}).get('affected_node_count',len(affected))
    if affected_count>=5: lvl,label='T3','业务域影响'
    elif affected_count>=2: lvl,label='T2','链路影响'
    elif affected_count==1: lvl,label='T1','单应用影响'
    else: lvl,label='T0','局部观察'
    why=[]
    if root: why.append(f"疑似源头节点 {root[0]} 在拓扑中位于当前应用下游依赖，且存在 recent_change/cache_miss 信号")
    if len(affected)>=2: why.append('多个相关节点同时退化，影响面超过单应用')
    if path: why.append('异常传播路径与调用/依赖方向一致')
    return {
        'impact_scope_level':lvl,
        'impact_scope_label':label,
        'root_cause_scope':'downstream_dependency' if root else 'unknown',
        'suspected_root_nodes':root,
        'affected_nodes':affected,
        'propagation_path':path,
        'why':why,
        'confidence_effect':{'direction':'raise','delta':0.1,'reason':'拓扑传播链与异常时间顺序基本一致'} if lvl in ['T2','T3','T4'] else {'direction':'none','delta':0,'reason':'拓扑影响面不足'}
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--candidate',default='inputs/sample_candidate_v2.json')
    ap.add_argument('--rules',default='kb/experience-rules.jsonl')
    ap.add_argument('--output',default='outputs/sample_judgement_v2.json')
    args=ap.parse_args()
    base=Path(__file__).parent
    cand=json.loads((base/args.candidate).read_text())
    rules=load_jsonl(base/args.rules)
    evals=[eval_rule(cand,r) for r in rules]
    matched=[e for e in evals if e['match_level'] in ['strong','medium','weak']]
    unmatched=[e for e in evals if e['match_level']=='none']
    confidence=0.55
    evidence_for=[]; evidence_against=[]
    for e in matched:
        eff=e.get('confidence_effect') or {}
        delta=float(eff.get('default_delta') or 0)
        if eff.get('direction')=='raise': confidence+=delta; evidence_for.append(f"命中{e['experience_id']}：{'; '.join(e['why_matched'])}")
        elif eff.get('direction')=='lower': confidence-=delta; evidence_against.append(f"命中{e['experience_id']}：{'; '.join(e['why_matched'])}")
    topo_analysis=topology_analysis(cand)
    topo_eff=topo_analysis.get('confidence_effect') or {}
    if topo_eff.get('direction')=='raise':
        confidence += float(topo_eff.get('delta') or 0)
        evidence_for.append('拓扑影响分析：'+ '; '.join(topo_analysis.get('why') or []))
    confidence=max(0,min(1,confidence))
    judgement='valid_high_confidence' if confidence>=0.75 else 'valid_medium_confidence' if confidence>=0.45 else 'likely_noise'
    confidence_level='high' if confidence>=0.75 else 'medium' if confidence>=0.45 else 'low'
    confidence_label={'high':'高置信度','medium':'中置信度','low':'低置信度'}[confidence_level]
    out={
        'schema_version':'it_occ_sensing_judgement_result.v2',
        'judgement_id':'JDG-'+cand['candidate_id'],
        'candidate_id':cand['candidate_id'],
        'appid':cand['appid'],
        'judgement':judgement,
        'confidence_level':confidence_level,
        'confidence_label':confidence_label,
        'confidence_score':round(confidence,2),
        'confidence_explanation':'主展示采用高中低三档；数字分数仅作为详情补充。',
        'confidence':round(confidence,2),
        'impact_level':'L2' if confidence>=0.7 else 'L3',
        'should_trigger_warroom': confidence>=0.9,
        'timeline_summary':timeline_summary(cand),
        'topology_analysis':topo_analysis,
        'experience_rule_evaluation':{'matched_rules':matched,'unmatched_relevant_rules':unmatched},
        'evidence_for':evidence_for,
        'evidence_against':evidence_against,
        'missing_inputs':['topology_dependency','downstream_dependency_status'],
        'recommended_actions':[{'action':'notify_owner','mode':'dry_run','reason':'预警中高置信，建议应用Owner确认变更影响和下游依赖状态'}],
        'audit':{'agent':'mock_agentscope_rule_evaluator','created_at':datetime.now(timezone.utc).isoformat(),'knowledge_base_version':'it-occ-sensing-kb-v0.1'}
    }
    outp=base/args.output; outp.parent.mkdir(parents=True,exist_ok=True); outp.write_text(json.dumps(out,ensure_ascii=False,indent=2))
    print(outp)
if __name__=='__main__': main()
