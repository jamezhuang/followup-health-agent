"use client";
import type { ReportFields, ReportDetails } from "./v2-api";
import s from "./v2.module.css";

type Key = keyof ReportDetails;
type Entry = readonly [Key, string, number];
const groups: {
  title: string;
  help: string;
  fields: Entry[];
  automatic?: boolean;
}[] = [
  {
    title: "患者与就诊信息",
    automatic: true,
    help: "按本份报告填写，年龄保留报告记载，不推算出生日期。",
    fields: [
      ["patient_sex", "报告性别", 20],
      ["patient_age", "报告年龄", 40],
      ["department", "科室", 200],
      ["patient_number", "患者 / 病人编号", 100],
      ["outpatient_number", "门诊号", 100],
      ["inpatient_number", "住院号", 100],
      ["ward", "病区", 100],
      ["bed_number", "床号", 100],
    ],
  },
  {
    title: "检查项目与编号",
    help: "CT、MR、超声、检验等报告均可核对；报告未提供的项目显示“无”。",
    fields: [
      ["report_title", "报告标题", 200],
      ["examination_name", "检查项目", 500],
      ["examination_number", "检查号", 100],
      ["imaging_number", "影像号", 100],
      ["application_number", "申请单号", 100],
      ["examination_time", "检查时刻", 40],
      ["report_time", "报告时刻", 40],
    ],
  },
  {
    title: "检查拆分信息",
    automatic: true,
    help: "后台按原文分别保存，不要求患者重复核对。",
    fields: [
      ["examination_body_part", "检查部位", 300],
      ["examination_method", "检查方式", 500],
      ["examination_modality", "检查类型", 40],
      ["examination_technical_details", "扫描技术描述", 1500],
      ["examination_original", "原始检查项目", 1500],
    ],
  },
  {
    title: "报告正文与意见",
    help: "保留报告原文中的所见、意见及建议；“无”仅表示未提供该项。",
    fields: [
      ["clinical_history", "临床病史 / 检查原因", 4000],
      ["comparison_info", "对比旧片 / 既往检查", 2000],
      ["findings", "影像表现 / 检查所见", 16000],
      ["impression", "报告意见 / 印象 / 结论", 8000],
      ["recommendations", "报告建议", 4000],
    ],
  },
  {
    title: "医生与签发信息",
    help: "签名无法辨认时保留为空，不猜测姓名。",
    fields: [
      ["referring_doctor", "申请医生", 100],
      ["reporting_doctor", "报告医生", 100],
      ["reviewing_doctor", "审核医生", 100],
      ["technician", "技师", 100],
      ["review_datetime", "审核日期 / 时间", 100],
    ],
  },
];
export const emptyReportDetails = Object.fromEntries(
  groups.flatMap((group) => group.fields.map(([key]) => [key, ""])),
) as ReportDetails;

export function ReportDetailsSummary({ fields }: { fields: ReportFields }) {
  return (
    <div>
      {groups
        .filter((group) => !group.automatic)
        .map((group) => (
          <section className={s.reportSection} key={group.title}>
            <h3>{group.title}</h3>
            <dl className={s.reportReadOnly}>
              {group.fields.map(([key, label]) => (
                <div key={key}>
                  <dt>{label}</dt>
                  <dd>{fields.details?.[key] || "无"}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
    </div>
  );
}

export default function ReportDetailsEditor({
  fields,
  onChange,
}: {
  fields: ReportFields;
  onChange: (fields: ReportFields) => void;
}) {
  const details = { ...emptyReportDetails, ...fields.details };
  return (
    <>
      {groups
        .filter((group) => !group.automatic)
        .map((group) => (
          <section className={s.reportSection} key={group.title}>
            <div className={s.reportSectionTitle}>
              <h3>{group.title}</h3>
              <p>{group.help}</p>
            </div>
            <div className={s.reviewGrid}>
              {group.fields.map(([key, label, limit]) => (
                <label
                  className={`${s.field} ${limit >= 2000 ? s.reportWideField : ""}`}
                  key={key}
                >
                  <span className={s.reportFieldHeading}>{label}</span>
                  {limit >= 2000 ? (
                    <textarea
                      aria-label={label}
                      rows={key === "findings" || key === "impression" ? 5 : 2}
                      placeholder="无"
                      maxLength={limit}
                      value={details[key]}
                      onChange={(e) =>
                        onChange({
                          ...fields,
                          details: { ...details, [key]: e.target.value },
                        })
                      }
                    />
                  ) : (
                    <input
                      aria-label={label}
                      placeholder="无"
                      maxLength={limit}
                      value={details[key]}
                      onChange={(e) =>
                        onChange({
                          ...fields,
                          details: { ...details, [key]: e.target.value },
                        })
                      }
                    />
                  )}
                </label>
              ))}
            </div>
          </section>
        ))}
    </>
  );
}
