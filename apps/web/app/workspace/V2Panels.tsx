"use client";
import { useCallback, useEffect, useState, useRef } from "react";
import type { FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, safeUrl, API } from "./v2-api";
import type { Subject, DocumentRow, Dashboard, Trace, Result, TaskLookup } from "./v2-api";
import s from "./v2.module.css";
import FollowupReportsPanel from "./FollowupReportsPanel";
import KnowledgeMonitorPanel from "./KnowledgeMonitorPanel";
import { answerText } from "./answer-text";
import RunDiagnosisPanel, {
  answerStatuses,
  stageLabels,
} from "./RunDiagnosisPanel";

export const diseases: Record<string, string> = {
  hypertension: "高血压",
  diabetes: "2 型糖尿病",
  dyslipidemia: "血脂异常",
  thyroid: "甲状腺疾病",
  gout: "痛风 / 高尿酸血症",
};
export function DiseaseChoices({
  value,
  onChange,
}: {
  value: string[];
  onChange: (value: string[]) => void;
}) {
  return (
    <div className={s.checks}>
      {Object.entries(diseases).map(([id, name]) => (
        <label key={id}>
          <input
            type="checkbox"
            checked={value.includes(id)}
            onChange={(e) =>
              onChange(
                e.target.checked
                  ? [...value, id]
                  : value.filter((x) => x !== id),
              )
            }
          />
          {name}
        </label>
      ))}
    </div>
  );
}

export function HealthPanel({
  subjects,
  subject,
  onChanged,
  onSelect,
  fail,
}: {
  subjects: Subject[];
  subject: Subject | null;
  onChanged: () => Promise<void>;
  onSelect: (id: string) => void;
  fail: (e: unknown) => void;
}) {
  const empty = {
    name: "",
    patient_name: "",
    relation: "本人",
    diseases: [] as string[],
    constraints: "",
    sex: "",
    birth_date: "",
    age: "",
    medical_history: "",
    allergies: "",
    other_conditions: "",
    additional_info: "",
  };
  const [draft, setDraft] = useState(empty);
  const [editing, setEditing] = useState<Subject | null>(null);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  async function saveSubject(e: FormEvent) {
    e.preventDefault();
    if (!editing) return;
    setBusy(true);
    try {
      const row = await api<Subject>(`/v2/subjects/${editing.id}`, "PUT", {
        ...draft,
        revision: editing?.revision || 1,
      });
      await onChanged();
      onSelect(row.id);
      setShow(false);
      setEditing(null);
      setDraft(empty);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function removeSubject(row: Subject) {
    if (!confirm(`删除「${row.name}」及其个人资料、记录和复诊对话？`)) return;
    setBusy(true);
    try {
      await api(`/v2/subjects/${row.id}`, "DELETE");
      if (subject?.id === row.id) onSelect("");
      await onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className={`${s.toolbar} ${s.sectionHeading}`}>
        <h2>复诊档案</h2>
        <p className={s.muted}>按患者分别管理疾病信息和历次复诊报告。</p>
      </div>
      {show && (
        <form className={`${s.card} ${s.form}`} onSubmit={saveSubject}>
          <h3>编辑复诊对象</h3>
          <p className={s.muted}>
            称呼为必填；其余资料可稍后补充，空白显示“无”，不会默认已有疾病或无过敏。
          </p>
          <section className={s.reportSection}>
            <h3>基本信息</h3>
            <div className={s.reviewGrid}>
              <label className={s.field}>
                姓名 / 称呼（必填）
                <input
                  aria-label="姓名 / 称呼"
                  required
                  maxLength={40}
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                />
              </label>
              <label className={s.field}>
                与我的关系
                <input
                  maxLength={20}
                  placeholder="无"
                  value={draft.relation}
                  onChange={(e) =>
                    setDraft({ ...draft, relation: e.target.value })
                  }
                />
              </label>
              <label className={s.field}>
                报告上的患者姓名（可选）
                <input
                  maxLength={40}
                  value={draft.patient_name}
                  placeholder="无"
                  onChange={(e) =>
                    setDraft({ ...draft, patient_name: e.target.value })
                  }
                />
              </label>
              <label className={s.field}>
                性别
                <select
                  aria-label="对象性别"
                  value={draft.sex}
                  onChange={(e) => setDraft({ ...draft, sex: e.target.value })}
                >
                  <option value="">无 / 未填写</option>
                  <option value="男">男</option>
                  <option value="女">女</option>
                  <option value="其他">其他 / 不便透露</option>
                </select>
              </label>
              <label className={s.field}>
                出生日期
                {!draft.birth_date && (
                  <span className={s.missingValue}>无</span>
                )}
                <input
                  aria-label="出生日期"
                  type="date"
                  value={draft.birth_date}
                  onChange={(e) =>
                    setDraft({ ...draft, birth_date: e.target.value })
                  }
                />
              </label>
              <label className={s.field}>
                年龄（可选）
                <input
                  placeholder="无"
                  maxLength={40}
                  value={draft.age}
                  onChange={(e) => setDraft({ ...draft, age: e.target.value })}
                />
              </label>
            </div>
          </section>
          <section className={s.reportSection}>
            <h3>健康背景</h3>
            <p className={s.muted}>
              已确诊、需要准备复诊的疾病（可不选，尚未确诊时不代选）
            </p>
            <DiseaseChoices
              value={draft.diseases}
              onChange={(value) => setDraft({ ...draft, diseases: value })}
            />
            <div className={s.reviewGrid}>
              {(
                [
                  ["other_conditions", "其他疾病 / 复诊关注事项", 2000],
                  ["medical_history", "既往病史 / 手术史", 3000],
                  ["allergies", "过敏史", 2000],
                  ["additional_info", "其他补充资料", 4000],
                ] as const
              ).map(([key, label, length]) => (
                <label className={s.field} key={key}>
                  {label}
                  <textarea
                    aria-label={label}
                    rows={3}
                    placeholder="无"
                    maxLength={length}
                    value={draft[key]}
                    onChange={(e) =>
                      setDraft({ ...draft, [key]: e.target.value })
                    }
                  />
                </label>
              ))}
            </div>
          </section>
          <label className={s.field}>
            复诊准备约束
            <textarea
              maxLength={1500}
              placeholder="例如：只整理已确认资料；不要把家人的记录混在一起。"
              value={draft.constraints}
              onChange={(e) =>
                setDraft({ ...draft, constraints: e.target.value })
              }
            />
          </label>
          <div className={s.row}>
            <button className={s.primary} disabled={busy}>
              保存
            </button>
            <button
              type="button"
              className={s.secondary}
              onClick={() => setShow(false)}
            >
              取消
            </button>
          </div>
        </form>
      )}
      <div className={s.grid}>
        {subjects.map((row) => (
          <article className={s.card} key={row.id}>
            <h3>
              {row.name}{" "}
              {row.relation && <span className={s.badge}>{row.relation}</span>}
            </h3>
            <p className={s.muted}>
              患者姓名：{row.patient_name || "无"} · 性别：{row.sex || "无"} ·
              年龄：{row.age || "无"}
            </p>
            <p>
              {row.diseases.map((d) => diseases[d]).join(" · ") ||
                "已填写病种：无"}
            </p>
            {row.other_conditions && <p>{row.other_conditions}</p>}
            <p className={s.muted}>{row.constraints || "未添加额外约束"}</p>
            <div className={s.row}>
              <button className={s.secondary} onClick={() => onSelect(row.id)}>
                {subject?.id === row.id ? "当前对象" : "选择对象"}
              </button>
              <button
                className={s.secondary}
                onClick={() => {
                  setEditing(row);
                  setDraft({
                    patient_name: row.patient_name || "",
                    name: row.name,
                    relation: row.relation,
                    diseases: row.diseases,
                    constraints: row.constraints,
                    sex: row.sex || "",
                    birth_date: row.birth_date || "",
                    age: row.age || "",
                    medical_history: row.medical_history || "",
                    allergies: row.allergies || "",
                    other_conditions: row.other_conditions || "",
                    additional_info: row.additional_info || "",
                  });
                  setShow(true);
                }}
              >
                编辑
              </button>
              <button
                className={s.danger}
                disabled={busy}
                onClick={() => void removeSubject(row)}
              >
                删除
              </button>
            </div>
          </article>
        ))}
      </div>
      {subject && (
        <FollowupReportsPanel key={subject.id} subject={subject} fail={fail} />
      )}
      {!subjects.length && !show && (
        <div className={s.empty}>
          上传报告后，点击附件上的“保存到复诊档案”，核对患者信息并确认新建对象；这里会自动显示对象和已归档报告。
        </div>
      )}
    </>
  );
}

export function DocumentsPanel({
  subject,
  publicLibrary = false,
  fail,
}: {
  subject: Subject | null;
  publicLibrary?: boolean;
  fail: (e: unknown) => void;
}) {
  const path = publicLibrary ? "/v2/admin/knowledge" : "/v2/documents";
  const blank = {
    subject: subject?.id || "",
    title: "",
    pieces: [{ location: "正文", text: "" }],
    diseases: subject?.diseases || [],
    confirmed: false,
    report_type: "inspection" as "inspection" | "followup",
    url: "",
    institution: "",
    published_at: "",
    collected_at: "",
    source_type: "article" as "article" | "directory_snapshot",
    regions: [] as string[],
    rights: "",
    revision: 1,
  };
  const [items, setItems] = useState<DocumentRow[]>([]);
  const [draft, setDraft] = useState(blank);
  const [editing, setEditing] = useState<DocumentRow | null>(null);
  type ImportTask = {id:string;filename:string;status:string;stage:string;error_code:string;document_id:string;attempts:number};
  const [importTasks, setImportTasks] = useState<ImportTask[]>([]);
  type PublishTask = {id:string;document_id:string;title:string;target_revision:number;status:string;stage:string;completed_chunks:number;total_chunks:number;attempts:number;error_code:string};
  const [publishTasks,setPublishTasks] = useState<PublishTask[]>([]);
  const [publishError,setPublishError] = useState("");
  const publicationStamp = useRef("");
  const [pendingImport, setPendingImport] = useState<string | null>(null);
  const [taskError, setTaskError] = useState("");
  const [batchUpload, setBatchUpload] = useState<{total:number;processed:number;accepted:number;taskIds:string[];failures:{name:string;reason:string}[]} | null>(null);
  useEffect(() => {
    if (!batchUpload || batchUpload.processed < batchUpload.total) return;
    const finished = batchUpload.taskIds.every(id => importTasks.some(task => task.id === id && ["completed", "failed"].includes(task.status)));
    if (finished) setBatchUpload(null);
  }, [batchUpload, importTasks]);
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [libraryFilter,setLibraryFilter]=useState("");
  const [libraryStatus,setLibraryStatus]=useState("");
  const [inspection, setInspection] = useState<{title: string; summary: string; items: {id: string; location: string; text: string; title?: string}[]} | null>(null);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    if (!publicLibrary && !subject) {
      setItems([]);
      return;
    }
    setItems(
      (
        await api<{ items: DocumentRow[] }>(
          path + (publicLibrary ? "" : `?subject=${subject!.id}`),
        )
      ).items.filter((row) => publicLibrary || row.report_type !== "followup"),
    );
  }, [publicLibrary, subject, path]);
  useEffect(() => {
    void refresh().catch(fail);
    setShow(false);
  }, [refresh, fail]);
  useEffect(() => {
    if (!publicLibrary) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await api<{items:ImportTask[]}>("/v2/admin/knowledge-imports");
        if (disposed) return;
        setImportTasks(result.items);
        setTaskError("");
        const pending = result.items.find(task=>task.id===pendingImport);
        if (pending?.status==="completed") {
          const documents = await api<{items:DocumentRow[]}>(path);
          if (disposed) return;
          setItems(documents.items);
          const row = documents.items.find(doc=>doc.id===pending.document_id);
          if (row && row.status === "draft") {
            setDraft({subject:"",title:row.title,pieces:row.pieces,diseases:row.diseases || [],
              confirmed:false,report_type:"inspection",url:row.url || "",institution:row.institution || "",
              published_at:row.published_at || "",collected_at:row.collected_at || "",source_type:row.source_type || "article",
              regions:row.regions || [],rights:row.rights || "",revision:row.revision});
            setEditing(row); setShow(true);
          }
          setPendingImport(null);
          setNotice(row?.status === "draft" ? "原件已留存并创建草稿，请核对正文、补充来源信息后保存。草稿尚未参与问答。" : row ? "该文件已入库，已刷新下方资料列表。" : "对应资料已删除，可重新上传文件。");
        } else if (pending?.status === "failed") {
          setPendingImport(null);
          setNotice("入库处理失败，请查看任务原因；修复资料后重新上传，或重试临时失败任务。");
        }
      } catch {
        if (!disposed) setTaskError("任务状态暂时无法获取，将自动重试；请勿重复上传。");
      } finally {
        if (!disposed) timer = setTimeout(poll,1500);
      }
    }
    void poll();
    return ()=>{disposed=true;clearTimeout(timer);};
  }, [publicLibrary,pendingImport,path]);
  useEffect(()=>{
    if (!publicLibrary) return;
    let disposed=false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll(){
      try {
        const result=await api<{items:PublishTask[]}>("/v2/admin/knowledge-publications");
        if(disposed) return;
        setPublishTasks(result.items);setPublishError("");
        const stamp=result.items.filter(task=>task.status==="completed").map(task=>task.id).sort().join(",");
        if(stamp!==publicationStamp.current){
          await refresh();
          publicationStamp.current=stamp;
        }
      } catch {if(!disposed)setPublishError("发布任务状态暂时无法获取，将自动重试。");}
      finally {if(!disposed)timer=setTimeout(poll,1500);}
    }
    void poll();
    return ()=>{disposed=true;clearTimeout(timer);};
  },[publicLibrary,refresh]);
  async function retryPublish(task: PublishTask){
    setBusy(true);
    try {
      const result=await api<PublishTask>(`/v2/admin/knowledge-publications/${task.id}/retry`,"POST");
      setPublishTasks(tasks=>tasks.map(item=>item.id===result.id?result:item));
    } catch(error){fail(error);} finally {setBusy(false);}
  }
  async function retryImport(task: ImportTask) {
    setBusy(true);
    try {
      const row = await api<ImportTask>(`/v2/admin/knowledge-imports/${task.id}/retry`,"POST");
      setImportTasks(tasks=>tasks.map(item=>item.id===row.id ? row : item));
      setPendingImport(row.id);
    } catch(e) { fail(e); } finally { setBusy(false); }
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api(
        editing ? `${path}/${editing.id}` : path,
        editing ? "PUT" : "POST",
        { ...draft, subject: publicLibrary ? "" : subject!.id },
      );
      await refresh();
      setShow(false);
      setInspection(null);
      setNotice(publicLibrary ? "草稿已保存；核对来源后可审核发布。" : "资料已保存。");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function parse(file: File) {
    setBusy(true);
    try {
      const body = new FormData();
      body.append("file", file);
      if (publicLibrary) {
        const task = await api<ImportTask>("/v2/admin/knowledge-imports","POST",body);
        setPendingImport(task.id);
        setImportTasks(tasks=>[task,...tasks.filter(item=>item.id!==task.id)]);
        setNotice("文件已提交，后台正在处理；可以离开页面，稍后在入库任务中查看结果。");
        return;
      }
      const parsed = await api<{
        title: string;
        pieces: { location: string; text: string }[];
        id?: string;
        revision?: number;
        collected_at?: string;
      }>(publicLibrary ? `${path}/import` : "/v2/parse", "POST", body);
      setDraft({ ...blank, title: parsed.title, pieces: parsed.pieces,
        revision: parsed.revision || 1, collected_at: parsed.collected_at || "" });
      setEditing(publicLibrary ? parsed as DocumentRow : null);
      if (publicLibrary) {
        await refresh();
        setNotice("原件已留存并创建草稿，请核对正文、补充来源信息后保存。草稿尚未参与问答。");
      }
      setShow(true);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function uploadPublicFiles(files: File[]) {
    if (!files.length) return;
    if (files.length>20) {
      fail(new Error("每批最多选择20个文件，请分批上传。"));
      return;
    }
    setBusy(true);
    setPendingImport(null);
    const summary = {total:files.length,processed:0,accepted:0,taskIds:[] as string[],failures:[] as {name:string;reason:string}[]};
    setBatchUpload({...summary});
    let singleTask: ImportTask | null = null;
    try {
      // Bound network concurrency; one rejected file never aborts the remaining files.
      for (const file of files) {
        try {
          if (!file.size || file.size>8*1024*1024) throw new Error("文件必须非空且不超过8MB");
          const form = new FormData();
          form.append("file",file);
          const task = await api<ImportTask>("/v2/admin/knowledge-imports","POST",form);
          summary.accepted++;
          summary.taskIds.push(task.id);
          singleTask = task;
          setImportTasks(tasks=>[task,...tasks.filter(item=>item.id!==task.id)]);
        } catch(error) {
          summary.failures.push({name:file.name,reason:error instanceof Error ? error.message : "提交失败，请重新选择此文件"});
        }
        summary.processed++;
        setBatchUpload({...summary,taskIds:[...summary.taskIds],failures:[...summary.failures]});
      }
      if (files.length===1 && singleTask) setPendingImport(singleTask.id);
      setNotice(`已提交或复用 ${summary.accepted} 个文件的入库任务；处理结果请查看下方任务列表。`);
    } finally { setBusy(false); }
  }
  async function action(row: DocumentRow, operation: string) {
    setBusy(true);
    try {
      if (operation === "delete") {
        if (!confirm(`删除「${row.title}」？`)) return;
        await api(`${path}/${row.id}`, "DELETE");
      } else if(publicLibrary && operation === "publish") {
        const task=await api<PublishTask>(`${path}/${row.id}/publish-task`,"POST",{revision:row.revision});
        setPublishTasks(tasks=>[task,...tasks.filter(item=>item.id!==task.id)]);
        setNotice(task.status==="failed" ? "该版本已有失败的发布任务，请在发布任务列表查看原因并重试。" : "审核发布任务已提交，可在发布任务列表查看进度；完成后资料才会参与检索。");
        return;
      } else
        await api(`${path}/${row.id}/${operation}`, "POST", {
          revision: row.revision,
        });
      await refresh();
      setInspection(null);
      setNotice(operation === "publish" ? "资料已发布，可使用本地试检索检查召回结果。" : operation === "withdraw" ? "资料已撤回，不再参与新的公共知识检索。" : "资料已删除。");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function inspect(row: DocumentRow) {
    setBusy(true);
    try {
      const result = await api<{total:number; vector_chunks:number; items:{id:string; location:string; text:string}[]}>(`${path}/${row.id}/chunks`);
      setInspection({title:row.title,summary:`共 ${result.total} 个片段，${result.vector_chunks} 个包含向量；${row.status === "published" ? "已发布" : "未发布，不参与问答"}`,items:result.items});
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function searchLocal(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const result = await api<{method:string; items:{id:string; title:string; location:string; text:string}[]}>(`${path}/search`,"POST",{query});
      const methods: Record<string,string> = {bm25:"关键词检索",hybrid:"关键词与向量融合检索",bm25_fallback:"向量服务不可用，已使用关键词检索"};
      setInspection({title:"本地试检索结果",summary:`${methods[result.method] || result.method} · 仅检索已发布资料 · 未调用外部搜索`,items:result.items});
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  if (!publicLibrary && !subject)
    return <div className={s.empty}>请先到复诊档案中创建并选择复诊对象。</div>;
  const documentForm = show && (
    <form className={`${s.card} ${s.form} ${s.reportForm}`} onSubmit={save}>
      <h3>
        {publicLibrary
          ? editing
            ? "编辑资料"
            : "核对并录入资料"
          : `${editing ? "编辑" : "核对并录入"}${draft.report_type === "followup" ? "复诊报告" : "检查报告"}`}
      </h3>
      {publicLibrary && <p className={s.muted}>核对正文 → 补充来源 → 保存草稿 → 审核发布。编辑已发布资料后，需要重新发布。</p>}
      <label className={s.field}>
        资料名称
        <input
          required
          maxLength={160}
          value={draft.title}
          onChange={(e) => setDraft({ ...draft, title: e.target.value })}
        />
      </label>
      {publicLibrary && <p className={s.muted}>原文共 {draft.pieces.length} 个片段，区域内约显示3个窗口，其余向下滚动查看和编辑。</p>}
      <div className={publicLibrary ? s.editorPieces : undefined} role={publicLibrary ? "region" : undefined} aria-label={publicLibrary ? "原文片段编辑区域" : undefined} tabIndex={publicLibrary ? 0 : undefined}>
      {draft.pieces.map((p, i) => (
        <label className={s.field} key={i}>
          {p.location}
          <textarea
            required
            maxLength={40000}
            value={p.text}
            onChange={(e) =>
              setDraft({
                ...draft,
                pieces: draft.pieces.map((x, j) =>
                  i === j ? { ...x, text: e.target.value } : x,
                ),
              })
            }
          />
        </label>
      ))}
      </div>
      {publicLibrary ? (
        <>
          <label className={s.field}>
            原始来源链接
            <input
              type="url"
              placeholder="https://..."
              value={draft.url}
              onChange={(e) => setDraft({ ...draft, url: e.target.value })}
            />
          </label>
          <div className={s.row}>
            <label className={s.field}>
              发布机构
              <input
                value={draft.institution}
                maxLength={200}
                onChange={(e) =>
                  setDraft({ ...draft, institution: e.target.value })
                }
              />
            </label>
            <label className={s.field}>
              发布日期
              <input
                type="date"
                value={draft.published_at}
                onChange={(e) =>
                  setDraft({ ...draft, published_at: e.target.value })
                }
              />
            </label>
          </div>
          <label className={s.field}>
            使用授权 / 许可依据
            <input
              maxLength={400}
              value={draft.rights}
              onChange={(e) => setDraft({ ...draft, rights: e.target.value })}
            />
          </label>
          <div className={s.row}>
            <label className={s.field}>资料类型
              <select value={draft.source_type} onChange={e=>setDraft({...draft,source_type:e.target.value as "article" | "directory_snapshot"})}>
                <option value="article">文章 / 指南</option><option value="directory_snapshot">动态名录快照</option>
              </select>
            </label>
            <label className={s.field}>采集日期
              <input type="date" value={draft.collected_at} onChange={e=>setDraft({...draft,collected_at:e.target.value})}/>
            </label>
          </div>
          <label className={s.field}>适用地区（以顿号分隔，可留空）
            <input maxLength={400} value={draft.regions.join("、")} onChange={e=>setDraft({...draft,regions:e.target.value.split(/[、,，]/).map(x=>x.trim()).filter(Boolean)})}/>
          </label>
          <p>适用病种（可选，不选为通用知识）</p>
          <DiseaseChoices
            value={draft.diseases}
            onChange={(value) => setDraft({ ...draft, diseases: value })}
          />
        </>
      ) : (
        <label className={s.row}>
          <input
            type="checkbox"
            checked={draft.confirmed}
            onChange={(e) =>
              setDraft({ ...draft, confirmed: e.target.checked })
            }
          />
          我已核对正文、日期、数值和单位，允许在我主动请求复诊整理时引用。
        </label>
      )}
      <div className={s.row} style={{ marginTop: 20 }}>
        <button className={s.primary} disabled={busy}>
          {busy ? "处理中…" : publicLibrary ? "保存草稿" : "保存资料"}
        </button>
        <button
          type="button"
          className={s.secondary}
          onClick={() => setShow(false)}
        >
          取消
        </button>
      </div>
    </form>
  );
  const visibleDocuments=publicLibrary ? items.filter(row=>(!libraryStatus || row.status===libraryStatus) && (!libraryFilter.trim() || [row.title,row.institution,row.original_filename,...(row.regions || [])].join(" ").toLowerCase().includes(libraryFilter.trim().toLowerCase()))) : items;
  return (
    <div className={publicLibrary ? s.libraryWorkspace : undefined}>
      <div className={`${s.toolbar} ${s.sectionHeading}`}>
        <h2>{publicLibrary ? "公共知识库" : "个人检查报告"}</h2>
        <p className={s.muted}>
          {publicLibrary
            ? "资料先保存草稿，核实来源与授权后发布；编辑会撤回发布状态。"
            : "上传、核对并保存个人检查结果，便于日后查阅和按需参考。"}
        </p>
        <label className={`${s.secondary} ${s.file}`}>
            {publicLibrary ? "批量上传文件" : "上传检查报告"}
          <input
            disabled={busy}
            type="file"
              multiple={publicLibrary}
            accept={publicLibrary ? ".txt,.md,.docx,.pdf" : ".txt,.md,.docx,.pdf,.png,.jpg,.jpeg,.bmp"}
            onChange={(e) => {
                const files = Array.from(e.target.files || []);
                if (publicLibrary) void uploadPublicFiles(files);
                else if (files[0]) void parse(files[0]);
              e.target.value = "";
            }}
          />
        </label>
        {publicLibrary && (
          <button
            className={s.primary}
            disabled={busy}
            onClick={() => {
              setDraft({
                ...blank,
                report_type: publicLibrary ? "inspection" : "followup",
              });
              setEditing(null);
              setShow(true);
            }}
          >
            录入正文
          </button>
        )}
      </div>
      {publicLibrary && <>
        <div className={s.libraryStats} aria-label="公共资料概况">
          {([['','全部资料'],['published','已发布'],['draft','待核对草稿'],['withdrawn','已撤回']] as const).map(([status,label])=><button key={status} type="button" aria-pressed={libraryStatus===status} className={s.libraryStat} onClick={()=>setLibraryStatus(status)}><span>{label}</span><strong>{status ? items.filter(row=>row.status===status).length : items.length}</strong><small>{status==='published'?'参与公共问答':status==='draft'?'核对后审核发布':status==='withdrawn'?'暂不参与检索':'当前已加载资料'}</small></button>)}
        </div>
        <p className={s.muted}>支持一次选择多个 TXT、MD、DOCX 或文本 PDF，每批最多20个，每个文件最大8MB。按住 Ctrl / Shift 可多选；每个文件独立入库，扫描件请先转换为可提取文字的文件。</p>
        <p role="status" aria-live="polite">{busy ? "正在处理，请稍候…" : notice}</p>
        {batchUpload && <section className={s.card} aria-label="批量上传结果">
          <p role="status">已检查 {batchUpload.processed} / {batchUpload.total} 个文件 · 已提交或复用 {batchUpload.accepted} 个任务 · 提交失败 {batchUpload.failures.length} 个</p>
          {batchUpload.failures.map((item,index)=><p key={`${item.name}-${index}`}>{item.name}：{item.reason}</p>)}
          {batchUpload.failures.length>0 && <p className={s.muted}>其他文件仍会继续处理。可单独重新选择提交失败的文件；相同文件不会重复创建资料。</p>}
        </section>}
        {(importTasks.length>0 || taskError) && <section className={s.card} aria-label="公共知识入库任务">
          <h3>最近入库任务</h3><p className={s.muted}>提交 → 排队 → 解析 → 保存草稿。资料仍需核对和审核发布。</p>
          {taskError && <p role="status">{taskError}</p>}
            <div className={s.importTaskScroll} role="region" aria-label="入库历史文件列表" tabIndex={0}>
            {importTasks.map(task=><div className={s.importTask} key={task.id}>
            <div><strong>{task.filename}</strong><p className={s.muted}>
              {({queued:"等待处理",parsing:"正在解析",saving:"正在保存草稿",completed:"已完成",failed:"处理失败"} as Record<string,string>)[task.stage] || task.stage} · 已尝试 {task.attempts} / 3 次
              {task.error_code && ` · ${({worker_interrupted:"服务重启中断，可重试",utf8_required:"请转换为 UTF-8 文件",ocr_required:"扫描件需先转换为文本",encrypted_pdf:"请解除 PDF 密码",parse_failed:"文件解析失败，请检查完整性",document_limits:"正文超出限制，请拆分",empty_document:"文件没有可用正文",import_failed:"暂时处理失败，可重试"} as Record<string,string>)[task.error_code] || "文件超出处理限制或解析失败，请检查后重新上传"}`}
            </p></div>
            {task.status === "failed" && task.attempts<3 && <button className={s.secondary} disabled={busy} onClick={()=>void retryImport(task)}>重试任务</button>}
            {task.status === "completed" && <button className={s.secondary} disabled={busy} onClick={()=>{setPendingImport(task.id);}}>查看入库资料</button>}
          </div>)}
            </div>
        </section>}
        {documentForm}
        <KnowledgeMonitorPanel documents={items} onChanged={refresh}/>
          {(publishTasks.length>0 || publishError) && <section className={s.card} aria-label="公共知识发布任务">
            <h3>最近发布任务</h3><p className={s.muted}>审核提交 → 分块 → 向量化（已配置时）→ 发布。处理失败不会发布半成品。</p>
            {publishError && <p role="status">{publishError}</p>}
            <div className={s.importTaskScroll} role="region" aria-label="发布历史任务列表" tabIndex={0}>
              {publishTasks.map(task=><div className={s.importTask} key={task.id}>
                <div><strong>{task.title}</strong><p className={s.muted}>
                  版本 {task.target_revision} · {({queued:"排队中",chunking:"正在分块",embedding:"正在生成向量",saving:"正在保存索引",completed:"已发布",failed:"发布失败"} as Record<string,string>)[task.stage] || task.stage}
                  {task.total_chunks>0 && ` · ${task.completed_chunks} / ${task.total_chunks} 个片段`} · 已尝试 {task.attempts} / 3 次
                  {task.error_code && ` · ${({worker_interrupted:"服务重启中断，可重试",publication_stale:"资料已编辑、撤回或删除，请重新审核最新版本",publication_timeout:"处理超时，可重试",publication_failed:"发布处理失败，请检查向量服务或稍后重试"} as Record<string,string>)[task.error_code] || "发布未完成"}`}
                </p></div>
                {task.status==="failed" && task.attempts<3 && task.error_code!=="publication_stale" && <button className={s.secondary} disabled={busy} onClick={()=>void retryPublish(task)}>重试发布</button>}
              </div>)}
            </div>
          </section>}
        <form className={`${s.card} ${s.form} ${s.knowledgeSearch}`} onSubmit={searchLocal}>
          <label className={s.field}>本地知识试检索<input required maxLength={1000} value={query} onChange={e=>setQuery(e.target.value)} placeholder="输入问题，检查已发布资料是否能被找到"/></label>
          <button className={s.secondary} disabled={busy || !query.trim()}>试检索</button>
        </form>
        {inspection && <section className={s.card} aria-label="知识片段预览">
          <h3>{inspection.title}</h3><p className={s.muted}>{inspection.summary}</p>
          {!inspection.items.length && <p>未找到相关片段，请检查资料是否发布或调整查询。</p>}
          {inspection.items.map(item=><details key={item.id}><summary>{item.title ? `${item.title} · ` : ""}{item.location}</summary><p style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{item.text}</p></details>)}
          <button type="button" className={s.secondary} onClick={()=>setInspection(null)}>关闭预览</button>
        </section>}
      </>}
      {!publicLibrary && (
        <p className={`${s.muted} ${s.uploadHelp}`}>
          支持 TXT、MD、DOCX、文本 PDF，最大8MB；图片与扫描件需启用OCR。
        </p>
      )}
      {publicLibrary && <section className={s.libraryListHeader} aria-label="资料列表筛选">
        <div><h3>资料目录</h3><p className={s.muted}>当前显示 {visibleDocuments.length} / {items.length} 份资料</p></div>
        <label className={s.field}>搜索资料<input value={libraryFilter} onChange={event=>setLibraryFilter(event.target.value)} placeholder="资料名称、机构、原文件或地区"/></label>
        <label className={s.field}>发布状态<select aria-label="发布状态" value={libraryStatus} onChange={event=>setLibraryStatus(event.target.value)}><option value="">全部状态</option><option value="published">已发布</option><option value="draft">草稿</option><option value="withdrawn">已撤回</option></select></label>
      </section>}
      <div className={`${s.grid} ${publicLibrary ? s.knowledgeGrid : s.reportList}`}>
        {visibleDocuments.map((row) => (
          <article className={`${s.card} ${publicLibrary ? s.knowledgeCard : ""}`} key={row.id}>
            {!publicLibrary && (
              <span className={s.badge} style={{ marginRight: 8 }}>
                {row.report_type === "followup"
                  ? "复诊报告"
                  : row.report_type === "inspection"
                    ? "检查报告"
                    : "未分类报告"}
              </span>
            )}
            <span className={s.badge}>
              {publicLibrary
                ? { draft: "草稿", published: "已发布", withdrawn: "已撤回" }[
                    row.status || ""
                  ] || row.status
                : row.confirmed
                  ? "已核对"
                  : "待核对"}
            </span>
            <h3 style={publicLibrary ? undefined : { marginTop: 12 }}>{row.title}</h3>
            <p className={s.muted}>
              版本 {row.revision} · {row.pieces.length} 个原文位置
            </p>
            {publicLibrary && (
              <p className={s.muted}>
                {row.institution || "机构待补充"} ·{" "}
                {row.published_at ||
                  (row.collected_at
                    ? `采集快照：${row.collected_at}`
                    : "日期待补充")}
              </p>
            )}
            {publicLibrary && <p className={s.muted}>{row.index_summary ? `${row.index_summary.chunks} 个片段 · ${row.index_summary.vector_chunks} 个向量片段` : "发布后可查看索引信息"}{row.original_filename ? ` · 原件：${row.original_filename}` : ""}</p>}
            <div className={publicLibrary ? s.knowledgeActions : s.row}>
              {publicLibrary && <button className={s.secondary} disabled={busy} onClick={()=>void inspect(row)}>预览分块</button>}
              <button
                className={s.secondary}
                onClick={() => {
                  setEditing(row);
                  setDraft({
                    subject: row.subject,
                    title: row.title,
                    pieces: row.pieces,
                    diseases: row.diseases,
                    confirmed: row.confirmed,
                    report_type: row.report_type || "inspection",
                    url: row.url,
                    institution: row.institution,
                    published_at: row.published_at,
                    collected_at: row.collected_at || "",
                    source_type: row.source_type || "article",
                    regions: row.regions || [],
                    rights: row.rights,
                    revision: row.revision,
                  });
                  setShow(true);
                }}
              >
                查看 / 编辑
              </button>
              {publicLibrary && (
                <button
                  className={s.secondary}
                  disabled={busy || (row.status!=="published" && publishTasks.some(task=>task.document_id===row.id && task.target_revision===row.revision && ["queued","processing"].includes(task.status)))}
                  onClick={() =>
                    void action(
                      row,
                      row.status === "published" ? "withdraw" : "publish",
                    )
                  }
                >
                  {row.status === "published" ? "撤回" : "审核发布"}
                </button>
              )}
              <button
                className={s.danger}
                disabled={busy}
                onClick={() => void action(row, "delete")}
              >
                删除
              </button>
              {publicLibrary && row.original_filename && <a className={s.secondary} href={`${API}${path}/${row.id}/original`}>下载原件</a>}
            </div>
          </article>
        ))}
      </div>
      {!publicLibrary && documentForm}
      {publicLibrary && items.length>0 && !visibleDocuments.length && <p className={s.empty}>没有符合筛选条件的资料，请调整关键词或发布状态。</p>}
      {!items.length && (
        <div className={s.empty}>
          {publicLibrary
            ? "尚无公共资料。先录入一小批核实过来源的真实资料，再验证检索和引用。"
            : "还没有检查报告。请上传文件，核对识别内容后保存。各类报告请从对话输入框左下角“＋”上传。"}
        </div>
      )}
    </div>
  );
}

export function ResultCard({
  result,
  onExport,
}: {
  result: Result;
  onExport?: () => void;
}) {
  const verifiedCitations=["passed","repaired"].includes(result.citation_review?.status || "") ? result.citations || [] : [];
  if (result.kind === "chat")
    return (
      <div className={s.chatReply}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          skipHtml
          components={{
            img: () => null,
            a: ({ href, children }) =>
              safeUrl(href || "") ? (
                <a href={safeUrl(href || "")} target="_blank" rel="noreferrer">
                  {children}
                </a>
              ) : (
                <span>{children}</span>
              ),
          }}
        >
          {(result.content || "").replace(/\[(S\d+)\](?!\()/g,(marker,label:string)=>{
            const citation=verifiedCitations.find(item=>item.label===label);
            const url=citation ? safeUrl(citation.url) : "";
            return url ? `[${label}](${url.replace(/\(/g,"%28").replace(/\)/g,"%29")})` : citation || !result.citation_review ? marker : "";
          })}
        </ReactMarkdown>
        {!!verifiedCitations.length && <details>
          <summary>查看引用原文（{verifiedCitations.length}条）</summary>
          {verifiedCitations.map((citation,index)=><blockquote key={`${citation.label}-${index}`}>
            <p><strong>{citation.label} · {citation.title}</strong>{citation.location ? ` · ${citation.location}` : ""}</p>
            <p>{citation.quote}</p>
          </blockquote>)}
        </details>}
      </div>
    );
  const checklist = result.preparation_checklist || [];
  if (checklist.length) return (
    <section className={s.result}>
      {onExport && <button className={s.secondary} onClick={onExport}>导出摘要</button>}
      {result.stale && <p className={s.notice}>生成后档案或资料已更新，请重新整理以使用最新资料。</p>}
      <div className={s.chatReply}><ReactMarkdown skipHtml>{answerText(result)}</ReactMarkdown></div>
    </section>
  );
  const questionsTitle = checklist.length
    ? "复诊时重点问医生"
    : "可进一步确认的问题";
  const nextSteps = (
    <>
      {result.gaps.length > 0 && (
        <section aria-label="仍需补充或确认">
          <h3>仍需补充或确认</h3>
          <ul>
            {result.gaps.map((gap, index) => (
              <li key={index}>{gap}</li>
            ))}
          </ul>
        </section>
      )}
      {result.questions.length > 0 && (
        <section aria-label={questionsTitle}>
          <h3>{questionsTitle}</h3>
          <ul>
            {result.questions.map((question, index) => (
              <li key={index}>{question}</li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
  return (
    <section className={s.result}>
      {result.material_notice && <p className={s.notice} role="status">{result.material_notice}</p>}
      <div className={s.row}>
        <h2>{checklist.length ? "复诊准备摘要" : "检查资料整理"}</h2>
        {onExport && (
          <button className={s.secondary} onClick={onExport}>
            导出摘要
          </button>
        )}
      </div>
      {result.stale && (
        <p className={s.notice}>
          生成后档案或资料已更新。这份摘要保留生成时的记录，请重新整理以使用最新资料。
        </p>
      )}
      {checklist.length > 0 && (
        <section className={s.preparation} aria-label="复诊前准备清单">
          <h3>复诊前准备清单</h3>
          <p className={s.muted}>{result.preparation_scope_note}</p>
          <ol className={s.preparationList}>
            {checklist.map((item) => (
              <li key={item.id}>
                <div className={s.preparationHeading}>
                  <strong>{item.title}</strong>
                  <span
                    className={s.preparationStatus}
                    data-status={item.status}
                  >
                    {
                      {
                        provided: "本轮已提供",
                        to_confirm: "待确认",
                        reminder: "准备提醒",
                      }[item.status]
                    }
                  </span>
                </div>
                <p>{item.action}</p>
              </li>
            ))}
          </ol>
        </section>
      )}
      {checklist.length > 0 && nextSteps}
      {result.records.length > 0 && (
        <>
          <h3>已确认记录 · 按项目和日期排列</h3>
          <div className={s.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>日期</th>
                  <th>项目</th>
                  <th>内容 / 数值</th>
                  <th>单位</th>
                </tr>
              </thead>
              <tbody>
                {result.records.map((r) => (
                  <tr key={r.id}>
                    <td>{r.date}</td>
                    <td>{r.label}</td>
                    <td>{r.value}</td>
                    <td>{r.unit || "未填写"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {!!result.comparisons?.length && (
        <>
          <h3>同项目、同单位的数值对比</h3>
          {result.comparisons.map((c, i) => (
            <p key={i}>
              {c.label}：{c.previous_date} 的 {c.previous_value} →{" "}
              {c.current_date} 的 {c.current_value} {c.unit}，差值 {c.delta}{" "}
              {c.unit}。
            </p>
          ))}
          <p className={s.muted}>
            差值仅表示数值变化；不据此判断改善、恶化或调整用药。
          </p>
        </>
      )}
      {result.claims.length > 0 && (
        <>
          <h3>有来源的整理结论</h3>
          <ul>
            {result.claims.map((c, i) => (
              <li key={i}>{c.text}</li>
            ))}
          </ul>
        </>
      )}
      {!checklist.length && nextSteps}
      {result.service_information && (
        <>
          <h3>医院与就医信息</h3>
          <p className={s.reportText}>{result.service_information}</p>
        </>
      )}
    </section>
  );
}

const purposes: Record<string, string> = {
  report_extraction: "报告关键字段提取",
  summary_completion: "补齐指定资料回答",
  context_validation: "长对话约束验收",
  hospital_service: "医院服务咨询",
  evidence_reranking: "候选证据重排",
  task_planning: "任务规划",
  query_embedding: "查询向量化",
  document_embedding: "资料向量化",
  retrieval_critique: "检索反思",
  summary_generation: "摘要生成",
  claim_verification: "证据核验",
  daily_chat: "日常对话",
  intent_analysis: "用户意图分析",
  medical_education: "医药知识科普",
  ocr_recognition: "OCR文字识别",
  asr_recognition: "语音识别",
};
const states: Record<string, string> = {
  running: "运行中",
  ok: "成功",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
export function AdminPanel({
  fail,
  onReturnChat,
}: {
  fail: (e: unknown) => void;
  onReturnChat: () => void;
}) {
  const [page, setPage] = useState("dashboard");
  const [data, setData] = useState<Dashboard | null>(null);
  const [auto, setAuto] = useState(true);
  const [full, setFull] = useState(false);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [taskId,setTaskId]=useState("");
  const [taskKind,setTaskKind]=useState("auto");
  const [taskDetail,setTaskDetail]=useState<TaskLookup|null>(null);
  const [lookupBusy,setLookupBusy]=useState(false);
  const [lookupError,setLookupError]=useState("");
  const taskLookupPanel=useRef<HTMLElement|null>(null);
  const [recovering, setRecovering] = useState(false);
  const [filter, setFilter] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const refresh = useCallback(
    async () => setData(await api<Dashboard>("/v2/admin/dashboard")),
    [],
  );
  async function lookupTask(identifier=taskId,kind=taskKind){
    setLookupBusy(true);setLookupError("");setTaskDetail(null);
    setTaskId(identifier.trim());setTaskKind(kind);
    try{setTaskDetail(await api<TaskLookup>(`/v2/admin/tasks/${encodeURIComponent(identifier.trim())}?kind=${kind}`));}
    catch(error){setLookupError(error instanceof Error?error.message:"任务查询失败");}
    finally{setLookupBusy(false);taskLookupPanel.current?.scrollIntoView({block:"start"});}
  }
  async function retryLocatedTask(){
    if(!taskDetail || taskDetail.task.type==="run")return;
    const task=taskDetail.task;
    setLookupBusy(true);setLookupError("");
    try{
      await api(`/v2/admin/${task.type==="import"?"knowledge-imports":task.type==="inspection"?"knowledge-inspections":"knowledge-publications"}/${task.id}/retry`,"POST");
      await lookupTask(task.id,task.type);await refresh();
    }catch(error){setLookupError(error instanceof Error?error.message:"重试失败");}
    finally{setLookupBusy(false);}
  }
  useEffect(() => {
    if (!trace?.run_id || trace.diagnosis?.outcome !== "running") return;
    let alive = true;
    const identifier = trace.run_id;
    const timer = setInterval(() => {
      void api<Trace>(`/v2/admin/traces/${identifier}`)
        .then((value) => {
          if (alive) setTrace(value);
        })
        .catch(fail);
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [trace?.run_id, trace?.diagnosis?.outcome, fail]);
  useEffect(() => {
    void refresh().catch(fail);
    if (!auto || page !== "dashboard") return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void refresh().catch(fail);
    }, 5000);
    return () => clearInterval(timer);
  }, [auto, page, refresh, fail]);
  const list =
    data?.calls.filter(
      (c) =>
        (!filter ||
          [c.agent, c.model, c.purpose, c.run_id, purposes[c.purpose]]
            .join(" ")
            .toLowerCase()
            .includes(filter.toLowerCase())) &&
        (!stateFilter || c.status === stateFilter),
    ) || [];
  return (
    <div
      className={`${page === "dashboard" ? s.adminDashboard : ""} ${full ? s.full : ""}`}
    >
      {full && (
        <button
          className={`${s.iconButton} ${s.backToChat}`}
          onClick={onReturnChat}
        >
          <span aria-hidden="true">‹</span>返回对话
        </button>
      )}
      <div className={s.toolbar}>
        <h2>后台管理</h2>
        <div className={s.row}>
          <button
            className={page === "dashboard" ? s.primary : s.secondary}
            onClick={() => setPage("dashboard")}
          >
            运行观测
          </button>
          <button
            className={page === "knowledge" ? s.primary : s.secondary}
            onClick={() => setPage("knowledge")}
          >
            公共知识库
          </button>
        </div>
        <span className={s.spacer} />
        <button className={s.secondary} onClick={() => setFull(!full)}>
          {full ? "退出大屏" : "大屏展示"}
        </button>
      </div>
      {page === "knowledge" ? (
        <DocumentsPanel publicLibrary subject={null} fail={fail} />
      ) : (
        <>
          <div className={s.toolbar}>
            <span className={s.muted}>
              {data
                ? `${data.window} · 更新于 ${new Date(data.updated_at).toLocaleTimeString("zh-CN")}`
                : "正在加载观测数据…"}
            </span>
            <label>
              <input
                type="checkbox"
                checked={auto}
                onChange={(e) => setAuto(e.target.checked)}
              />
              每 5 秒刷新
            </label>
            <button
              className={s.secondary}
              onClick={() => void refresh().catch(fail)}
            >
              刷新
            </button>
          </div>
          {data && (
            <>
              <div className={s.stats}>
                {[
                  ["实际模型请求", data.metrics.calls],
                  [
                    "调用成功率",
                    data.metrics.success_rate === null
                      ? "—"
                      : `${(data.metrics.success_rate * 100).toFixed(1)}%`,
                  ],
                  ["已知 Token", data.metrics.known_tokens.toLocaleString()],
                  [
                    "P50 耗时",
                    data.metrics.p50_ms === null
                      ? "—"
                      : `${data.metrics.p50_ms} ms`,
                  ],
                  [
                    "P95 耗时",
                    data.metrics.p95_ms === null
                      ? "—"
                      : `${data.metrics.p95_ms} ms`,
                  ],
                ].map(([label, value]) => (
                  <div className={s.stat} key={label}>
                    <span>{label}</span>
                    <strong>{value}</strong>
                  </div>
                ))}
              </div>
              <p className={s.muted}>
                有 {data.metrics.unknown_usage}{" "}
                次调用未返回实际用量。未知用量不按 0
                计入；演示模式不产生模型调用记录。
              </p>
              <div className={s.observationCharts}>
                <article className={s.card}>
                  <h3>每小时调用量</h3>
                  {data.trend.length ? (
                    <>
                      <div
                        className={s.chart}
                        role="img"
                        aria-label={data.trend
                          .map((x) => `${x.hour}时：${x.count}次`)
                          .join("，")}
                      >
                        {data.trend.slice(-12).map((x) => (
                          <div
                            className={s.bar}
                            key={x.hour}
                            title={`${x.hour}: ${x.count}次`}
                            style={{
                              height: `${Math.max(4, (x.count / Math.max(...data.trend.map((v) => v.count))) * 110)}px`,
                            }}
                          >
                            <small>{x.count}</small>
                          </div>
                        ))}
                      </div>
                      <p className={s.barTitle}>
                        最近有调用的 {Math.min(data.trend.length, 12)}{" "}
                        个小时（UTC）
                      </p>
                    </>
                  ) : (
                    <p className={s.muted}>暂无真实模型调用。</p>
                  )}
                </article>
                <article className={s.card}>
                  <h3>Agent 调用分布</h3>
                  <div
                    className={s.distributionScroll}
                    role="region"
                    aria-label="Agent 调用分布列表"
                    tabIndex={0}
                  >
                    {Object.entries(data.agents).sort((a,b)=>b[1]-a[1]).map(([name, count]) => (
                      <p key={name}>
                        {name}
                        <strong style={{ float: "right" }}>{count}</strong>
                        <progress className={s.distributionBar} max={Math.max(1,...Object.values(data.agents))} value={count} aria-label={`${name}: ${count}`}/>
                      </p>
                    ))}
                    {!Object.keys(data.agents).length && (
                      <p className={s.muted}>运行一次模型复诊任务后显示。</p>
                    )}
                  </div>
                </article>
                <article className={s.card}>
                  <h3>失败原因分布</h3>
                  <div
                    className={s.distributionScroll}
                    role="region"
                    aria-label="失败原因分布列表"
                    tabIndex={0}
                  >
                    {Object.entries(data.failures).sort((a,b)=>b[1]-a[1]).map(([reason, count]) => (
                      <p key={reason}>
                        {reason}
                        <strong style={{ float: "right" }}>{count}</strong>
                        <progress className={`${s.distributionBar} ${s.failureBar}`} max={Math.max(1,...Object.values(data.failures))} value={count} aria-label={`${reason}: ${count}`}/>
                      </p>
                    ))}
                    {!Object.keys(data.failures).length && (
                      <p className={s.muted}>当前窗口内没有失败记录。</p>
                    )}
                  </div>
                </article>
              </div>
              {data.knowledge_observability && <section className={`${s.card} ${s.knowledgeOverview}`} aria-label="知识任务与引用观测">
                <h3>知识任务与引用观测</h3>
                <div className={s.grid}>
                  {(["import","publication","inspection"] as const).filter(kind=>data.knowledge_observability!.queues[kind]).map(kind=><article key={kind}>
                    <h4>{kind==="import" ? "文件入库任务" : kind==="inspection"?"来源巡检任务":"审核发布任务"}（全量）</h4>
                    <div className={s.queueTrack} role="img" aria-label={`${kind}任务状态占比`}>
                      {Object.entries(data.knowledge_observability!.queues[kind]!).map(([status,count])=>count>0 && <span key={status} data-state={status} style={{flex:count}} title={`${status}: ${count}`}/>)}
                    </div>
                    <div className={s.queueLegend}>{Object.entries(data.knowledge_observability!.queues[kind]!).map(([status,count])=><span key={status} data-state={status}>{({queued:"排队",processing:"处理",completed:"完成",failed:"失败"} as Record<string,string>)[status]} <strong>{count}</strong></span>)}</div>
                  </article>)}
                  <article><h4>回答引用检查</h4>
                    <p>{data.knowledge_observability.citations.window}，共 {data.knowledge_observability.citations.run_count} 条；尝试核验 {data.knowledge_observability.citations.attempted} 条，通过 {data.knowledge_observability.citations.passed} 条。</p>
                    <p>引用核验通过率：{data.knowledge_observability.citations.pass_rate===null ? "—" : `${(data.knowledge_observability.citations.pass_rate*100).toFixed(1)}%`}</p>
                    {data.knowledge_observability.citations.pass_rate!==null && <progress className={s.distributionBar} max={1} value={data.knowledge_observability.citations.pass_rate} aria-label="引用核验通过率"/>}
                    <p className={s.muted}>仅检查来源编号、原文与正文关联，不代表事实正确率。未记录或不适用任务不计入分母，服务不可用计为未通过。</p>
                  </article>
                </div>
                <button className={s.secondary} onClick={()=>setPage("knowledge")}>管理知识任务 / 重试</button>
                <div className={s.observationFailures}><div><h4>最近知识任务失败</h4>
                <div className={s.importTaskScroll} role="region" aria-label="知识任务失败列表" tabIndex={0}>
                  {!data.knowledge_observability.recent_failures.length && <p className={s.muted}>暂无失败记录。</p>}
                  {data.knowledge_observability.recent_failures.map(task=><div className={s.importTask} key={task.id}>
                    <div><strong>{task.type==="import"?"入库":task.type==="inspection"?"巡检":"发布"} · {task.id.slice(0,12)}</strong>
                    <p className={s.muted}>已尝试 {task.attempts} / 3 次 · {({worker_interrupted:"服务重启中断",publication_stale:"资料版本已变化，请重新审核",publication_timeout:"发布超时",publication_failed:"发布失败，请检查向量服务",utf8_required:"文件不是UTF-8编码",ocr_required:"扫描件需要先转换为文本",parse_failed:"文件解析失败",empty_document:"文件无可用正文",document_limits:"正文超过处理限制",import_failed:"入库处理失败"} as Record<string,string>)[task.error_code] || task.error_code}</p></div>
                    <button className={s.secondary} disabled={lookupBusy} onClick={()=>void lookupTask(task.id,task.type)}>定位此任务</button>
                  </div>)}
                </div>
                </div><div><h4>最近引用检查未通过</h4>
                <div className={s.importTaskScroll} role="region" aria-label="引用检查失败列表" tabIndex={0}>
                  {!data.knowledge_observability.citations.recent_failures.length && <p className={s.muted}>暂无未通过记录。</p>}
                  {data.knowledge_observability.citations.recent_failures.map(run=><div className={s.importTask} key={run.id}>
                    <div><strong>任务 {run.id.slice(0,12)}</strong><p className={s.muted}>{({failed:"引用修复后仍未通过",failed_after_answer_review:"事实修正后引用失效",unavailable:"结构化引用暂不可用"} as Record<string,string>)[run.status] || run.status}</p></div>
                    <button className={s.secondary} onClick={async()=>{try{setTrace(await api<Trace>(`/v2/admin/traces/${run.id}`));}catch(error){fail(error);}}}>查看执行链路</button>
                  </div>)}
                </div>
                </div></div>
              </section>}
          <section className={`${s.card} ${s.lookupCard}`} aria-label="任务精确定位" ref={taskLookupPanel} style={{scrollMarginTop:90}}>
            <h3>任务精确定位与消耗</h3>
            <form className={s.taskLookupForm} onSubmit={event=>{event.preventDefault();void lookupTask();}}>
              <label className={s.field}>完整任务 ID<input required maxLength={80} value={taskId} onChange={event=>setTaskId(event.target.value)} placeholder="粘贴完整ID，不支持前缀匹配"/></label>
              <label className={s.field}>任务类型<select value={taskKind} onChange={event=>setTaskKind(event.target.value)}>
                <option value="auto">自动识别</option><option value="run">对话任务</option><option value="import">文件入库</option><option value="publication">审核发布</option><option value="inspection">来源巡检</option>
              </select></label>
              <button className={s.secondary} disabled={lookupBusy || !taskId.trim()}>{lookupBusy?"查询中…":"查询任务"}</button>
            </form>
            {lookupError && <p role="alert">{lookupError}</p>}
            {taskDetail && <div aria-label="任务定位结果">
              <p>完整ID：<code style={{overflowWrap:"anywhere"}}>{taskDetail.task.id}</code></p>
              <p>{({run:"对话任务",import:"文件入库",publication:"审核发布",inspection:"来源巡检"})[taskDetail.task.type]} · {({queued:"排队中",processing:"处理中",running:"执行中",completed:"已完成",failed:"失败",interrupted:"已中断",cancelled:"已取消"} as Record<string,string>)[taskDetail.task.status] || taskDetail.task.status}
                {taskDetail.task.attempts!==undefined && ` · 已尝试 ${taskDetail.task.attempts} / 3 次`}
                {taskDetail.task.error_code && ` · 错误码：${taskDetail.task.error_code}`}</p>
              <p>已知 Token：{taskDetail.usage.known_total_tokens.toLocaleString()} · 模型请求 {taskDetail.usage.calls} 次（含重试） · 用量未知 {taskDetail.usage.unknown_usage_calls} 次</p>
              {taskDetail.task.stage && <p>处理阶段：{({queued:"等待处理",parsing:"解析文件",saving:"保存结果",chunking:"文本分块",embedding:"生成向量",completed:"完成",failed:"失败"} as Record<string,string>)[taskDetail.task.stage] || taskDetail.task.stage}{taskDetail.task.total_chunks!==undefined && ` · ${taskDetail.task.completed_chunks} / ${taskDetail.task.total_chunks} 个片段`}</p>}
              <p>模型请求累计耗时：{taskDetail.usage.model_request_sum_ms} ms · 耗时未知 {taskDetail.usage.unknown_duration_calls} 次</p>
              <p>任务生命周期耗时：{taskDetail.lifecycle_elapsed_ms===null?"未知":`${taskDetail.lifecycle_elapsed_ms} ms`}（含排队、处理及重试等待）</p>
              <p className={s.muted}>未知用量不按0计入；累计请求耗时可能包含并行请求，不能当作用户等待时长。未换算费用。</p>
              {taskDetail.legacy_document_calls_present && <p className={s.muted}>该文档存在无法精确归属的旧版模型调用，未计入此任务统计；当前数字不是完整历史消耗。</p>}
              {taskDetail.task.type==="run" && <button className={s.secondary} onClick={async()=>{try{setTrace(await api<Trace>(`/v2/admin/traces/${taskDetail.task.id}`));}catch(error){fail(error);}}}>查看该任务执行链路</button>}
              {taskDetail.task.type!=="run" && taskDetail.task.status==="failed" && (taskDetail.task.attempts || 0)<3 && taskDetail.task.error_code!=="publication_stale" && <button className={s.secondary} disabled={lookupBusy} onClick={()=>void retryLocatedTask()}>重试此任务</button>}
            </div>}
          </section>
              <div className={s.toolbar} style={{ marginTop: 24 }}>
                <h3>调用明细</h3>
                <input
                  className={s.search}
                  style={{ maxWidth: 310 }}
                  aria-label="筛选调用"
                  placeholder="搜索 Agent、模型、用途或任务 ID"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
                <select
                  className={s.secondary}
                  aria-label="筛选调用状态"
                  value={stateFilter}
                  onChange={(e) => setStateFilter(e.target.value)}
                >
                  <option value="">全部状态</option>
                  {Object.entries(states).map(([id, name]) => (
                    <option value={id} key={id}>
                      {name}
                    </option>
                  ))}
                </select>
              </div>
              <div
                className={`${s.card} ${s.tableWrap}`}
                role="region"
                aria-label="调用明细滚动区域"
                tabIndex={0}
              >
                <table className={s.table}>
                  <thead>
                    <tr>
                      {[
                        "时间",
                        "Agent / 用途",
                        "模型",
                        "输入 / 输出 Token",
                        "总 Token",
                        "耗时",
                        "状态",
                        "失败原因",
                        "链路",
                      ].map((x) => (
                        <th key={x}>{x}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((c) => (
                      <tr key={c.id}>
                        <td>
                          {new Date(c.started_at).toLocaleString("zh-CN")}
                        </td>
                        <td>
                          {c.agent}
                          <br />
                          <small>
                            {purposes[c.purpose] || c.purpose} · 第 {c.attempt}{" "}
                            次
                          </small>
                        </td>
                        <td>{c.model}</td>
                        <td>
                          {c.input_tokens ?? "未知"} /{" "}
                          {c.output_tokens ?? "未知"}
                          {c.estimated_input_tokens !== undefined && (
                            <small style={{ display: "block" }}>
                              调用前输入上界估算：{c.estimated_input_tokens}
                              （UTF-8字节法，非账单用量）
                            </small>
                          )}
                        </td>
                        <td>{c.total_tokens ?? "未知"}</td>
                        <td>
                          {c.duration_ms === undefined
                            ? "—"
                            : `${c.duration_ms} ms`}
                        </td>
                        <td>
                          <span className={s.badge} data-state={c.status}>
                            {states[c.status] || c.status}
                          </span>
                        </td>
                        <td>{c.failure_reason || "—"}</td>
                        <td>
                          <button
                            className={s.secondary}
                            onClick={async () => {
                                if(c.run_id.startsWith("knowledge-publication-")){
                                  await lookupTask(c.run_id.slice("knowledge-publication-".length),"publication");
                                  return;
                                }
                                if(c.run_id.startsWith("knowledge-")){
                                  setLookupError("此记录为文档级或独立检索调用，不能唯一归属到任务；请在调用明细查看。");
                                  taskLookupPanel.current?.scrollIntoView({block:"start"});
                                  return;
                                }
                              try {
                                setTrace(
                                    await api<Trace>(
                                      `/v2/admin/traces/${c.run_id}`,
                                    ),
                                );
                              } catch (e) {
                                fail(e);
                              }
                            }}
                          >
                            查看
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!list.length && (
                  <p className={s.muted}>
                    没有匹配的模型调用。大屏不会用模拟日志填充。
                  </p>
                )}
              </div>
            </>
          )}
          {data && (
            <section className={s.trace}>
              <h3>最近对话任务 · 含无模型调用的任务</h3>
              <div
                className={s.tableWrap}
                role="region"
                aria-label="最近任务滚动区域"
                tabIndex={0}
              >
                <table className={s.table}>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>任务 ID</th>
                      <th>模式</th>
                      <th>状态</th>
                      <th>答复结果</th>
                      <th>定位阶段</th>
                      <th>耗时</th>
                      <th>失败码</th>
                      <th>链路</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.runs.map((r) => (
                      <tr key={r.id}>
                        <td>
                          {new Date(r.created_at).toLocaleString("zh-CN")}
                        </td>
                        <td>{r.id.slice(0, 12)}</td>
                        <td>
                          {r.mode === "local"
                            ? "本地规则"
                            : r.mode === "demo"
                              ? "演示"
                              : "真实模型"}
                        </td>
                        <td>
                          {(
                            {
                              completed: "已完成",
                              running: "执行中",
                              failed: "失败",
                              cancelled: "已取消",
                              interrupted: "已中断",
                            } as Record<string, string>
                          )[r.status] || r.status}
                        </td>
                        <td>
                          {r.diagnosis
                            ? answerStatuses[r.diagnosis.outcome] ||
                              r.diagnosis.outcome
                            : "旧任务未标注"}
                        </td>
                        <td>
                          {r.diagnosis?.failed_stage
                            ? stageLabels[r.diagnosis.failed_stage] ||
                              r.diagnosis.failed_stage
                            : "—"}
                        </td>
                        <td>{r.duration_ms} ms</td>
                        <td>{r.error_code || "—"}</td>
                        <td>
                          <button
                            className={s.secondary}
                            onClick={async () => {
                              try {
                                setTrace(
                                  await api<Trace>(`/v2/admin/traces/${r.id}`),
                                );
                              } catch (e) {
                                fail(e);
                              }
                            }}
                          >
                            查看
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!data.runs.length && (
                  <p className={s.muted}>还没有复诊任务。</p>
                )}
              </div>
            </section>
          )}
          {trace && (
            <section className={s.trace}>
              <div className={s.toolbar}>
                <h3>Agent 执行链路</h3>
                <button className={s.secondary} onClick={() => setTrace(null)}>
                  关闭
                </button>
              </div>
                {trace.usage_summary && <p className={s.muted}>本任务模型请求 {trace.usage_summary.calls} 次（含重试） · 已知 Token {trace.usage_summary.known_total_tokens.toLocaleString()} · 用量未知 {trace.usage_summary.unknown_usage_calls} 次 · 请求累计耗时 {trace.usage_summary.model_request_sum_ms} ms（并行请求会累加，不等于等待时长）</p>}
              <RunDiagnosisPanel
                trace={trace}
                busy={recovering}
                onResume={() => {
                  if (!trace.run_id) return;
                  const identifier = trace.run_id;
                  setRecovering(true);
                  void api(`/v2/admin/runs/${identifier}/resume`, "POST")
                    .then(async () => {
                      const value = await api<Trace>(
                        `/v2/admin/traces/${identifier}`,
                      );
                      setTrace(value);
                      await refresh();
                    })
                    .catch(fail)
                    .finally(() => setRecovering(false));
                }}
              />
              <div
                className={s.steps}
                role="region"
                aria-label="执行链路滚动区域"
                tabIndex={0}
              >
                {trace.stages.map((x, i) => (
                  <div key={i}>
                    <strong>{x.agent}</strong>
                    <small>
                      {x.action}
                      {x.round ? ` · 第${x.round}轮` : ""}
                    </small>
                    {x.status && (
                      <small>
                        状态：
                        {(
                          {
                            ok: "成功",
                            failed: "失败",
                            partial: "部分完成",
                            running: "执行中",
                          } as Record<string, string>
                        )[x.status] || x.status}
                      </small>
                    )}
                    {x.reason && (
                      <small>
                        决策：
                        {(
                          {
                            daily: "日常事务咨询",
                            medical_education: "医药知识科普",
                            followup: "复诊询问",
                            hospital: "医院服务咨询",
                            ok: "查询完成",
                            search_empty: "没有搜索结果",
                            all_search_providers_failed: "搜索服务均未成功",
                          } as Record<string, string>
                        )[x.reason] || x.reason}
                      </small>
                    )}
                    {x.stop_reason && (
                      <small>判定 / 停止原因：{x.stop_reason}</small>
                    )}
                    {!!x.provider_attempts?.length && (
                      <div
                        className={s.searchAttempts}
                        aria-label="搜索服务调用记录"
                      >
                        <small>实际搜索顺序（不含未调用的服务）</small>
                        <ol>
                          {x.provider_attempts.map((attempt, index) => (
                            <li key={`${attempt.provider}-${index}`}>
                              <strong>
                                {(
                                  {
                                    baidu: "百度",
                                    bocha: "Bocha",
                                    tavily: "Tavily",
                                    tencent: "腾讯 SearchPro",
                                    searxng: "SearXNG",
                                  } as Record<string, string>
                                )[attempt.provider] || attempt.provider}
                              </strong>
                              {attempt.error ? (
                                <span>
                                  {" · 调用失败："}
                                  {(
                                    {
                                      search_http_error: "接口返回错误",
                                      search_timeout: "请求超时",
                                      search_network_error: "连接失败",
                                      search_business_error: "服务拒绝请求",
                                      search_schema_error: "响应格式异常",
                                      search_key_missing: "缺少凭据",
                                      search_auth_error: "授权或凭据错误",
                                      search_quota_error: "服务额度不足",
                                      search_access_refused:
                                        "访问被拒绝，已按规则尝试恢复",
                                    } as Record<string, string>
                                  )[attempt.error] || attempt.error}
                                  {attempt.http_status
                                    ? `（HTTP ${attempt.http_status}）`
                                    : ""}
                                </span>
                              ) : (
                                <span>
                                  {` · 返回 ${attempt.raw_count ?? "未知"} 条，保留 ${attempt.candidate_count ?? "未知"} 条候选`}
                                  {(attempt.candidate_count ?? 0) > 0
                                    ? " · 本轮采用"
                                    : " · 无可用候选"}
                                </span>
                              )}
                              {index <
                                (x.provider_attempts?.length ?? 0) - 1 && (
                                <small>继续下一次搜索尝试</small>
                              )}
                            </li>
                          ))}
                        </ol>
                        <small>搜索候选须读取正文后才可作为回答依据。</small>
                      </div>
                    )}
                    {x.source_count !== undefined && (
                      <small>
                        {x.action === "读取医院资料"
                          ? "已取得正文"
                          : "来源数量"}
                        ：{x.source_count}
                      </small>
                    )}
                    {x.browser_reads !== undefined && (
                      <small>浏览器补充读取：{x.browser_reads} 次</small>
                    )}
                  </div>
                ))}
              </div>
              <p className={s.muted}>
                此链路有 {trace.calls.length}{" "}
                次实际模型请求。日志不包含患者正文、模型提示词或密钥。
              </p>
            </section>
          )}
        </>
      )}
    </div>
  );
}
