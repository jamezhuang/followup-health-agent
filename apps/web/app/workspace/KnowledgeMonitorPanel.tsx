"use client";
import {useEffect,useState} from "react";
import {api,safeUrl} from "./v2-api";
import type {DocumentRow} from "./v2-api";
import s from "./v2.module.css";

type Inspection={id:string;document_id:string;title:string;target_revision:number;url:string;status:string;outcome:string;error_code:string;review_state:string;attempts:number;
  before_text?:string;published_text?:string;snapshot?:{text?:string;url?:string;authority?:string;published_at?:string;collected_at?:string;knowledge_text_identical?:boolean}};
const outcomes:Record<string,string>={baseline_created:"首次采集，待核对",unchanged:"网页文本未变化",changed:"网页文本有变化，待审核"};
const errors:Record<string,string>={external_disabled:"外部读取未启用",source_stale:"资料版本已变化，请重新巡检",inspection_timeout:"读取超时",source_read_failed:"来源读取失败",snapshot_size:"正文过短或超出限制，需人工检查",source_host_changed:"来源域名发生变化，请先核对链接",worker_interrupted:"服务重启中断，可重试",source_unverified:"来源暂不可读取",browser_busy:"浏览器忙，可稍后重试",non_public_host:"不允许访问非公网地址"};

export default function KnowledgeMonitorPanel({documents,onChanged}:{documents:DocumentRow[];onChanged:()=>Promise<void>}){
  const eligible=documents.filter(doc=>doc.status==="published" && doc.url);
  const [selected,setSelected]=useState<string[]>([]);
  const [tasks,setTasks]=useState<Inspection[]>([]);
  const [detail,setDetail]=useState<Inspection|null>(null);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState("");
  const [pollError,setPollError]=useState("");
  useEffect(()=>{
    let stopped=false;
    let timer:ReturnType<typeof setTimeout>;
    async function poll(){
      try{const result=await api<{items:Inspection[]}>("/v2/admin/knowledge-inspections");if(!stopped){setTasks(result.items);setPollError("");}}
      catch{if(!stopped)setPollError("巡检任务暂时无法获取，将自动重试。");}
      finally{if(!stopped)timer=setTimeout(poll,2000);}
    }
    void poll();return()=>{stopped=true;clearTimeout(timer);};
  },[]);
  async function submit(){
    setBusy(true);setMessage("");
    try{const result=await api<{items:Inspection[]}>("/v2/admin/knowledge-inspections","POST",{document_ids:selected});
      setTasks(old=>[...result.items,...old.filter(task=>!result.items.some(item=>item.id===task.id))]);
      setSelected([]);setMessage(`已提交或复用 ${result.items.length} 个巡检任务；不会自动覆盖或发布。`);
    }catch(error){setMessage(error instanceof Error?error.message:"巡检提交失败");}finally{setBusy(false);}
  }
  async function open(task:Inspection){
    setBusy(true);
    try{setDetail(await api<Inspection>(`/v2/admin/knowledge-inspections/${task.id}`));}
    catch(error){setMessage(error instanceof Error?error.message:"详情读取失败");}finally{setBusy(false);}
  }
  async function review(action:"apply"|"dismiss"){
    if(!detail)return;setBusy(true);
    try{await api(`/v2/admin/knowledge-inspections/${detail.id}/review`,"POST",{action});
      setDetail({...detail,review_state:action==="apply"?"applied":"dismissed"});
      setTasks(old=>old.map(task=>task.id===detail.id?{...task,review_state:action==="apply"?"applied":"dismissed"}:task));
      setMessage(action==="apply"?"已保留旧内容并采用为草稿，当前资料暂停发布；请核对正文与日期后重新审核发布。":"已标记暂不采用，已发布知识没有改变。");
      await onChanged();
    }catch(error){setMessage(error instanceof Error?error.message:"审核未完成");}finally{setBusy(false);}
  }
  async function retry(task:Inspection){
    setBusy(true);
    try{const next=await api<Inspection>(`/v2/admin/knowledge-inspections/${task.id}/retry`,"POST");setTasks(old=>old.map(row=>row.id===next.id?next:row));}
    catch(error){setMessage(error instanceof Error?error.message:"重试失败");}finally{setBusy(false);}
  }
  return <section className={s.card} aria-label="资料更新巡检">
    <h3>资料更新巡检</h3>
    <p className={s.muted}>手动检查已发布公共资料的原始链接，每批最多20份。首次采集建立网页观察基线；后续比较网页文本。导航、时间戳也可能造成变化，需人工核对。</p>
    <details><summary>选择来源（已选 {selected.length} / 20，可巡检 {eligible.length} 份）</summary>
      <div className={s.row}><button type="button" className={s.secondary} disabled={busy} onClick={()=>setSelected(eligible.slice(0,20).map(doc=>doc.id))}>选择前20份</button><button type="button" className={s.secondary} onClick={()=>setSelected([])}>清空选择</button></div>
      <div className={s.importTaskScroll} role="region" aria-label="待巡检来源" tabIndex={0}>
        {!eligible.length && <p>暂无带来源链接的已发布资料。</p>}
        {eligible.map(doc=><label className={s.importTask} key={doc.id}><input type="checkbox" checked={selected.includes(doc.id)} disabled={busy || (!selected.includes(doc.id) && selected.length>=20)} onChange={event=>setSelected(old=>event.target.checked?[...old,doc.id]:old.filter(id=>id!==doc.id))}/><span style={{flex:1,overflowWrap:"anywhere"}}>{doc.title}</span></label>)}
      </div>
    </details>
    <button className={s.secondary} disabled={busy || !selected.length} onClick={()=>void submit()}>开始巡检所选来源</button>
    <p role="status">{message}</p>{pollError && <p role="status">{pollError}</p>}
    <div className={s.importTaskScroll} role="region" aria-label="巡检任务列表" tabIndex={0}>
      {!tasks.length && <p className={s.muted}>尚无巡检记录。</p>}
      {tasks.map(task=><div className={s.importTask} key={task.id}>
        <div><strong>{task.title}</strong><p className={s.muted}>版本 {task.target_revision} · {task.status==="completed"?outcomes[task.outcome]:task.status==="failed"?(errors[task.error_code] || "来源读取失败"):task.status==="queued"?"排队中":"读取来源中"} · 已尝试 {task.attempts} / 3 次{task.review_state==="applied"?" · 已采用为草稿":task.review_state==="dismissed"?" · 暂不采用":""}</p></div>
        {task.status==="completed" && <button className={s.secondary} disabled={busy} onClick={()=>void open(task)}>查看巡检对比</button>}
        {task.status==="failed" && task.attempts<3 && task.error_code!=="source_stale" && <button className={s.secondary} disabled={busy} onClick={()=>void retry(task)}>重试巡检</button>}
      </div>)}
    </div>
    {detail && <section className={s.card} aria-label="巡检快照对比">
      <h4>{detail.title} · {outcomes[detail.outcome]}</h4>
      <p className={s.muted}>网页变化不是事实变化或官网身份认证。采用前请核实正文、机构、适用范围和发布日期。</p>
      {safeUrl(detail.snapshot?.url || "") && <a href={safeUrl(detail.snapshot?.url || "")} target="_blank" rel="noreferrer">打开本次来源页面</a>}
      <p className={s.muted}>来源等级：{detail.snapshot?.authority || "未确认"} · 来源发布日期：{detail.snapshot?.published_at || "未提取到，需人工核对"} · 采集日期：{detail.snapshot?.collected_at || "未知"}</p>
      <div className={s.grid}>
        <div><h4>{detail.outcome==="baseline_created"?"提交时的知识库内容（可能是节选）":"上次成功读取的网页文本"}</h4><pre className={s.snapshotText}>{detail.outcome==="baseline_created"?detail.published_text:detail.before_text}</pre></div>
        <div><h4>本次读取的网页文本</h4><pre className={s.snapshotText}>{detail.snapshot?.text || "无可用正文"}</pre></div>
      </div>
      {detail.outcome!=="baseline_created" && <details><summary>查看巡检提交时的知识库内容</summary><pre className={s.snapshotText}>{detail.published_text}</pre></details>}
      <div className={s.row}>
        {detail.review_state==="unreviewed" && !detail.snapshot?.knowledge_text_identical && ["baseline_created","changed","unchanged"].includes(detail.outcome) && <>
          <button className={s.primary} disabled={busy} onClick={()=>void review("apply")}>采用为草稿（暂停当前发布）</button>
          <button className={s.secondary} disabled={busy} onClick={()=>void review("dismiss")}>暂不采用</button>
        </>}
        <button className={s.secondary} onClick={()=>setDetail(null)}>关闭巡检对比</button>
      </div>
    </section>}
  </section>;
}
