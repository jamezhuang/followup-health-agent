"""Offline source-domain review. Expected domains never enter the agent's inputs."""
import argparse,json,sys
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_agent_experiments import save

# Evaluation references only; these six institutions are not added to source_registry.
REFERENCES=[('ynpfph.cn','https://www.ynpfph.cn/'),('gsyy.cn','https://www.gsyy.cn/info/10341/69841.htm'),
 ('fjsl.com.cn','https://fjsl.com.cn/sy.htm'),('phhp.com.cn','https://phhp.com.cn/'),
 ('zrlyyy.com','https://www.dayi.org.cn/hospital/2382.html'),('xydyfy.com','https://www.xydyfy.com/')]
def read(path):return json.loads(path.read_text(encoding='utf8'))
def score(folder):
    report=read(folder/'report.json');meta=read(folder/'metadata.json');rows=[]
    for row,(host,reference) in zip(report['rows'],REFERENCES):
        sources=row.get('lookup',{}).get('sources',[])
        def matching(source):
            actual=(urlsplit(source.get('url','')).hostname or '').removeprefix('www.')
            return (actual==host or actual.endswith('.'+host)) and len(source.get('text',''))>80
        matched=[s for s in sources if matching(s)]
        rows.append({'province':row['province'],'hospital':row['hospital'],'any_body':bool(sources),'reference_host_body':bool(matched),
            'government_body':any(s.get('authority')=='government' for s in sources),
            'matched_urls':[s['url'] for s in matched],'review_reference':reference,'elapsed_s':row.get('elapsed_s'),
            'known_tokens':row['tokens']['known_total_tokens'],'unknown_usage_calls':row['tokens']['unknown_usage_calls']})
    return {'rows':rows,'n':len(rows),'reference_host_body_n':sum(r['reference_host_body'] for r in rows),'any_body_n':sum(r['any_body'] for r in rows),
        'known_tokens':sum(r['known_tokens'] for r in rows),'unknown_usage_calls':sum(r['unknown_usage_calls'] for r in rows),
        'metadata':meta,'artifacts':str(folder)}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--before',type=Path,required=True);parser.add_argument('--after',type=Path,required=True);parser.add_argument('--regression',type=Path,required=True);args=parser.parse_args()
    before=score(args.before);after=score(args.after);regression=read(args.regression/'summary.json')
    assert before['metadata']['registry_sha']==after['metadata']['registry_sha']
    assert before['metadata']['input_urls'] is False and after['metadata']['input_urls'] is False
    assert not before['metadata']['registered_matches'] and not after['metadata']['registered_matches']
    replay=read(ROOT/'data/agent_eval_v2/round4/candidate_replay_final.json')
    output={'before':before,'after':after,'candidate_replay':replay,'offline_regression':regression,
        'limits':['Six development probes, not nationwide validation or a blind holdout','Domain match means a body from an independently reviewed host, not complete answer correctness','Jilin reference is secondary because the official site was not independently read','No test hospital URL supplied to the agent; no six-hospital registry additions','One live provider configured: bocha; cannot assume other engines were exercised']}
    dest=ROOT/'data/agent_eval_v2/round4';save(dest/'cross_province_summary.json',output)
    table='\n'.join('| '+row['province']+' | '+row['hospital']+' | '+('是' if old['reference_host_body'] else '否')+' | '+('是' if row['reference_host_body'] else '否')+' |' for old,row in zip(before['rows'],after['rows']))
    total_tests=sum(s.get('tests',{}).get('n',0) for s in regression['stages'].values())
    text=f'''# 第四轮：通用医院官网发现（2026-10-19）

目标为任意医院名称动态检索；已登记官网仅是优先线索，不是访问准入名单。本轮跨省样本不提供网址、未加入官网登记；前后登记文件哈希一致。评估域名仅在本汇总脚本中，不传入agent。

## 六家未登记医院实测

| 地区 | 医院 | 优化前取得对照官网正文 | 最新取得对照官网正文 |
|---|---|---|---|
{table}

优化前 {before['reference_host_body_n']}/{before['n']}，最新 {after['reference_host_body_n']}/{after['n']}。两轮取得任意公开正文分别为 {before['any_body_n']}/{before['n']}、{after['any_body_n']}/{after['n']}；公开资料、政府资料、医院自身网页三者分开，不能全部当作官网命中。不同域名的疑似医院页面不冒充已核对官网。

## 通用修正

1. 未登记医院先做官网候选发现，再按服务问题检索；不构造医院域名。
2. 搜索服务返回约50条候选时，先按机构发布者线索排序，再截到12条。仅调整排序，不提高来源可信等级。
3. 将候选排序依据带到后续选页，避免排序后又被服务关键词覆盖。仍保留总页数和超时限制。
4. 只跟随读取页面中的实际官网链接。已发现站点的旧路径失效时，可尝试同一URL来源的根目录，不另猜主机名；未知来源仍为unverified。
5. 记录逐页URL、读取方式与错误，区分未召回、提前截断、DNS失败、HTTP失败、浏览器忙、正文缺失。

冻结候选池回放：云南官网原第49位变第2位，甘肃第21/23位变第1/2位，福建第43位变第1位，海南第34位变第2位。吉林、新疆的对照官网不在该轮50条候选里，排序无法补回；不能将候选回放4/6当作联网正文4/6，联网结果以上表为准。

## 回归与用量

最新固定离线回归 {total_tests} 项，{'通过' if regression['passed'] else '未通过'}。新增测试覆盖名单外发现、截断前排序、优先级传递、真实链接跟随、同源恢复、来源不自动升级和两家同名关联机构隔离。完整模型回归在前一子阶段为意图99/100、端到端15/16、单位4/4；通用发现最终改动后未重新跑这100题，不能将其当作最终版本重测成绩。

六院完整检索与回答的已知模型token：优化前 {before['known_tokens']}，最新 {after['known_tokens']}，变化 {(after['known_tokens']/before['known_tokens']-1):+.1%}。未知用量调用：{before['unknown_usage_calls']}、{after['unknown_usage_calls']}。这是固定六个开发题的观测；博查查询费用、网页读取和浏览器资源不计入模型token。中间所有试验保留各自目录，不以重试后的成绩覆盖失败。部分阶段有并行实验，耗时不能当作稳定性能承诺。

## 仍未证明的能力

六个省区不代表全国所有医院，也未覆盖足够的地市、县级医院与同名机构；官网正文命中不代表院区数量和实时交通完整正确。当前实际可用搜索服务仅博查，多引擎适配代码不等于多引擎已配置或测试。上游未召回、过期网址和动态站点读取仍需继续验证；不通过逐家补网址掩盖问题。

原始目录：优化前 `{args.before.as_posix()}`，最新 `{args.after.as_posix()}`。所有额外中间轮保留在同级cross-province目录中。独立来源对照链接保存在cross_province_summary.json，吉林仅使用次级参考，未宣称完成官网身份核验。
'''
    (dest/'CROSS_PROVINCE_REPORT.md').write_text(text,encoding='utf8');print(str(dest/'CROSS_PROVINCE_REPORT.md'))
if __name__=='__main__':main()
