import type { Result } from "./v2-api";

export function answerText(result: Result): string {
  if (result.material_notice && !result.content?.startsWith(result.material_notice))
    return result.material_notice + "\n\n" + answerText({ ...result, material_notice: undefined });
  if (result.kind === "chat") return result.content || "";
  const preparation = result.preparation_checklist || [];
  if (preparation.length) {
    return [
      '## 需要准备的资料清单',
      ...preparation.map((item) => `- ${item.title}：${item.action}`),
      ...(result.patient_notes?.length ? ['\n## 档案备注与本次关注事项', ...result.patient_notes.map((note) => `- ${note}`)] : []),
      '\n## 复诊时重点问医生',
      ...(result.questions.length ? result.questions : ['本次复诊需要重点解决什么问题，后续复诊如何安排？']).map((question) => `- ${question}`),
      ...(result.preparation_suggestions?.length ? ['\n## 结合疾病情况的准备建议', ...result.preparation_suggestions.map((suggestion) => `- ${suggestion}`)] : []),
    ].join('\n');
  }
  const nextSteps = [
    ...(result.gaps.length
      ? ["\n## 仍需补充或确认", ...result.gaps.map((gap) => `- ${gap}`)]
      : []),
    ...(result.questions.length
      ? [
          preparation.length ? "\n## 复诊时重点问医生" : "\n## 可进一步确认的问题",
          ...result.questions.map((question) => `- ${question}`),
        ]
      : []),
  ];
  return [
    preparation.length ? "# 复诊准备摘要" : "# 检查资料整理",
    preparation.length ? "根据已确认资料整理，供本次复诊准备使用。" : "根据已确认资料回答本次问题。",
    ...(preparation.length
      ? [
          "\n## 复诊前准备清单",
          result.preparation_scope_note || "",
          ...preparation.map(
            (item) =>
              `- **${item.title}**（${{ provided: "本轮已提供", to_confirm: "待确认", reminder: "准备提醒" }[item.status]}）：${item.action}`,
          ),
          ...nextSteps,
        ]
      : []),
    ...(result.records.length
      ? [
          "\n## 已确认记录",
          ...result.records.map(
            (r) => `- ${r.date} ${r.label}：${r.value} ${r.unit}`,
          ),
        ]
      : []),
    ...(result.comparisons?.length
      ? [
          "\n## 历次记录对比",
          ...result.comparisons.map(
            (c) =>
              `- ${c.label}：${c.previous_date} ${c.previous_value} → ${c.current_date} ${c.current_value} ${c.unit}，差值 ${c.delta} ${c.unit}。`,
          ),
        ]
      : []),
    ...(result.claims.length
      ? [
          "\n## 有来源的整理结论",
          ...result.claims.map((c) => `- ${c.text}\n  引用：${c.quote}`),
        ]
      : []),
    ...(!preparation.length ? nextSteps : []),
    ...(result.sources.length
      ? [
          "\n## 来源",
          ...result.sources.map(
            (source) =>
              `- ${source.title} ${source.location} ${source.url || ""}`,
          ),
        ]
      : []),
    ...(result.service_information
      ? ["\n## 医院与就医信息", result.service_information]
      : []),
  ]
    .filter(Boolean)
    .join("\n");
}

export function spokenText(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, "代码内容略。")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(?:#{1,6}|[-*>])\s+/gm, "")
    .replace(/[`*_~]/g, "")
    .replace(/\|/g, "，")
    .trim();
}
