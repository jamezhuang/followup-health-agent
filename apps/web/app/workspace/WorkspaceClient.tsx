"use client";

import {useCallback, useEffect, useRef, useState} from "react";
import type {FormEvent} from "react";
import {sendMessage, stopMessage} from "./cancellable-chat";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {API, ApiError, request, sourceHref, cleanText, statusNames as apiStatusNames, errorNames} from "./workspace-api";
import type {User, Source, Consultation, History, Doc, Piece, Run, RunDetail, Turn} from "./workspace-api";
import styles from "./workspace.module.css";
import answerStyles from "./answer.module.css";
import selectionStyles from "./document-selection.module.css";
import HealthMemoryPanel from "./HealthMemoryPanel";
import WorkspaceIcon from "./WorkspaceIcon";
import {conversationHistory} from "./conversation-history";
import AssistantWelcome, { SidebarToggle, useConsultationLayout } from "../components/AssistantWelcome";
import {emptyFileMentionDraft, editFileMentionDraft, syncFileMentionDraft} from "./file-mentions";
import type {FileMentionDraft, SelectedFileName} from "./file-mentions";

type Tab = "chat" | "documents" | "profile" | "runs";
const navigation: [Tab,string][] = [["chat","\u667a\u80fd\u54a8\u8be2"],["documents","\u4e2a\u4eba\u77e5\u8bc6\u5e93"],["profile","\u5065\u5eb7\u6863\u6848"],["runs","\u8fd0\u884c\u4e0e\u53cd\u9988"]];
const suggestions = ["抗生素是什么？", "咳嗽三天，去医院应该挂什么科？", "请查询佛山市有哪些三甲医院，并给出来源。", "请结合我上传的报告解释检查结果。"];
const statusNames:Record<string,string> = {...apiStatusNames,cancelled:"已停止"};

function time(value: string) {return new Date(value).toLocaleString("zh-CN");}

function answerText(content: string, turn?: Turn|null) {
  const summary = turn?.summary.trim() || "";
  // summary 是用户可见答案，不以是否包含 Markdown 标题判断。
  // 聊天 content 可能附加 claims 证据说明，仅在没有 summary 时兜底。
  return cleanText(summary || content.trim()).trim();
}

type AnswerNode = {type:string; value?:string; children?:AnswerNode[]; data?:Record<string,unknown>};

function nodeText(node:AnswerNode):string {
  return node.value || node.children?.map(nodeText).join("") || "";
}

function DepartmentCard({department}:{department:string}) {
  return <div className={styles.department} role="region" aria-label="建议就诊方向">
    <small>建议就诊方向</small><strong>{department}</strong>
  </div>;
}

function AnswerBody({text,department}: {text:string; department?:string}) {
  // Insert a display node without splitting Markdown or changing stored answers.
  // Report answers place the card after the introduction and before basic info.
  function placeDepartment() {
    return (tree:AnswerNode) => {
      if(!department?.trim() || !tree.children)return;
      let index=tree.children.findIndex(node=>["heading","paragraph"].includes(node.type) &&
        /^(?:一[、.．]\s*)?报告中的基本信息\s*$/.test(nodeText(node).trim()));
      if(index<0)index=tree.children.findIndex(node=>node.type==="heading");
      tree.children.splice(index<0?0:index,0,{type:"paragraph",
        data:{hProperties:{"data-carebrief-department":true}},
        children:[{type:"text",value:"建议就诊方向："+department.trim()}]});
    };
  }
  return <div className={answerStyles.answer}><ReactMarkdown remarkPlugins={[remarkGfm,placeDepartment]} skipHtml
    components={{
      p: ({node,children,...props}) => node?.properties?.["data-carebrief-department"] || node?.properties?.dataCarebriefDepartment
        ? <DepartmentCard department={department!.trim()}/> : <p {...props}>{children}</p>,
      a: ({href,children}) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
      table: ({children}) => <div className={answerStyles.tableWrap}><table>{children}</table></div>,
      img: () => null,
    }}>{text}</ReactMarkdown></div>;
}

function SourceCard({source, index}: {source:Source; index:number}) {
  const href = sourceHref(source);
  return <details className={styles.source}><summary><span className={styles.badge}>{source.kind === "private_document" ? "个人资料" : source.kind === "external_excerpt" ? "本轮网页" : "公共资料"}</span> 来源 {index+1} · {source.title}</summary>
    <p className={styles.muted}>{source.institution}{source.kind === "private_document" ? " · 引用确认稿，请对照原文件" : source.published_at ? ` · 发布：${source.published_at}` : " · 发布日期未核实"}</p>
    <pre>{source.text}</pre>{href && <a href={href} target="_blank" rel="noreferrer">{source.kind === "private_document" ? "下载本人原文件" : "查看原始页面"} ↗</a>}
  </details>;
}

export default function WorkspaceClient() {
  const layout = useConsultationLayout();
  const [tab,setTab] = useState<Tab>("chat");
  const [user,setUser] = useState<User|null>(null);
  const [boot,setBoot] = useState(true);
  const [registering,setRegistering] = useState(false);
  const [email,setEmail] = useState("");
  const [password,setPassword] = useState("");
  const [busy,setBusy] = useState(false);
  const [stopping,setStopping] = useState(false);
  const activeMessage = useRef<{id:string; sessionId:string; submitted:boolean; cancelled:boolean; controller:AbortController; runId:string}|null>(null);
  const [operation,setOperation] = useState(false);
  const [error,setError] = useState("");
  const [notice,setNotice] = useState("");
  const [available,setAvailable] = useState(false);
  const [admin,setAdmin] = useState(false);
  const [history,setHistory] = useState<History[]>([]);
  const [historyQuery,setHistoryQuery] = useState("");
  const historyGroups = conversationHistory(history, historyQuery);
  const [session,setSession] = useState<Consultation|null>(null);
  const [question,setQuestion] = useState("");
  const questionInput = useRef<HTMLTextAreaElement>(null);
  const mentionDraft = useRef<FileMentionDraft>(emptyFileMentionDraft());
  const previousMentionFiles = useRef<SelectedFileName[]>([]);

  function writeDraft(draft:FileMentionDraft) {
    mentionDraft.current=draft;
    setQuestion(draft.text);
  }

  function replaceQuestion(text:string) {
    writeDraft(emptyFileMentionDraft(text));
  }

  function editQuestion(text:string) {
    writeDraft(editFileMentionDraft(mentionDraft.current,text));
  }
  const [pending,setPending] = useState("");
  const [needsRefresh,setNeedsRefresh] = useState(false);
  const [docs,setDocs] = useState<Doc[]>([]);
  const [doc,setDoc] = useState<Doc|null>(null);
  const [pieces,setPieces] = useState<Piece[]>([]);
  const [selected,setSelected] = useState<string[]>([]);
  const [selectionLoading,setSelectionLoading] = useState(false);
  const selectedDocuments = docs.filter(d => d.status === "ready" && selected.includes(d.id));
  const selectedNamesKey = JSON.stringify(selectedDocuments.map(({id,name})=>({id,name})));
  const [runs,setRuns] = useState<Run[]>([]);
  const [detail,setDetail] = useState<RunDetail|null>(null);
  const [adminView,setAdminView] = useState(true);
  const [liveDetail,setLiveDetail] = useState<RunDetail|null>(null);
  const [copying,setCopying] = useState(false);
  const [copiedRunId,setCopiedRunId] = useState("");
  const generation = useRef(0);
  const selectionVersion = useRef(0);
  const end = useRef<HTMLDivElement>(null);
  const latestSession = useRef<Consultation|null>(null);
  latestSession.current = session;
  const isWelcome = !busy && !pending && !session?.messages.some(message => message.role === "user");

  const fail = useCallback((e:unknown) => {setError(e instanceof Error ? e.message : "处理失败。");},[]);

  useEffect(() => {
    let alive = true;
    request<{user:User|null}>("/auth/me").then(data => {if(alive)setUser(data.user);}).catch(e => {if(alive)fail(e);}).finally(() => {if(alive)setBoot(false);});
    const params = new URLSearchParams(window.location.search);
    const initial = params.get("tab") as Tab;
    if(navigation.some(([id]) => id === initial))setTab(initial);
    return () => {alive = false;};
  },[fail]);

  const refreshDocs = useCallback(async () => {
    const current = generation.current;
    const data = await request<{items:Doc[]}>("/workspace/documents");
    if(current === generation.current)setDocs(data.items);
  },[]);

  const refreshHistory = useCallback(async () => {
    const current = generation.current;
    const data = await request<{items:History[]}>("/sessions");
    if(current === generation.current)setHistory(data.items);
  },[]);

  useEffect(() => {
    generation.current += 1;
    const current = generation.current;
    setAdmin(false);setAdminView(true);setSession(null);setHistory([]);setDocs([]);setDoc(null);setSelected([]);setSelectionLoading(false);setHistoryQuery("");setRuns([]);setDetail(null);setLiveDetail(null);setNeedsRefresh(false);
    if(!user)return;
    let alive = true;
    async function load() {
      try {
        const [h,s] = await Promise.all([request<{items:History[]}>("/sessions"),request<{available:boolean;admin:boolean}>("/workspace/status")]);
        if(!alive || current !== generation.current)return;
        setHistory(h.items);setAvailable(s.available);setAdmin(s.admin && user!.role === "admin");
        if(s.available)await refreshDocs();
        const saved = localStorage.getItem("cb-workspace-session-"+user!.id);
        if(saved) {
          try {
            const old = await request<Consultation>(`/sessions/${saved}`);
            if(alive && current === generation.current)setSession(old);
          } catch {localStorage.removeItem("cb-workspace-session-"+user!.id);}
        }
      } catch(e) {if(alive)fail(e);}
    }
    void load();
    return () => {alive = false;generation.current += 1;};
  },[user,fail,refreshDocs]);

  useEffect(() => {
    if(!user || !available || !docs.some(d => ["queued","parsing","indexing"].includes(d.status)))return;
    const id = setInterval(() => {void refreshDocs().catch(fail);},2000);
    return () => clearInterval(id);
  },[user,available,docs,refreshDocs,fail]);

  useEffect(() => {
    if(!user || !session || !available){setSelected([]);setSelectionLoading(false);return;}
    let alive = true;
    const version = ++selectionVersion.current;
    setSelected([]);setSelectionLoading(true);
    request<{document_ids:string[]}>(`/workspace/sessions/${session.id}/selection`)
      .then(data => {if(alive && selectionVersion.current===version)setSelected(data.document_ids);})
      .catch(e => {if(alive && selectionVersion.current===version)fail(e);})
      .finally(() => {if(alive && selectionVersion.current===version)setSelectionLoading(false);});
    return () => {alive = false;};
  },[user,session?.id,available,fail]);

  useEffect(() => {
    const files:SelectedFileName[]=JSON.parse(selectedNamesKey);
    const result=syncFileMentionDraft(mentionDraft.current,previousMentionFiles.current,files);
    previousMentionFiles.current=files;
    mentionDraft.current=result.draft;
    setQuestion(result.draft.text);
    if(result.skippedNames.length)setNotice("输入框已接近字数上限，所选资料已保存；缩短问题后可手动补充文件名。");
    if(!result.added)return;
    const frame=requestAnimationFrame(()=>{
      const input=questionInput.current;
      if(!input || input.disabled)return;
      input.focus({preventScroll:true});
      input.setSelectionRange(input.value.length,input.value.length);
    });
    return ()=>cancelAnimationFrame(frame);
  },[selectedNamesKey]);

  useEffect(() => {
    if(session && user)localStorage.setItem("cb-workspace-session-"+user.id,session.id);
    const messages = end.current?.parentElement;
    messages?.scrollTo({top:isWelcome?0:messages.scrollHeight,behavior:isWelcome?"auto":"smooth"});
  },[session,user,pending,isWelcome]);

  useEffect(() => {
    if(!admin || !busy || !available || !session)return;
    let alive = true;
    const started = Date.now();
    async function poll() {
      try {
        const data = await request<{items:Run[]}>(`/workspace/runs?session_id=${session!.id}`);
        const current = data.items.find(r => new Date(r.created_at).getTime() >= started-1500);
        if(current) {
          const d = await request<RunDetail>(`/workspace/runs/${current.id}`);
          if(alive)setLiveDetail(d);
        }
      } catch { /* Keep the chat request independent of observability polling. */ }
    }
    const timer = setInterval(() => {void poll();},1600);
    void poll();
    return () => {alive = false;clearInterval(timer);};
  },[admin,busy,available,session?.id]);

  useEffect(() => {
    if (user && user.role !== "admin" && tab === "runs") {
      setTab("chat");
      const url = new URL(window.location.href); url.searchParams.set("tab", "chat"); window.history.replaceState({}, "", url);
    }
  }, [user, tab]);

  function navigate(next:Tab) {
    if(next === "runs" && !admin)return;
    setTab(next);setError("");setNotice("");
    const url = new URL(window.location.href);url.searchParams.set("tab",next);window.history.replaceState({},"",url);
  }

  async function authenticate(e:FormEvent) {
    e.preventDefault();setOperation(true);setError("");
    try {const u = await request<User>(registering ? "/auth/register" : "/auth/login","POST",{username:email,password});setPassword("");setUser(u);} catch(e){fail(e);} finally{setOperation(false);}
  }

  async function newSession() {
    const s = await request<Consultation>("/sessions","POST");setSession(s);setSelected([]);setSelectionLoading(false);setLiveDetail(null);setNeedsRefresh(false);await refreshHistory();return s;
  }

  async function openSession(id:string) {
    setOperation(true);setError("");
    try {const data = await request<Consultation>(`/sessions/${id}`);setSession(data);setLiveDetail(null);setNeedsRefresh(false);navigate("chat");} catch(e){fail(e);} finally{setOperation(false);}
  }

  async function stopGeneration() {
    const active = activeMessage.current;
    if(!active || stopping)return;
    setStopping(true);setError("");
    try {
      if(!active.submitted) {
        active.cancelled=true;active.controller.abort();
        setNotice("已取消发送，原问题将恢复到输入框。");
        return;
      }
      const result = await stopMessage(active.sessionId,active.id);
      if(activeMessage.current!==active)return;
      active.runId=result.run_id;
      if(result.status === "cancelled") {
        active.cancelled=true;active.controller.abort();
        setNotice("已停止生成，原问题已恢复到输入框。请编辑后再发送。");
      } else if(result.status === "stopping") {
        setNotice("正在停止并清理工具任务，请稍等；仍未结束时可再次点击停止。");
      } else {
        setNotice("本轮已经结束，请核对返回结果；不会撤销已完成的回答。");
      }
    } catch(e) {
      if(activeMessage.current!==active)return;
      fail(e);setNotice("尚未确认后端停止，当前请求继续等待。可以再次点击停止。");
    } finally {if(activeMessage.current===active || !activeMessage.current)setStopping(false);}
  }

  async function send(e?:FormEvent, override?:string) {
    e?.preventDefault();const value = (override || question).trim();if(!value || busy || activeMessage.current || operation || selectionLoading || needsRefresh)return;
    const chosenIds = [...selected];
    const submittedDraft = mentionDraft.current;
    const active = {id:crypto.randomUUID(),sessionId:"",submitted:false,cancelled:false,controller:new AbortController(),runId:""};
    activeMessage.current=active;
    setBusy(true);setPending(value);setError("");setNotice("");setLiveDetail(null);replaceQuestion("");
    try {
      const s = latestSession.current || await newSession();
      active.sessionId=s.id;
      if(active.cancelled)throw new ApiError("已取消发送。","","request_cancelled");
      if(available)await request(`/workspace/sessions/${s.id}/selection`,"PUT",{document_ids:chosenIds});
      if(active.cancelled)throw new ApiError("已取消发送。","","request_cancelled");
      active.submitted=true;
      const result = await sendMessage(s.id,active.id,value,active.controller.signal);
      // Stop and response can arrive together; a confirmed stop wins in the UI.
      if(active.cancelled)throw new ApiError("已停止生成。","","request_cancelled");
      setSession(result);await refreshHistory();
    } catch(e) {
      writeDraft(editFileMentionDraft(submittedDraft,value));
      const cancelled=active.cancelled || (e instanceof ApiError && e.code === "request_cancelled");
      if(cancelled) {
        setError("");setNotice("已停止生成，原问题已恢复到输入框。请编辑后再发送。");
        if(active.submitted) {
          try {setSession(await request<Consultation>(`/sessions/${active.sessionId}`));await refreshHistory();}
          catch {setNeedsRefresh(true);}
        }
      } else {
        fail(e);
        if(e instanceof ApiError && (["network","timeout"].includes(e.code)||e.status===409))setNeedsRefresh(true);
      }
      const runId=active.runId || (e instanceof ApiError ? e.runId : "");
      if(admin && runId && available) {
        try {setLiveDetail(await request<RunDetail>(`/workspace/runs/${runId}`));} catch { /* No fabricated trace. */ }
      }
    } finally {
      if(activeMessage.current===active){activeMessage.current=null;setBusy(false);setStopping(false);setPending("");}
    }
  }

  async function reconcile() {
    if(!session)return;
    setOperation(true);
    try {
      const data=await request<Consultation>(`/sessions/${session.id}`);
      const lastUser=[...data.messages].reverse().find(m=>m.role==="user");
      if(lastUser?.content===question.trim())replaceQuestion("");
      setSession(data);setNeedsRefresh(false);setError("");setNotice("已读取后端最新会话，请核对后继续。");
    } catch(e){fail(e);} finally{setOperation(false);}
  }

  async function toggleDocument(id:string, checked:boolean) {
    if(busy || operation || selectionLoading)return;
    setOperation(true);setError("");
    try {
      const next = checked ? [...selected,id] : selected.filter(x=>x!==id);
      if(next.length > 5)throw new Error("一次最多选择 5 份资料。");
      const s = session || await newSession();
      selectionVersion.current += 1;
      await request(`/workspace/sessions/${s.id}/selection`,"PUT",{document_ids:next});setSelected(next);
    } catch(e){fail(e);} finally{setOperation(false);setSelectionLoading(false);}
  }

  async function openDoc(item:Doc) {
    setOperation(true);setError("");
    try {const d = await request<Doc>(`/workspace/documents/${item.id}`);setDoc(d);setPieces(d.pieces || []);} catch(e){fail(e);} finally{setOperation(false);}
  }

  async function mutateDoc(action:"confirm"|"retry"|"delete", item:Doc) {
    if(action === "delete" && !window.confirm("删除后停止用于新检索；历史咨询中已保存的回答仍保留。继续删除？"))return;
    setOperation(true);setError("");
    try {
      if(action === "delete") {selectionVersion.current += 1;await request(`/workspace/documents/${item.id}`,"DELETE");if(doc?.id===item.id)setDoc(null);setSelected(selected.filter(x=>x!==item.id));}
      else {await request(`/workspace/documents/${item.id}/${action}`,action === "confirm" ? "PUT" : "POST",{revision:item.revision,...(action === "confirm" ? {pieces} : {})});setDoc(null);}
      await refreshDocs();
    } catch(e){fail(e);} finally{setOperation(false);}
  }

  async function upload(file:File) {
    setOperation(true);setError("");setNotice("");
    try {const data = new FormData();data.append("file",file);const result=await request<Doc & {duplicate:boolean}>("/workspace/documents","POST",data);setNotice(result.duplicate ? "本人资料中已有相同文件，未重复入库。" : "已上传，解析完成后请核对文字再确认。 ");await refreshDocs();} catch(e){fail(e);} finally{setOperation(false);}
  }


  async function loadRuns() {
    if(!admin)return;
    setOperation(true);setError("");
    try {setRuns((await request<{items:Run[]}>(adminView ? "/workspace/admin/runs" : "/workspace/runs")).items);} catch(e){fail(e);} finally{setOperation(false);}
  }

  async function loadRun(id:string) {
    if(!admin)return;
    setOperation(true);setError("");
    try {setDetail(await request<RunDetail>((adminView ? "/workspace/admin/runs/" : "/workspace/runs/")+id));} catch(e){fail(e);} finally{setOperation(false);}
  }

  async function feedback(turn:Turn, category:string) {
    setOperation(true);setError("");
    try {await request("/workspace/feedback","POST",{run_id:turn.run_id,category,comment:""});setNotice("已保存反馈，并关联本轮运行记录。");} catch(e){fail(e);} finally{setOperation(false);}
  }

  async function copyCurrentAnswer() {
    const current = latestSession.current;
    const currentTurn = current?.last_turn || current?.last_result;
    if(!current || !currentTurn || copying || busy || operation)return;
    // 只取当前会话最后一条助手答案，不读取整个页面或全部聊天历史。
    const assistant = [...current.messages].reverse().find(m=>m.role==="assistant");
    const body = answerText(assistant?.content || "", currentTurn);
    const parts:string[] = [];
    if(currentTurn.department?.trim()) {
      parts.push("建议就诊方向："+currentTurn.department);
    }
    parts.push(body);
    if(currentTurn.risk_notice && !body.includes(currentTurn.risk_notice)) {
      parts.push(cleanText(currentTurn.risk_notice));
    }
    const text = parts.filter(Boolean).join("\n\n").trim();
    if(!text){setError("本轮暂无可复制的答案。");return;}
    setCopying(true);setCopiedRunId("");setError("");
    try {
      let copied = false;
      if(navigator.clipboard?.writeText) {
        try {await navigator.clipboard.writeText(text);copied=true;} catch { /* Try local selection fallback. */ }
      }
      if(!copied) {
        const previousFocus = document.activeElement;
        const textarea = document.createElement("textarea");
        textarea.value=text;textarea.readOnly=true;
        textarea.setAttribute("aria-hidden","true");
        textarea.style.cssText="position:fixed;left:-9999px;top:0;opacity:0;pointer-events:none;";
        document.body.appendChild(textarea);
        try {
          textarea.focus();textarea.select();
          copied=document.execCommand("copy");
        } finally {
          textarea.remove();
          if(previousFocus instanceof HTMLElement)previousFocus.focus({preventScroll:true});
        }
      }
      if(!copied)throw new Error("复制失败，请允许浏览器访问剪贴板，或手动选择本轮答案复制。");
      setCopiedRunId(currentTurn.run_id);
    } catch(e){fail(e);} finally{setCopying(false);}
  }

  if(boot)return <div className={styles.loading}>正在连接健康咨询工作台…</div>;
  if(!user)return <main className={styles.login}><section className={styles.loginIntro}><AssistantWelcome onChoose={()=>document.querySelector<HTMLInputElement>('input[autocomplete="username"]')?.focus()}/><span className={styles.eyebrow}>CAREBRIEF</span><h1>让每一次咨询<br/>都有资料可查。</h1><p>管理自己的报告，查询医院与科室，核对回答来源。</p><div className={styles.loginFeature}>个人资料 · 多轮咨询 · 原文追溯</div></section><form className={styles.loginForm} onSubmit={authenticate}><h2>{registering?"创建账号":"登录工作台"}</h2><p className={styles.muted}>账号和密码为 6–20 位数字或字母，可单独使用或混合。注册默认为普通用户。</p><label>账号<input type="text" autoCapitalize="none" spellCheck={false} minLength={registering?6:1} maxLength={registering?20:160} pattern={registering?"[A-Za-z0-9]{6,20}":undefined} value={email} onChange={e=>setEmail(e.target.value)} autoComplete="username" required/></label><label>密码<input type="password" value={password} onChange={e=>setPassword(e.target.value)} minLength={registering?6:1} maxLength={registering?20:128} pattern={registering?"[A-Za-z0-9]{6,20}":undefined} autoComplete={registering?"new-password":"current-password"} required/></label>{error&&<p role="alert" className={styles.error}>{error}</p>}<button disabled={operation}>{operation?"处理中…":registering?"注册并登录":"登录"}</button><button type="button" className={styles.secondary} onClick={()=>setRegistering(!registering)}>{registering?"已有账号，去登录":"没有账号，创建账号"}</button></form></main>;

  const turn = session?.last_turn || session?.last_result;
  const lastAssistantIndex = session?.messages.map(m=>m.role).lastIndexOf("assistant") ?? -1;
  const disabled = busy || operation;
  return <div className={`${styles.shell} ${layout.collapsed?styles.collapsed:""} ${layout.sourcesOpen?styles.sourcesOpen:""}`}>
    <aside id="workspace-sidebar" className={styles.sidebar} hidden={layout.collapsed}>
      <div className={styles.sidebarHeader}>
        <button type="button" className={styles.brandHome} onClick={()=>navigate("chat")} aria-label="返回智能咨询">
          <svg className={styles.brandLogo} width="35" height="40" viewBox="0 0 35 40" fill="none" aria-hidden="true"><path d="M7 5v30" stroke="#619CFF" strokeWidth="11" strokeLinecap="round"/><path d="m8 14 19 12" stroke="#7BB4FF" strokeWidth="11" strokeLinecap="round"/><path d="M28 5v30" stroke="#3D72ED" strokeWidth="11" strokeLinecap="round"/></svg>
          <span>CareBrief<small>让健康对话，更自然</small></span>
        </button>
        <SidebarToggle collapsed={layout.collapsed} onClick={layout.toggleSidebar} controls="workspace-sidebar" />
      </div>
      <button type="button" className={styles.newConversation} disabled={disabled} onClick={async()=>{
        setOperation(true);setError("");
        try {await newSession();replaceQuestion("");setHistoryQuery("");navigate("chat");}
        catch(e){fail(e);}finally{setOperation(false);}
      }}><span><WorkspaceIcon name="plus" size={20}/></span>新对话</button>
      <label className={styles.conversationSearch}>
        <WorkspaceIcon name="search" size={18}/>
        <input type="search" aria-label="搜索对话" placeholder="搜索对话" value={historyQuery} onChange={e=>setHistoryQuery(e.target.value)} maxLength={100}/>
      </label>
      <section className={styles.conversationList} aria-label="历史对话" tabIndex={0}>
        {historyGroups.map(group=><div className={styles.historyGroup} key={group.label}>
          <h2>{group.label}</h2>
          {group.conversations.map(h=><button type="button" disabled={disabled} className={`${styles.conversationRow} ${session?.id===h.id?styles.selectedConversation:""}`} key={h.id} title={h.title} aria-current={session?.id===h.id?"true":undefined} onClick={()=>{void openSession(h.id);}}><WorkspaceIcon name="chat" size={17}/><span>{h.title||"新对话"}</span></button>)}
        </div>)}
        {!historyGroups.length&&<p className={styles.historyEmpty} role="status">{historyQuery.trim()?"没有找到相关对话":"还没有对话，开始聊聊吧"}</p>}
      </section>
      <details className={styles.accountCard}>
        <summary><span className={styles.accountAvatar}><WorkspaceIcon name="user" size={21}/></span><span className={styles.accountIdentity}>{user.username || user.email}<small>{user.role==="admin"?"管理员":"普通用户"}</small></span><WorkspaceIcon name="chevron" size={17}/></summary>
        <button type="button" disabled={disabled} onClick={async()=>{try{await request("/auth/logout","POST");setUser(null);setError("");}catch(e){fail(e);}}}>退出登录</button>
      </details>
    </aside>
    <main className={styles.main}>
      <header className={styles.header}>
        {layout.collapsed&&<SidebarToggle collapsed={layout.collapsed} onClick={layout.toggleSidebar} controls="workspace-sidebar" />}
        <div className={styles.pageIdentity}><h1>{navigation.find(([id])=>id===tab)?.[1]}</h1>{tab!=="chat"&&<button type="button" className={styles.backToChat} onClick={()=>navigate("chat")}>返回对话</button>}</div>
        <nav className={styles.topNavigation} aria-label="工作台功能">
          <button type="button" aria-current={tab==="documents"?"page":undefined} onClick={()=>navigate("documents")}><WorkspaceIcon name="library" size={17}/>个人知识库</button>
          <button type="button" aria-current={tab==="profile"?"page":undefined} onClick={()=>navigate("profile")}><WorkspaceIcon name="heart" size={17}/>健康档案</button>
          {tab==="chat"&&<button type="button" onClick={layout.toggleSources} aria-expanded={layout.sourcesOpen} aria-controls="workspace-evidence">资料与来源</button>}
          {admin&&<button type="button" aria-current={tab==="runs"?"page":undefined} onClick={()=>navigate("runs")}><WorkspaceIcon name="logs" size={17}/>运行与反馈</button>}
        </nav>
      </header>
      {error&&<div role="alert" className={styles.error}>{error}</div>}{notice&&<div role="status" className={styles.notice}>{notice}</div>}
      {!available&&<div className={styles.notice}>个人资料与运行记录暂未初始化。请按安装说明连接数据库；现有咨询接口仍可使用。</div>}
      {tab==="chat"&&<div className={styles.chatGrid}><section className={`${styles.chat} ${isWelcome?styles.welcomeState:""}`}><div className={styles.messages}>
        {isWelcome&&<div className={styles.welcome}><AssistantWelcome onChoose={text=>{replaceQuestion(text);questionInput.current?.focus();}}/><div className={styles.examples}>{suggestions.map(s=><button disabled={disabled} className={styles.secondary} key={s} onClick={()=>replaceQuestion(s)}>{s}</button>)}</div></div>}
        {!isWelcome&&session?.messages.map((m,i)=><article key={i} className={`${styles.message} ${m.role==="user"?styles.userMessage:""}`}><small>{m.role==="user"?"你":"CareBrief"}</small>{m.role==="assistant"?<AnswerBody text={answerText(m.content,i===lastAssistantIndex?turn:undefined)} department={i===lastAssistantIndex?turn?.department:undefined}/>:<div>{cleanText(m.content)}</div>}</article>)}
        {pending&&<article className={`${styles.message} ${styles.userMessage}`}><small>你 · 正在提交</small><div>{pending}</div></article>}
        {needsRefresh&&<div className={styles.notice}>本次提交状态需要核对。<button type="button" disabled={operation} onClick={()=>{void reconcile();}}>刷新当前咨询后继续</button></div>}{busy&&<div className={styles.progress} aria-live="polite">{liveDetail?.events.length ? liveDetail.events.slice(-4).map((e,i)=><p key={i}>{e.tool || e.stage} · {statusNames[e.status||""] || e.status || "处理中"}{e.source_count!==undefined?` · ${e.source_count} 个来源`:""}</p>) : <p>等待后端处理…</p>}</div>}
        {turn&&<section className={styles.result}>{turn.risk_notice&&<p className={styles.error}>{turn.risk_notice}</p>}<div className={styles.actions}><button type="button" className={styles.secondary} disabled={disabled||copying} onClick={()=>{void copyCurrentAnswer();}}>{copying?"复制中…":copiedRunId===turn.run_id?"已复制":"复制本轮回答"}</button><button className={styles.secondary} disabled={disabled||!available} onClick={()=>{void feedback(turn,"helpful");}}>有帮助</button><select aria-label="反馈问题类型" disabled={disabled||!available} value="" onChange={e=>{if(e.target.value)void feedback(turn,e.target.value);}}><option value="">反馈回答问题</option><option value="off_topic">答非所问</option><option value="irrelevant_source">来源不相关</option><option value="missing_document">遗漏个人资料</option><option value="tool_failure">工具失败</option></select></div></section>}
        <div ref={end}/></div>
        <form className={styles.composer} onSubmit={send}>
          <section className={selectionStyles.inputBox} aria-label="咨询输入区">
            {selectedDocuments.length > 0 && <section className={selectionStyles.panel} aria-label="已选择的引用文件" aria-live="polite">
              <div className={selectionStyles.heading}>已选择资料<span>实际引用见本轮来源</span></div>
              <div className={selectionStyles.files}>{selectedDocuments.map(d => <span className={selectionStyles.chip} key={d.id} title={d.name}>
                <span aria-hidden="true">文</span><span className={selectionStyles.name}>{d.name}</span>
                <button type="button" aria-label={`取消选择 ${d.name}`} disabled={disabled||selectionLoading} onClick={() => {void toggleDocument(d.id,false);}}>×</button>
              </span>)}</div>
            </section>}
            <textarea ref={questionInput} aria-label="咨询问题" disabled={busy} placeholder={selectedDocuments.length ? "想了解所选资料的哪些内容？" : "描述症状、询问科普，或告诉我想查询哪家医院…"} value={question} maxLength={2000} onChange={e=>editQuestion(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void send();}}}/>
          </section>
          <div><small>Enter 发送，Shift＋Enter 换行</small>{busy ? <button type="button" disabled={stopping} onClick={()=>{void stopGeneration();}}>{stopping?"正在停止…":"停止生成"}</button> : <button disabled={disabled||selectionLoading||needsRefresh||!question.trim()}>发送</button>}</div></form>
      </section><aside id="workspace-evidence" className={styles.evidence} hidden={!layout.sourcesOpen}><h3>本会话资料</h3><p className={styles.muted}>最多选择 5 份。未选择时，私有工具可检索本人全部已就绪资料。</p>{docs.filter(d=>d.status==="ready").map(d=><label className={styles.fileChoice} key={d.id}><input type="checkbox" checked={selected.includes(d.id)} disabled={disabled||selectionLoading} onChange={e=>{void toggleDocument(d.id,e.target.checked);}}/>{d.name}</label>)}<button className={styles.secondary} onClick={()=>navigate("documents")}>上传与管理资料</button><h3>本轮来源</h3>{turn?.sources?.length?turn.sources.map((s,i)=><SourceCard key={s.id} source={s} index={i}/>):<p className={styles.muted}>本轮未展示检索引用。</p>}<p className={styles.muted}>健康信息辅助。个人报告与公共医学依据分别核对。</p></aside></div>}
      {tab==="documents"&&<section><div className={styles.toolbar}><p>TXT、MD、DOCX、文本型 PDF · 每份最多 8MB · 解析后需确认</p><label className={styles.upload}>＋ 上传资料<input type="file" accept=".txt,.md,.docx,.pdf" disabled={disabled||!available} onChange={e=>{const f=e.target.files?.[0];if(f)void upload(f);e.target.value="";}}/></label><button className={styles.secondary} disabled={disabled||!available} onClick={()=>{void refreshDocs().catch(fail);}}>刷新</button></div><div className={styles.docGrid}>{docs.map(d=><article className={styles.card} key={d.id}><span className={styles.badge}>{statusNames[d.status]||d.status}</span><h3>{d.name}</h3><p className={styles.muted}>{Math.ceil(d.size/1024)} KB · {d.chunk_count} 个片段 · {time(d.created_at)}</p>{d.error_code&&<p className={styles.error}>{errorNames[d.error_code]||d.error_code}</p>}<div className={styles.actions}><button className={styles.secondary} disabled={disabled} onClick={()=>{void openDoc(d);}}>查看与确认</button>{d.status==="failed"&&<button disabled={disabled} onClick={()=>{void mutateDoc("retry",d);}}>重试</button>}<button className={styles.danger} disabled={disabled} onClick={()=>{void mutateDoc("delete",d);}}>删除</button></div></article>)}</div>{!docs.length&&<div className={styles.empty}>上传第一份资料后，它会显示在这里。</div>}{doc&&<section className={styles.editor}><div className={styles.toolbar}><h2>{doc.name}</h2><button className={styles.secondary} onClick={()=>setDoc(null)}>关闭预览</button><a href={`${API}/workspace/documents/${doc.id}/file`} target="_blank" rel="noreferrer">下载原文件</a></div><p className={styles.notice}>请核对原文件中的文字、数值、单位及参考范围。保存修改后会重新建立索引。</p>{pieces.map((p,i)=><label key={i}>{p.location}<textarea aria-label={p.location} value={p.text} onChange={e=>setPieces(pieces.map((v,j)=>j===i?{...v,text:e.target.value}:v))}/></label>)}<button disabled={disabled||!pieces.length||!["awaiting_confirmation","ready","failed"].includes(doc.status)} onClick={()=>{void mutateDoc("confirm",doc);}}>确认内容并建立索引</button></section>}</section>}
      {tab==="profile"&&<HealthMemoryPanel onError={fail} onNotice={setNotice}/>}
      {admin&&tab==="runs"&&<section><div className={styles.toolbar}><p>记录节点与工具执行情况，帮助复现和修复失败。</p>{admin&&<label><input type="checkbox" checked={adminView} onChange={e=>{setAdminView(e.target.checked);setRuns([]);setDetail(null);}}/>管理员事件视图</label>}<button disabled={disabled||!available} onClick={()=>{void loadRuns();}}>加载运行记录</button></div><div className={styles.runGrid}><div>{runs.map(r=><button className={styles.runItem} key={r.id} onClick={()=>{void loadRun(r.id);}}><span>{statusNames[r.status]||r.status}</span><small>{time(r.created_at)} · {r.duration_ms} ms</small>{r.error_code&&<code>{r.error_code}</code>}</button>)}</div><article className={styles.card}>{detail?<><h2>执行时间线</h2>{detail.events.map((e,i)=><div className={styles.event} key={i}><strong>{e.tool||e.stage}</strong><span>{e.status}{e.source_count!==undefined?` · 来源 ${e.source_count}`:""}</span><small>{e.time&&time(e.time)} {e.error_code}</small></div>)}<h3>失败分析与建议</h3>{detail.analysis.length?detail.analysis.map(a=><p key={a.error_code}><code>{a.error_code}</code> · {a.suggestion}</p>):<p className={styles.muted}>没有记录到失败错误码。运行成功不代表医学内容已经人工审核。</p>}</>:<p className={styles.muted}>选择一条运行记录。</p>}</article></div></section>}
    </main>
  </div>;
}
