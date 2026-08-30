#!/usr/bin/env python3
import argparse,json
from pathlib import Path

def read_jsonl(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]

def write_jsonl(p, rows):
    Path(p).write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--rules',default='kb/experience-rules.jsonl')
    ap.add_argument('--feedback',default='feedback/sample_feedback_true_positive.json')
    ap.add_argument('--out',default='kb/experience-rules.updated.jsonl')
    args=ap.parse_args(); base=Path(__file__).parent
    rules=read_jsonl(base/args.rules); fb=json.loads((base/args.feedback).read_text())
    byid={(r['experience_id'],r.get('version')):r for r in rules}
    for rf in fb.get('rule_feedback',[]):
        r=byid.get((rf.get('experience_id'),rf.get('version')))
        if not r: continue
        stats=r.setdefault('verification_stats',{})
        if rf.get('agent_match_status')=='matched': stats['hit_count']=int(stats.get('hit_count') or 0)+1
        hf=rf.get('human_feedback')
        if hf in ['match_correct','unmatch_correct']:
            stats['confirmed_correct']=int(stats.get('confirmed_correct') or 0)+1
        elif hf in ['match_wrong','should_have_matched']:
            stats['confirmed_wrong']=int(stats.get('confirmed_wrong') or 0)+1
        denom=int(stats.get('confirmed_correct') or 0)+int(stats.get('confirmed_wrong') or 0)
        stats['precision']=round((int(stats.get('confirmed_correct') or 0)/denom),3) if denom else None
        if denom>=1 and stats['precision']>=0.8: r['status']='verified_candidate'
    topo_fb=fb.get('topology_feedback') or {}
    if topo_fb:
        for r in rules:
            if r.get('experience_id')=='EXP-TOPO-DOWNSTREAM-PROPAGATION-001':
                ts=r.setdefault('topology_verification_stats',{})
                if topo_fb.get('source_node_accuracy')=='correct': ts['source_node_correct']=int(ts.get('source_node_correct') or 0)+1
                elif topo_fb.get('source_node_accuracy') in ['wrong','不准确']: ts['source_node_wrong']=int(ts.get('source_node_wrong') or 0)+1
                acc=topo_fb.get('impact_scope_accuracy')
                if acc=='correct': ts['blast_radius_correct']=int(ts.get('blast_radius_correct') or 0)+1
                elif acc=='overestimated': ts['blast_radius_overestimated']=int(ts.get('blast_radius_overestimated') or 0)+1
                elif acc=='underestimated': ts['blast_radius_underestimated']=int(ts.get('blast_radius_underestimated') or 0)+1
    write_jsonl(base/args.out,rules)
    print(base/args.out)
if __name__=='__main__': main()
