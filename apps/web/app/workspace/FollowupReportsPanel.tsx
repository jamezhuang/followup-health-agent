"use client";
import { useCallback, useEffect, useState } from "react";
import { API, api } from "./v2-api";
import type { DocumentRow, Subject, ReportMedication } from "./v2-api";
import s from "./v2.module.css";
import { ReportDetailsSummary } from "./ReportDetailsEditor";

type Information = {
  id: string;
  document_id: string;
  title: string;
  followup_date: string;
  institution: string;
  summary: string;
  created_at: string;
  medications?: ReportMedication[];
};
type Report = DocumentRow & {
  created_at: string;
  upload_id?: string;
  summary?: string;
};
type Reports = {
  items: Report[];
  current: Information | null;
  history: Information[];
};
type Todo = {
  id: string;
  content: string;
  due_date: string;
  quote: string;
  source_document_id: string;
  status: string;
  revision: number;
};
type Event = {
  document_id: string;
  updated_current: boolean;
  record_count: number;
  task_count: number;
};
const recordedTime = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  });

export default function FollowupReportsPanel({
  subject,
  fail,
}: {
  subject: Subject;
  fail: (error: unknown) => void;
}) {
  const [data, setData] = useState<Reports>({
    items: [],
    current: null,
    history: [],
  });
  const [todos, setTodos] = useState<Todo[]>([]),
    [events, setEvents] = useState<Event[]>([]),
    [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const load = useCallback(async () => {
    const [reports, documents, tasks, updates] = await Promise.all([
      api<Reports>(`/v2/followup-reports?subject=${subject.id}`),
      api<{ items: Report[] }>(`/v2/documents?subject=${subject.id}`),
      api<{ items: Todo[] }>(`/v2/todos?subject=${subject.id}`),
      api<{ items: Event[] }>(`/v2/report-events?subject=${subject.id}`),
    ]);
    return {
      reports: {
        ...reports,
        items: [
          ...new Map(
            [
              ...documents.items.filter((row) => row.confirmed),
              ...reports.items,
            ]
              .filter((row) => row.subject === subject.id)
              .map((row) => [row.id, row]),
          ).values(),
        ].sort(
          (a, b) =>
            b.created_at.localeCompare(a.created_at) ||
            b.id.localeCompare(a.id),
        ),
      },
      tasks: tasks.items,
      updates: updates.items,
    };
  }, [subject.id]);
  useEffect(() => {
    let alive = true;
    void load()
      .then((result) => {
        if (alive) {
          setData(result.reports);
          setTodos(result.tasks);
          setEvents(result.updates);
        }
      })
      .catch((error) => {
        if (alive) {
          setLoadFailed(true);
          fail(error);
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [load, fail]);
  async function updateTodo(todo: Todo, status: string) {
    setBusy(true);
    try {
      const updated = await api<Todo>(`/v2/todos/${todo.id}`, "PUT", {
        revision: todo.revision,
        status,
      });
      setTodos((old) =>
        old.map((row) => (row.id === updated.id ? updated : row)),
      );
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  }
  const scopeLabel = (
    <p className={s.archiveScope}>
      正在查看：<strong>{subject.name}</strong>
      {subject.relation ? `（${subject.relation}）` : ""} 的复诊档案
    </p>
  );
  if (loading || loadFailed) {
    return (
      <section className={s.followupReports} aria-label="复诊报告">
        {scopeLabel}
        <p className={s.muted} role={loadFailed ? "alert" : "status"}>
          {loadFailed
            ? "该对象的档案暂未加载成功，请重新选择对象或稍后重试。"
            : "正在加载该对象的报告与复诊记录…"}
        </p>
      </section>
    );
  }
  return (
    <section className={s.followupReports} aria-label="复诊报告">
      {scopeLabel}
      <article className={s.card} style={{ marginBottom: 18 }}>
        <h3>当前复诊信息</h3>
        {data.current ? (
          <>
            <p>
              录入时间：
              <time dateTime={data.current.created_at}>
                {recordedTime(data.current.created_at)}
              </time>
            </p>
            <p>
              {data.current.followup_date
                ? `就诊日期：${data.current.followup_date} · `
                : ""}
              {data.current.institution || "未填写就诊机构"}
            </p>
            <p className={s.reportText}>{data.current.summary}</p>
            {!!data.current.medications?.length && (
              <details>
                <summary>本次已确认的药品资料</summary>
                {data.current.medications.map((med, index) => (
                  <p key={index}>
                    {med.name} · {med.strength || "规格未确认"} ·{" "}
                    {med.dose || "用量未确认"} · {med.frequency || "频次未确认"}
                    {med.effective_date
                      ? ` · 生效日期：${med.effective_date}`
                      : ""}
                  </p>
                ))}
                <p className={s.muted}>
                  资料来自本次核对，不代表患者已实际执行全部用法。
                </p>
              </details>
            )}
            <p className={s.muted}>
              来源：{data.current.title} · 第{data.history.length}次信息更新
            </p>
            {data.history.length > 1 && (
              <details>
                <summary>查看以前的复诊信息</summary>
                {data.history.slice(1).map((info) => (
                  <article className={s.reviewItem} key={info.id}>
                    <p>
                      {recordedTime(info.created_at)} · {info.title}
                    </p>
                    <p className={s.reportText}>{info.summary}</p>
                  </article>
                ))}
              </details>
            )}
          </>
        ) : (
          <p className={s.muted}>
            尚未更新当前复诊信息。请从对话输入框“＋”上传复诊报告，核对后选择是否更新。
          </p>
        )}
      </article>
      <div
        className={`${s.toolbar} ${s.sectionHeading}`}
        style={{ marginTop: 24 }}
      >
        <h2>历史报告与更新记录</h2>
        <p className={s.muted}>
          仅显示「{subject.name}
          」的已归档报告，按录入时间排列；其他对象的报告请切换对应档案查看。
        </p>
      </div>
      <div className={s.reportHistory}>
        {data.items.map((report) => {
          const index = data.history.findIndex(
            (info) => info.document_id === report.id,
          );
          const event = events.find((item) => item.document_id === report.id);
          const patientName = report.key_fields?.patient_name?.trim() || "";
          const expectedName =
            subject.patient_name?.trim() || subject.name.trim();
          const nameDiffers =
            patientName &&
            patientName.replace(/\s/g, "") !== expectedName.replace(/\s/g, "");
          return (
            <article className={s.card} key={report.id}>
              <p className={s.reportOwnership}>
                <span>
                  所属档案：<strong>{subject.name}</strong>
                </span>
                <span>报告患者：{patientName || "报告未记载姓名"}</span>
              </p>
              {nameDiffers && (
                <p className={s.badge} data-state="failed">
                  报告患者姓名与档案登记姓名「{expectedName}
                  」不同，请核对资料归属。
                </p>
              )}
              <span className={s.badge}>
                {report.report_type === "followup"
                  ? "复诊报告"
                  : "检查报告 / 其他资料"}
              </span>
              <span className={s.badge}>
                {data.current?.document_id === report.id
                  ? "当前信息来源"
                  : index >= 0
                    ? "历史信息版本"
                    : "已归档"}
              </span>
              <h3 style={{ marginTop: 10 }}>{report.title}</h3>
              <p>
                录入时间：
                <time dateTime={report.created_at}>
                  {recordedTime(report.created_at)}
                </time>
              </p>
              <p className={s.muted}>
                {report.followup_date
                  ? `检查 / 就诊日期：${report.followup_date} · `
                  : "检查 / 就诊日期未确认 · "}
                {report.key_fields?.report_date
                  ? `报告日期：${report.key_fields.report_date} · `
                  : "报告日期未确认 · "}
                {report.institution || "机构未填写"}
                {index >= 0
                  ? ` · 第${data.history.length - index}次信息更新`
                  : ""}
              </p>
              {event && (
                <p className={s.muted}>
                  本次新增 {event.record_count} 项检查记录、{event.task_count}{" "}
                  项待办；
                  {event.updated_current
                    ? "已更新当前复诊信息"
                    : "未改变当前复诊信息"}
                  。
                </p>
              )}
              {!!report.key_fields?.observations.length && (
                <details>
                  <summary>查看已确认检查项目</summary>
                  {report.key_fields.observations.map((item, i) => (
                    <p key={i}>
                      {item.label}：{item.value} {item.unit || "单位待确认"}
                      {item.reference_range
                        ? ` · 报告参考范围：${item.reference_range}`
                        : ""}
                    </p>
                  ))}
                </details>
              )}
              {report.key_fields && (
                <details>
                  <summary>查看已核对的报告字段</summary>
                  <ReportDetailsSummary fields={report.key_fields} />
                </details>
              )}
              {report.upload_id && (
                <a
                  className={s.reportDownload}
                  href={`${API}/v2/followup-reports/${report.id}/file`}
                  target="_blank"
                  rel="noreferrer"
                >
                  下载原文件
                </a>
              )}
            </article>
          );
        })}
      </div>
      {!data.items.length && (
        <p className={s.muted}>
          「{subject.name}
          」暂无已归档报告。从对话中上传并核对保存到该对象后，将记录在这里。
        </p>
      )}
      <div
        className={`${s.toolbar} ${s.sectionHeading}`}
        style={{ marginTop: 24 }}
      >
        <h2>复诊待办</h2>
        <p className={s.muted}>
          仅保存已经核对的医生交代事项，不自动安排新的检查或用药。
        </p>
      </div>
      {!todos.length && <p className={s.muted}>暂无已确认待办。</p>}
      {todos.map((todo) => (
        <article className={s.card} key={todo.id} style={{ marginBottom: 12 }}>
          <span className={s.badge}>
            {todo.status === "done"
              ? "已完成"
              : todo.status === "cancelled"
                ? "已取消"
                : "待完成"}
          </span>
          <h3>{todo.content}</h3>
          <p>
            {todo.due_date ? `执行日期：${todo.due_date}` : "执行日期未明确"}
          </p>
          <details>
            <summary>查看医嘱依据</summary>
            <p className={s.reportText}>{todo.quote}</p>
            <p className={s.muted}>
              来源：
              {data.items.find(
                (report) => report.id === todo.source_document_id,
              )?.title || "已确认报告"}
            </p>
          </details>
          <div className={s.row} style={{ marginTop: 12 }}>
            {todo.status === "pending" ? (
              <>
                <button
                  className={s.secondary}
                  disabled={busy}
                  onClick={() => void updateTodo(todo, "done")}
                >
                  标记完成
                </button>
                <button
                  className={s.iconButton}
                  disabled={busy}
                  onClick={() => void updateTodo(todo, "cancelled")}
                >
                  取消待办
                </button>
              </>
            ) : (
              <button
                className={s.secondary}
                disabled={busy}
                onClick={() => void updateTodo(todo, "pending")}
              >
                恢复待办
              </button>
            )}
          </div>
        </article>
      ))}
    </section>
  );
}
