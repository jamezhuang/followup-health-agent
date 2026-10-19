"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "./v2-api";
import type { Subject, ReportReview, DocumentRow } from "./v2-api";
import ReportReviewDialog from "./ReportReviewDialog";
import s from "./v2.module.css";

const NEW_SUBJECT = "__new__";
type SubjectProposal = { name: string; patient_name: string; sex: string; age: string; relation: string; created_subject_id: string; examination_date: string };

export default function ArchiveAttachmentDialog({ id, subjects, initialSubject, onClose, onSaved, onSubjectCreated }: {
  id: string; subjects: Subject[]; initialSubject: string; onClose: () => void;
  onSaved: (document: DocumentRow) => void;
  onSubjectCreated: (subject: Subject) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [subjectId, setSubjectId] = useState(initialSubject);
  const [draft, setDraft] = useState<ReportReview | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [createdSubject, setCreatedSubject] = useState<Subject | null>(null);
  const [fields, setFields] = useState({ name: "", patient_name: "", sex: "", age: "", relation: "" });
  const initialContext = useRef({ subjects, initialSubject });
  const [examinationDate, setExaminationDate] = useState("");
  const subject = createdSubject?.id === subjectId ? createdSubject : subjects.find(s => s.id === subjectId);
  useEffect(() => { if (!draft) dialog.current?.showModal(); return () => dialog.current?.close(); }, [draft]);
  useEffect(() => {
    let alive = true;
    void api<SubjectProposal>(`/v2/chat-attachments/${id}/subject-proposal`).then(proposal => {
      if (!alive) return;
      setFields({ name: proposal.name, patient_name: proposal.patient_name, age: proposal.age, relation: "",
        sex: ["男", "女", "其他"].includes(proposal.sex) ? proposal.sex : "" });
      setExaminationDate(proposal.examination_date);
      const { subjects: existing, initialSubject: initial } = initialContext.current;
      const sameName = existing.filter(person => (person.patient_name || person.name).trim() === proposal.patient_name && !!proposal.patient_name);
      const selected = existing.find(person => person.id === initial);
      setSubjectId(existing.some(person => person.id === proposal.created_subject_id) ? proposal.created_subject_id :
        selected && (!proposal.patient_name || (selected.patient_name || selected.name).trim() === proposal.patient_name) ? selected.id :
        sameName.length === 1 ? sameName[0].id : NEW_SUBJECT);
    }).catch(e => { if (alive) setError(e instanceof Error ? e.message : "无法读取报告中的患者信息。"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [id]);
  async function review() {
    if (loading || busy || (!subject && subjectId !== NEW_SUBJECT)) return;
    setBusy(true); setError("");
    try {
      let target = subject;
      if (subjectId === NEW_SUBJECT) {
        target = await api<Subject>(`/v2/chat-attachments/${id}/subject`, "POST", { ...fields, name: fields.name.trim(), confirmed: true, diseases: [], constraints: "" });
        setCreatedSubject(target); setSubjectId(target.id); onSubjectCreated(target);
      }
      if (!target) return;
      const row = await api<ReportReview>(`/v2/chat-attachments/${id}/archive-review?subject=${target.id}`, "POST");
      if (row.status === "completed") setSaved(true); else setDraft(row);
    } catch (e) { setError(e instanceof Error ? e.message : "无法打开核对窗口。"); }
    finally { setBusy(false); }
  }
  if (draft && subject) return <ReportReviewDialog draft={draft} subject={subject}
    onBusy={setBusy} onClose={onClose} onSaved={document => { onSaved(document); onClose(); }} />;
  return <dialog ref={dialog} className={`${s.reviewDialog} ${s.archiveDialog}`} aria-label="保存到复诊档案"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <h2>保存到复诊档案</h2>
    {saved ? <p role="status">此附件已经保存到复诊档案，无需重复保存。</p> : <>
      <p>选择已有对象，或核对报告信息后新建对象。</p>
      <label className={s.field}>保存对象
        <select aria-label="保存对象" value={subjectId} disabled={busy || loading} onChange={e => setSubjectId(e.target.value)}>
          <option value="">请选择对象</option>
          <option value={NEW_SUBJECT}>根据报告新建复诊对象</option>
          {subjects.map(person => <option key={person.id} value={person.id}>{person.name}</option>)}
        </select>
      </label>
      {loading && <p role="status">正在读取报告中的患者信息…</p>}
      {subjectId === NEW_SUBJECT && !loading && <section className={s.reportSection}>
        <h3>核对新对象信息</h3>
        <p className={s.muted}>信息来自本次报告，可修改；年龄按报告记载，缺失信息可稍后补充。</p>
        {examinationDate && <p className={s.muted}>报告检查日期：{examinationDate}</p>}
        {subjects.some(person => (person.patient_name || person.name).trim() === fields.patient_name.trim() && !!fields.patient_name.trim()) &&
          <p role="status">档案中已有同名对象，请先核对是否应存入已有对象。</p>}
        <div className={s.reviewGrid}>
          <label className={s.field}>患者姓名
            <input aria-label="新对象患者姓名" maxLength={40} value={fields.patient_name} disabled={busy}
              placeholder="报告未识别到姓名，请核对填写" onChange={e => setFields({ ...fields, patient_name: e.target.value,
                name: fields.name === fields.patient_name ? e.target.value : fields.name })} />
          </label>
          <label className={s.field}>档案姓名 / 称呼（必填）
            <input aria-label="新对象姓名 / 称呼" required maxLength={40} value={fields.name} disabled={busy}
              onChange={e => setFields({ ...fields, name: e.target.value })} />
          </label>
          <label className={s.field}>性别
            <select aria-label="新对象性别" value={fields.sex} disabled={busy} onChange={e => setFields({ ...fields, sex: e.target.value })}>
              <option value="">无 / 未识别</option><option value="男">男</option><option value="女">女</option><option value="其他">其他</option>
            </select>
          </label>
          <label className={s.field}>报告记载年龄
            <input aria-label="新对象报告年龄" maxLength={40} value={fields.age} disabled={busy} placeholder="无"
              onChange={e => setFields({ ...fields, age: e.target.value })} />
          </label>
          <label className={s.field}>与我的关系（可选）
            <input aria-label="新对象与我的关系" maxLength={20} value={fields.relation} disabled={busy} placeholder="如本人、父亲、母亲"
              onChange={e => setFields({ ...fields, relation: e.target.value })} />
          </label>
        </div>
      </section>}
      {error && <p role="alert" className={s.error}>{error}</p>}
    </>}
    <div className={s.archiveActions}>
      {!saved && <button className={s.primary} disabled={busy || loading || (subjectId === NEW_SUBJECT ? !fields.name.trim() : !subject)}
        onClick={() => void review()}>{subjectId === NEW_SUBJECT ? "确认新建对象并核对报告" : "核对并保存"}</button>
      }
      <button className={s.secondary} disabled={busy} onClick={onClose}>关闭</button>
    </div>
  </dialog>;
}
