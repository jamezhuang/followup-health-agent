"use client";

import {useCallback, useEffect, useState} from "react";
import type {FormEvent} from "react";
import {request} from "./workspace-api";
import type {Doc} from "./workspace-api";
import styles from "./health-memory.module.css";

type FieldName = "age" | "history" | "allergies";
type MemoryField = {value:string; source:string; confirmed_at:string; effective_date?:string|null};
type Candidate = {id:string; field:FieldName|"observation"; label:string; value:string; unit:string;
  effective_date:string|null; document_name:string; location:string; quote:string; stale:boolean;
  comparison:string; current_value:string; status:string; record_conflict?:boolean;
  evidence?:{piece_index:number; location:string; quote:string}[]};
type Snapshot = {version:number; fields:Partial<Record<FieldName,MemoryField>>; pending:Candidate[]; observations:Candidate[];
  ignored_pending_count?:number};
type ExtractionDetails = {partial:boolean; accepted_count?:number; rejected_count?:number;
  repair_attempted?:boolean; repair_failed?:boolean; warnings?:{reason:string}[]};
type Version = {version:number; action:string; before:Record<string,MemoryField>; after:Record<string,MemoryField>; created_at:string};
type Props = {onError:(e:unknown)=>void; onNotice:(value:string)=>void};
const labels:Record<FieldName,string> = {age:"年龄",history:"既往史",allergies:"过敏史"};
const comparisonNames:Record<string,string> = {consistent:"与当前档案一致",conflict:"信息冲突，需核对",difference:"信息不同，可核对后补充",
  historical:"较早的记录",new_information:"新增信息",dated_record:"按日期保存检查记录",undated_record:"日期未明确",same_record:"同日期已有相同记录"};
const actions:Record<string,string> = {import_legacy:"导入原有档案",user_edit:"用户修改档案",propose_document:"生成候选信息",
  resolve_replace:"确认当前值",resolve_append:"确认补充信息",resolve_record:"保存历史或检查记录",resolve_keep:"保留当前值",resolve_reject:"排除候选信息"};

export default function HealthMemoryPanel({onError,onNotice}:Props) {
  const [snapshot,setSnapshot] = useState<Snapshot|null>(null);
  const [draft,setDraft] = useState<Record<FieldName,string>>({age:"",history:"",allergies:""});
  const [busy,setBusy] = useState(false);
  const [docs,setDocs] = useState<Doc[]>([]);
  const [documentId,setDocumentId] = useState("");
  const [belongsToMe,setBelongsToMe] = useState(false);
  const [versions,setVersions] = useState<Version[]>([]);
  const [nextBefore,setNextBefore] = useState<number|null>(null);
  const [loadedVersions,setLoadedVersions] = useState(false);
  const [loading,setLoading] = useState(true);

  function apply(value:Snapshot) {
    setSnapshot(value);
    setDraft({age:value.fields.age?.value||"",history:value.fields.history?.value||"",allergies:value.fields.allergies?.value||""});
  }

  const refresh = useCallback(async () => {
    const [value,list] = await Promise.all([request<Snapshot>("/workspace/health-memory"),
      request<{items:Doc[]}>("/workspace/documents")]);
    apply(value);
    setDocs(list.items.filter(d=>d.status==="ready"));
  },[]);

  useEffect(() => {
    let alive=true;
    Promise.all([request<Snapshot>("/workspace/health-memory"),request<{items:Doc[]}>("/workspace/documents")])
      .then(([value,list]) => {if(alive){apply(value);setDocs(list.items.filter(d=>d.status==="ready"));}})
      .catch(e => {if(alive)onError(e);}).finally(() => {if(alive)setLoading(false);});
    return () => {alive=false;};
  },[onError]);

  async function save(e:FormEvent) {
    e.preventDefault();if(!snapshot||busy)return;
    const fields:Partial<Record<FieldName,string>> = {};
    for(const field of Object.keys(labels) as FieldName[]) {
      if(draft[field].trim()!==(snapshot.fields[field]?.value||""))fields[field]=draft[field].trim();
    }
    if(!Object.keys(fields).length){onNotice("档案没有变化。");return;}
    setBusy(true);
    try {
      apply(await request<Snapshot>("/profile","PUT",{expected_version:snapshot.version,fields}));
      setLoadedVersions(false);setVersions([]);onNotice("已保存确认信息，并保留变更版本。");
    } catch(e){onError(e);} finally{setBusy(false);}
  }

  async function extract() {
    const document=docs.find(d=>d.id===documentId);
    if(!document||!belongsToMe||busy)return;
    setBusy(true);
    try {
      // A document may have been edited since this panel loaded its dropdown.
      const latest=await request<Doc>(`/workspace/documents/${document.id}`);
      if(latest.status!=="ready")throw new Error("资料修改后仍在处理，请等待显示‘可用于咨询’再重新提取。");
      setDocs(old=>old.map(d=>d.id===latest.id?latest:d));
      const value=await request<Snapshot&{extraction:ExtractionDetails}>(`/workspace/documents/${latest.id}/health-memory`,"POST",
        {revision:latest.revision,subject_confirmed:true});
      apply(value);setVersions([]);setLoadedVersions(false);
      const info=value.extraction;
      const notices=[info.accepted_count===0?"本次未发现明确记载的年龄、既往史或过敏史；原档案保持不变。":
        `本次校验通过 ${info.accepted_count??value.pending.filter(row=>!row.stale).length} 条候选信息，请逐项核对；确认前不会覆盖健康档案。`];
      if(info.partial)notices.push("仅扫描了部分正文，尚未覆盖完整文件。");
      if(info.rejected_count)notices.push("部分候选仍无法对应原文，已跳过，不影响已通过的条目。");
      if(info.repair_failed)notices.push("自动修正未完成，可重试提取。");
      if(info.warnings?.some(row=>row.reason==="date_omitted"))notices.push("部分日期缺少原文依据，已标记为日期未明确。");
      onNotice(notices.join(" "));
    } catch(e){onError(e);} finally{setBusy(false);}
  }

  async function resolve(row:Candidate,action:string) {
    if(!snapshot||busy)return;
    const current = action==="replace"||action==="append";
    if(current && !window.confirm(`确认“${row.value}”属于本人，且适用于当前${labels[row.field as FieldName]}？\n原报告日期：${row.effective_date||"未明确"}。此操作会生成档案新版本。`))return;
    setBusy(true);
    try {
      apply(await request<Snapshot>(`/workspace/health-memory/candidates/${row.id}/resolve`,"POST",
        {expected_version:snapshot.version,action,confirm_current:current}));
      setVersions([]);setLoadedVersions(false);onNotice("已记录核对决定，旧版本仍可查看。");
    } catch(e){onError(e);} finally{setBusy(false);}
  }

  async function loadVersions(more=false) {
    if(busy)return;setBusy(true);
    try {
      const data=await request<{items:Version[];next_before:number|null}>("/workspace/health-memory/versions"+(more&&nextBefore?`?before=${nextBefore}`:""));
      setVersions(old=>more?[...old,...data.items]:data.items);setNextBefore(data.next_before);setLoadedVersions(true);
    } catch(e){onError(e);} finally{setBusy(false);}
  }

  if(loading)return <p>正在读取个人档案…</p>;
  if(!snapshot)return <button onClick={()=>{setBusy(true);void refresh().catch(onError).finally(()=>setBusy(false));}} disabled={busy}>重新读取档案</button>;
  const profilePending = snapshot.pending.filter(row=>row.field in labels);
  const activePending = profilePending.filter(row=>!row.stale);
  const stalePending = profilePending.filter(row=>row.stale);
  return <section className={styles.panel}>
    <div className={styles.heading}><div><h2>已确认健康档案</h2><p>版本 {snapshot.version} · 记录内容经本人确认，不代表医学确诊。</p></div>
      <button type="button" disabled={busy} onClick={()=>{setBusy(true);void refresh().catch(onError).finally(()=>setBusy(false));}}>刷新核对</button></div>
    <p>提取文件只生成候选信息。已确认档案不会自动覆盖，请在下方核对并确认当前值。</p>
    <form onSubmit={save} className={styles.form}>
      {(Object.keys(labels) as FieldName[]).map(field=><label key={field}>{labels[field]}
        <textarea aria-label={labels[field]} disabled={busy} maxLength={field==="age"?40:400} value={draft[field]} onChange={e=>setDraft({...draft,[field]:e.target.value})}/>
        <small>{snapshot.fields[field]?`${snapshot.fields[field]!.source} · 确认于 ${new Date(snapshot.fields[field]!.confirmed_at).toLocaleString("zh-CN")}`:"尚未确认"}</small></label>)}
      <button disabled={busy}>确认并保存档案</button>
    </form>
    <section className={styles.section}><h2>从本人资料提取候选信息</h2><p>仅提取年龄、既往史和过敏史。本次症状、现病史、检查指标与病理结果不生成档案候选；未明确记载的字段留空。</p>
      {!!snapshot.ignored_pending_count&&<p>已隐藏 {snapshot.ignored_pending_count} 条旧版范围外或分类不正确的候选。重新提取后会归档，已确认档案不受影响。</p>}
      <select aria-label="选择提取资料" disabled={busy} value={documentId} onChange={e=>{setDocumentId(e.target.value);setBelongsToMe(false);}}>
        <option value="">选择已就绪文件</option>{docs.map(d=><option key={d.id} value={d.id}>{d.name}</option>)}</select>
      <label className={styles.check}><input type="checkbox" disabled={busy} checked={belongsToMe} onChange={e=>setBelongsToMe(e.target.checked)}/>确认该资料记录的是本人信息，不是公众科普或案例</label>
      <button type="button" disabled={busy||!documentId||!belongsToMe} onClick={()=>{void extract();}}>{busy?"处理中…":"提取并核对"}</button>
    </section>
    <section className={styles.section}><h2>待核对信息 <small>{activePending.length} 条</small></h2>
      {!activePending.length&&<p>暂无当前版本的待核对信息。</p>}
      <div className={styles.scroll}>{activePending.map(row=><article className={styles.candidate} key={row.id}>
        <div className={styles.heading}><strong>{row.field==="observation"?row.label:labels[row.field]}</strong><span className={row.comparison==="conflict"?styles.conflict:styles.tag}>{row.stale?"原资料已变化，需重新提取":comparisonNames[row.comparison]||row.comparison}</span></div>
        <p>当前档案：{row.current_value||"尚未记录"}</p><p>资料记录：<strong>{row.value} {row.unit}</strong></p>
        <p className={styles.meta}>{row.document_name} · {row.location} · 记录日期：{row.effective_date||"未明确"}</p>
        <details><summary>核对原文{(row.evidence?.length||0)>1?`（${row.evidence!.length}处依据，已合并）`:""}</summary>
          {(row.evidence?.length?row.evidence:[{piece_index:0,location:row.location,quote:row.quote}]).map((source,index)=>
            <div key={index}><p className={styles.meta}>{source.location}</p><blockquote>{source.quote}</blockquote></div>)}
        </details>
        <div className={styles.actions}>
          {row.field!=="observation"&&<button disabled={busy||row.stale} onClick={()=>{void resolve(row,"replace");}}>确认作为当前值</button>}
          {["history","allergies"].includes(row.field)&&row.comparison!=="conflict"&&<button disabled={busy||row.stale} onClick={()=>{void resolve(row,"append");}}>确认补充到档案</button>}
          <button disabled={busy||row.stale} onClick={()=>{void resolve(row,"record");}}>仅保存记录</button>
          <button disabled={busy} onClick={()=>{void resolve(row,"keep");}}>保留当前档案</button>
          <button disabled={busy} onClick={()=>{void resolve(row,"reject");}}>不属于本人或提取错误</button>
        </div>
      </article>)}</div>
      {stalePending.length>0&&<details className={styles.record}><summary>旧版本候选已失效（{stalePending.length} 条）</summary>
        <p>这些引文属于修改前的正文，不是本次提取结果。重新提取最新版本后会归档。</p>
        {stalePending.map(row=><article className={styles.candidate} key={row.id}><strong>{row.field==="observation"?row.label:labels[row.field]}：{row.value}</strong>
          <p>{row.document_name} · {row.location} · 旧版本候选</p><blockquote>{row.quote}</blockquote>
          <button disabled={busy} onClick={()=>{void resolve(row,"reject");}}>排除这条失效候选</button></article>)}
      </details>}
    </section>
    <section className={styles.section}><h2>历史与检查记录</h2><p>展示最近100条保存记录。不同日期的结果分别保存；未明确日期的记录单独标记。</p>
      <div className={styles.scroll}>{snapshot.observations.length?snapshot.observations.map(row=><article className={styles.record} key={row.id}>
        <strong>{row.label||labels[row.field as FieldName]||"资料记录"}：{row.value} {row.unit}</strong>{row.record_conflict&&<span className={styles.conflict}>同日期存在不同记录，需核对原文</span>}<p>{row.effective_date||"日期未明确"} · {row.document_name}</p><details><summary>查看记录原文</summary><blockquote>{row.quote}</blockquote></details>
      </article>):<p>尚未保存历史或检查记录。</p>}</div>
    </section>
    <section className={styles.section}><h2>变更版本</h2><button disabled={busy} onClick={()=>{void loadVersions();}}>加载变更记录</button>
      <div className={styles.scroll}>{versions.map(v=><details className={styles.record} key={v.version}><summary>版本 {v.version} · {actions[v.action]||v.action} · {new Date(v.created_at).toLocaleString("zh-CN")}</summary>
        {(Object.keys(labels) as FieldName[]).map(field=><p key={field}>{labels[field]}：{v.before[field]?.value||"未记录"} → {v.after[field]?.value||"未记录"}</p>)}</details>)}</div>
      {loadedVersions&&nextBefore&&<button disabled={busy} onClick={()=>{void loadVersions(true);}}>加载更早版本</button>}
    </section>
  </section>;
}
