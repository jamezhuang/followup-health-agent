"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "./v2-api";
import type {
  ReportReview,
  ReportFields,
  Subject,
  DocumentRow,
} from "./v2-api";
import s from "./v2.module.css";
import ReportDetailsEditor, { emptyReportDetails } from "./ReportDetailsEditor";

export default function ReportReviewDialog({
  draft,
  subject,
  onClose,
  onSaved,
  onBusy,
}: {
  draft: ReportReview;
  subject: Subject;
  onClose: () => void;
  onSaved: (row: DocumentRow) => void;
  onBusy: (busy: boolean) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [row, setRow] = useState(draft);
  const [fields, setFields] = useState<ReportFields>({
    ...structuredClone(draft.fields),
    details: { ...emptyReportDetails, ...draft.fields.details },
    additional_fields: draft.fields.additional_fields || [],
  });
  const [title, setTitle] = useState(draft.title);
  const [type, setType] = useState(draft.report_type);
  const [update, setUpdate] = useState(draft.update_current);
  const [saveTasks, setSaveTasks] = useState(draft.save_tasks);
  const [confirmed, setConfirmed] = useState(false),
    [identity, setIdentity] = useState(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const previous =
    draft.latest_information === undefined
      ? draft.previous_information
      : draft.latest_information;
  const mismatch =
    fields.patient_name !== (subject.patient_name || subject.name);
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);
  function change(next: ReportFields) {
    setFields(next);
    setConfirmed(false);
    setIdentity(false);
  }
  async function persist() {
    const { additional_fields: automaticSupplement, ...reviewedFields } =
      fields;
    return await api<ReportReview>(`/v2/report-reviews/${row.id}`, "PUT", {
      ...reviewedFields,
      revision: row.revision,
      title,
      report_type: type,
      update_current: update,
      save_tasks: saveTasks,
      expected_current_id: previous?.id || "",
    });
  }
  async function finish(approve: boolean) {
    setBusy(true);
    onBusy(true);
    setError("");
    try {
      const saved = await persist();
      setRow(saved);
      if (approve) {
        const document = await api<DocumentRow>(
          `/v2/report-reviews/${row.id}/confirm`,
          "POST",
          {
            revision: saved.revision,
            confirmed,
            identity_confirmed: identity,
          },
        );
        onSaved(document);
      } else onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存未完成，请重试。");
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className={`${s.reviewDialog} ${s.compactReview}`}
      aria-labelledby="report-review-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) void finish(false);
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void finish(true);
        }}
      >
        <div className={`${s.sectionHeading} ${s.reviewHeading}`}>
          <h2 id="report-review-title">核对报告并保存</h2>
          <p className={s.muted}>
            保存到「{subject.name} · {subject.relation}
            」；“无”表示报告未提供，可按原文补充，不代表检查正常。
          </p>
        </div>
        {error && (
          <p role="alert" className={s.error}>
            {error}
          </p>
        )}
        <fieldset disabled={busy} className={s.reviewFieldset}>
          <h3>报告基本信息</h3>
          <div className={s.reviewGrid}>
            <label className={s.field}>
              资料名称
              <input
                required
                maxLength={160}
                value={title}
                onChange={(e) => {
                  setTitle(e.target.value);
                  setConfirmed(false);
                }}
              />
            </label>
            <label className={s.field}>
              资料类型
              <select
                aria-label="资料类型"
                value={type}
                onChange={(e) => {
                  setType(e.target.value as typeof type);
                  setUpdate(false);
                  setConfirmed(false);
                }}
              >
                <option value="inspection">检查报告 / 其他资料</option>
                <option value="followup">复诊报告</option>
              </select>
            </label>
            <label className={s.field}>
              报告患者姓名
              <input
                maxLength={40}
                value={fields.patient_name}
                placeholder="无"
                onChange={(e) =>
                  change({ ...fields, patient_name: e.target.value })
                }
              />
            </label>
            <label className={s.field}>
              检查 / 就诊日期
              {!fields.examination_date && (
                <span className={s.missingValue}>无</span>
              )}
              <input
                type="date"
                aria-label="检查 / 就诊日期"
                value={fields.examination_date || ""}
                onChange={(e) =>
                  change({ ...fields, examination_date: e.target.value })
                }
              />
            </label>
            <label className={s.field}>
              医院 / 医疗机构
              <input
                maxLength={200}
                value={fields.institution}
                placeholder="无"
                onChange={(e) =>
                  change({ ...fields, institution: e.target.value })
                }
              />
            </label>
            <label className={s.field}>
              报告日期
              {!fields.report_date && (
                <span className={s.missingValue}>无</span>
              )}
              <input
                type="date"
                aria-label="报告日期"
                aria-describedby="report-date-hint"
                value={fields.report_date || ""}
                onChange={(e) =>
                  change({ ...fields, report_date: e.target.value })
                }
              />
              <small id="report-date-hint" className={s.muted}>
                报告出具日期，可与检查日期不同；不清楚可留空。
              </small>
            </label>
          </div>
          <ReportDetailsEditor fields={fields} onChange={change} />
          {mismatch && (
            <label className={s.reviewNotice}>
              <input
                type="checkbox"
                checked={identity}
                onChange={(e) => setIdentity(e.target.checked)}
              />
              报告姓名缺失或与档案不同，我确认这份资料属于「{subject.name}」。
            </label>
          )}
          <h3>检查项目</h3>
          {!fields.observations.length && (
            <p className={s.muted}>
              无数值检查项目，可补充遗漏项目；影像文字已单独整理在上方。
            </p>
          )}
          {fields.observations.map((item, index) => (
            <section className={s.reviewItem} key={index}>
              <div className={s.reviewGrid}>
                {(
                  [
                    ["label", "检查项目"],
                    ["value", "数值 / 内容"],
                    ["unit", "单位"],
                    ["reference_range", "报告参考范围"],
                  ] as const
                ).map(([key, label]) => (
                  <label className={s.field} key={key}>
                    {label}
                    <input
                      required={key === "label" || key === "value"}
                      value={item[key]}
                      maxLength={key === "reference_range" ? 160 : 100}
                      placeholder="无"
                      onChange={(e) =>
                        change({
                          ...fields,
                          observations: fields.observations.map((old, i) =>
                            i === index
                              ? { ...old, [key]: e.target.value }
                              : old,
                          ),
                        })
                      }
                    />
                  </label>
                ))}
              </div>
              <UnitSuggestion
                label={item.label}
                unit={item.unit}
                onChoose={(unit) =>
                  change({
                    ...fields,
                    observations: fields.observations.map((old, i) =>
                      i === index ? { ...old, unit } : old,
                    ),
                  })
                }
              />
              {item.quote && (
                <p className={s.muted}>
                  报告原文 · {item.location}：{item.quote}
                </p>
              )}
              <button
                type="button"
                className={s.iconButton}
                onClick={() =>
                  change({
                    ...fields,
                    observations: fields.observations.filter(
                      (_, i) => i !== index,
                    ),
                  })
                }
              >
                移除此项目
              </button>
            </section>
          ))}
          <button
            type="button"
            className={s.secondary}
            disabled={fields.observations.length >= 60}
            onClick={() =>
              change({
                ...fields,
                observations: [
                  ...fields.observations,
                  {
                    label: "",
                    value: "",
                    unit: "",
                    reference_range: "",
                    location: "用户核对补充",
                    quote: "",
                  },
                ],
              })
            }
          >
            补充检查项目
          </button>
          {fields.observations.length > 0 && !fields.examination_date && (
            <p className={s.reviewNotice}>
              尚未确认实际检查日期：报告会保存，检查项目暂不加入按日期排列的记录。
            </p>
          )}
          <h3>药品与用法</h3>
          {!fields.medications.length && <p className={s.missingValue}>无</p>}
          <p className={s.muted}>
            这里保存报告记载，不代表患者已经开始服用；缺少规格时不换算剂量。
          </p>
          {fields.medications.map((item, index) => (
            <section className={s.reviewItem} key={index}>
              <div className={s.reviewGrid}>
                {(
                  [
                    ["name", "药品名称"],
                    ["strength", "规格"],
                    ["dose", "每次用量"],
                    ["frequency", "服用频次"],
                    ["effective_date", "已确认生效日期"],
                  ] as const
                ).map(([key, label]) => (
                  <label className={s.field} key={key}>
                    {label}
                    <input
                      required={key === "name"}
                      type={key === "effective_date" ? "date" : "text"}
                      value={item[key]}
                      maxLength={100}
                      onChange={(e) =>
                        change({
                          ...fields,
                          medications: fields.medications.map((old, i) =>
                            i === index
                              ? { ...old, [key]: e.target.value }
                              : old,
                          ),
                        })
                      }
                    />
                  </label>
                ))}
              </div>
              {item.quote && (
                <p className={s.muted}>
                  报告原文 · {item.location}：{item.quote}
                </p>
              )}
              <button
                type="button"
                className={s.iconButton}
                onClick={() =>
                  change({
                    ...fields,
                    medications: fields.medications.filter(
                      (_, i) => i !== index,
                    ),
                  })
                }
              >
                移除此药品
              </button>
            </section>
          ))}
          <button
            type="button"
            className={s.secondary}
            disabled={fields.medications.length >= 30}
            onClick={() =>
              change({
                ...fields,
                medications: [
                  ...fields.medications,
                  {
                    name: "",
                    strength: "",
                    dose: "",
                    frequency: "",
                    effective_date: "",
                    location: "用户核对补充",
                    quote: "",
                  },
                ],
              })
            }
          >
            补充药品
          </button>
          <label className={s.field}>
            复诊信息摘要{update ? "（必填）" : "（可选）"}
            <textarea
              required={update}
              maxLength={4000}
              value={fields.summary}
              onChange={(e) => change({ ...fields, summary: e.target.value })}
            />
          </label>
          {type === "followup" && (
            <>
              <label className={s.reviewNotice}>
                <input
                  type="checkbox"
                  checked={update}
                  onChange={(e) => {
                    setUpdate(e.target.checked);
                    setConfirmed(false);
                  }}
                />
                同时更新当前复诊信息（新增版本，旧信息保留）
              </label>
              {update && (
                <section className={s.reviewItem}>
                  <h3>本次更新核对</h3>
                  <p>当前信息：{previous?.summary || "尚无当前复诊信息"}</p>
                  <p className={s.reportText}>
                    拟更新为：{fields.summary || "请填写本次已核对摘要"}
                  </p>
                  <p className={s.muted}>
                    本次核对的药品资料随当前信息保存，不会自动覆盖日常服药记录。
                  </p>
                </section>
              )}
            </>
          )}
          {!fields.doctor_tasks.length && (
            <section className={s.reportSection}>
              <h3>医生交代的后续事项</h3>
              <p className={s.missingValue}>无</p>
            </section>
          )}
          {!!fields.doctor_tasks.length && (
            <section className={s.reviewItem}>
              <h3>医生交代的后续事项</h3>
              {fields.doctor_tasks.map((task, index) => (
                <div key={index}>
                  <label className={s.field}>
                    事项内容
                    <input
                      maxLength={300}
                      value={task.content}
                      onChange={(e) =>
                        change({
                          ...fields,
                          doctor_tasks: fields.doctor_tasks.map((old, i) =>
                            i === index
                              ? { ...old, content: e.target.value }
                              : old,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className={s.field}>
                    执行日期（未明确可留空）
                    <input
                      type="date"
                      value={task.due_date}
                      onChange={(e) =>
                        change({
                          ...fields,
                          doctor_tasks: fields.doctor_tasks.map((old, i) =>
                            i === index
                              ? { ...old, due_date: e.target.value }
                              : old,
                          ),
                        })
                      }
                    />
                  </label>
                  <p className={s.muted}>医嘱原文：{task.quote}</p>
                </div>
              ))}
              <label className={s.row}>
                <input
                  type="checkbox"
                  checked={saveTasks}
                  onChange={(e) => {
                    setSaveTasks(e.target.checked);
                    setConfirmed(false);
                  }}
                />
                我已核对以上事项，保存为待办
              </label>
            </section>
          )}
          <details>
            <summary>查看完整识别正文</summary>
            {draft.pieces.map((piece, index) => (
              <section key={index}>
                <small>{piece.location}</small>
                <p className={s.reportText}>{piece.text}</p>
              </section>
            ))}
          </details>
          <label className={s.reviewNotice}>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            我已核对患者、日期、检查项目和药品信息，确认保存到「{subject.name}
            」。
          </label>
          <div className={s.reviewFooter}>
            <button
              type="button"
              className={s.secondary}
              onClick={() => void finish(false)}
            >
              保存草稿，稍后核对
            </button>
            <button
              className={s.primary}
              disabled={!confirmed || (mismatch && !identity)}
            >
              {busy
                ? "正在保存…"
                : update
                  ? "保存并更新复诊信息"
                  : "确认并保存报告"}
            </button>
          </div>
        </fieldset>
      </form>
    </dialog>
  );
}

function UnitSuggestion({
  label,
  unit,
  onChoose,
}: {
  label: string;
  unit: string;
  onChoose: (unit: string) => void;
}) {
  const [hint, setHint] = useState("");
  useEffect(() => {
    let alive = true;
    void api<{ default: string }>(
      `/v2/unit-hints?label=${encodeURIComponent(label)}`,
    )
      .then((data) => {
        if (alive) setHint(data.default);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [label]);
  if (unit || !hint) return null;
  return (
    <p className={s.muted}>
      此项目常用单位为 {hint}，请核对报告后选择。
      <button
        type="button"
        className={s.iconButton}
        onClick={() => onChoose(hint)}
      >
        采用 {hint}
      </button>
    </p>
  );
}
