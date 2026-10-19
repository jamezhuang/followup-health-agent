"use client";
import type { Trace } from "./v2-api";
import s from "./v2.module.css";

export const answerStatuses: Record<string, string> = {
  answered: "已回答",
  partial: "部分回答",
  unanswered: "未能回答",
  execution_error: "执行故障",
  needs_input: "待补充 / 确认",
  running: "执行中",
};
export const stageLabels: Record<string, string> = {
  intent_analysis: "意图识别",
  hospital_retrieval: "医院资料检索",
  public_retrieval: "公共知识检索",
  hospital_query_plan: "医院查询规划",
  knowledge_query_plan: "公共知识查询规划",
  search_web: "外部搜索",
  verify_source: "来源核验",
  read_webpage: "网页正文读取",
  map_website: "页面链接发现",
  response_generation: "回答生成与核对",
  report_answer: "报告问答",
  followup_processing: "复诊资料处理",
  evidence_filter: "证据筛选",
  coverage_review: "覆盖判断",
  answer_review: "回答核对",
  private_retrieval: "授权资料读取",
  execution: "任务执行",
};
const reasons: Record<string, string> = {
  answer_completed: "处理已完成",
  execution_in_progress: "正在执行",
  service_unavailable: "搜索服务未正常响应",
  unconfigured: "搜索通道未配置",
  disabled: "联网功能未启用",
  unreadable: "候选网页未取得可用正文",
  insufficient_evidence: "已读取的资料不足以支持当前问题",
  no_results: "搜索成功但没有匹配结果",
  no_authorized_material: "没有可用于本次请求的已确认资料",
  external_search_declined: "按用户要求未联网，暂时无法核实所问信息",
  insufficient_coverage: "答案或资料覆盖不足",
  answer_review_unavailable: "回答核对服务未完成",
  answer_review_fallback: "回答核对未通过，已降级展示有依据内容",
  workflow_failed: "执行阶段异常",
  workflow_timeout: "任务超时",
  process_interrupted: "服务进程中断",
  request_cancelled: "用户停止任务",
  needs_clarification: "需要用户补充信息",
  confirmation_required: "等待用户核对确认",
  context_budget: "上下文超过处理范围",
};
const metrics: Record<string, string> = {
  returned_candidates: "返回候选",
  rejected_candidates: "筛除候选",
  duplicate_candidates: "重复候选",
  direct_read_attempts: "普通读取",
  browser_read_attempts: "浏览器读取",
  read_successes: "读取成功",
  read_failures: "读取失败",
  extracted_sources: "取得片段",
  rejected_sources: "初筛待进一步核对",
  semantic_matches: "语义核对补回",
  final_source_count: "最终可用来源",
  confirmed_institutions: "有相关诊疗证据的医院",
  related_institutions: "仅确认相关科室的医院",
};
const readErrors: Record<string, string> = {
  page_http_error: "网页 HTTP 请求失败",
  page_timeout: "读取超时",
  page_network_error: "网页连接失败",
  text_too_short: "正文过短",
  dns_failed: "域名解析失败",
  document_timeout: "文档处理超时",
  browser_failed: "浏览器读取失败",
  browser_timeout: "浏览器超时",
  other_read_error: "其他读取错误",
  page_unavailable: "页面不可用",
  unsupported_content_type: "不支持的内容格式",
};

export default function RunDiagnosisPanel({
  trace,
  busy,
  onResume,
}: {
  trace: Trace;
  busy: boolean;
  onResume: () => void;
}) {
  const diagnosis = trace.diagnosis;
  if (!diagnosis) return null;
  return (
    <article className={s.card} aria-label="任务故障诊断">
      <div className={s.toolbar}>
        <h3>
          答复结果：{answerStatuses[diagnosis.outcome] || diagnosis.outcome}
        </h3>
        {trace.recovery?.can_resume && (
          <button className={s.primary} disabled={busy} onClick={onResume}>
            {busy ? "正在提交恢复…" : "从检查点恢复 / 重试失败步骤"}
          </button>
        )}
      </div>
      <p>原因：{reasons[diagnosis.reason] || diagnosis.reason}</p>
      {diagnosis.failed_stage && (
        <p>
          定位阶段：
          <strong>
            {stageLabels[diagnosis.failed_stage] || diagnosis.failed_stage}
          </strong>
        </p>
      )}
      <p className={s.muted}>
        任务 ID：<code>{trace.run_id}</code> · 已恢复{" "}
        {trace.recovery?.resume_count ?? diagnosis.resume_count} /{" "}
        {trace.recovery?.max_resumes ?? 2} 次
      </p>
      <p className={s.muted}>
        这是按执行记录推断的故障位置；“执行完成”不代表回答完整或医学结论已被认证。
      </p>
      {trace.recovery && (
        <p className={s.muted}>
          {trace.recovery.available
            ? "检查点有效期24小时。恢复会复用成功步骤，给失败步骤一次新的有界执行机会；不会重复追加同一轮对话。资料或对话已变化时会拒绝恢复。"
            : "该任务没有可恢复的有效检查点；历史任务可查看已有链路，但需要重新提问才能生成检查点。"}
        </p>
      )}
      {Object.keys(diagnosis.retrieval).length > 0 && (
        <>
          <h4>检索损耗</h4>
          <div className={s.tableWrap}>
            <table className={s.table}>
              <thead>
                <tr>
                  <th>环节</th>
                  <th>数量</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(metrics)
                  .filter(
                    ([key]) => typeof diagnosis.retrieval[key] === "number",
                  )
                  .map(([key, label]) => (
                    <tr key={key}>
                      <td>{label}</td>
                      <td>{String(diagnosis.retrieval[key])}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {typeof diagnosis.retrieval.page_errors === "object" &&
            Object.entries(diagnosis.retrieval.page_errors).map(
              ([code, count]) => (
                <p key={code}>
                  {readErrors[code] || code}：{count} 次
                </p>
              ),
            )}
          {diagnosis.retrieval.stop_reason && (
            <p>检索停止原因：{String(diagnosis.retrieval.stop_reason)}</p>
          )}
        </>
      )}
      <h4>持久化阶段检查点</h4>
      {trace.checkpoints?.length ? (
        <div className={s.tableWrap}>
          <table className={s.table} aria-label="持久化阶段检查点">
            <thead>
              <tr>
                <th>阶段</th>
                <th>状态</th>
                <th>阶段执行次数</th>
                <th>最近耗时</th>
                <th>检查点</th>
              </tr>
            </thead>
            <tbody>
              {trace.checkpoints.map((step) => (
                <tr key={step.step_id}>
                  <td>{stageLabels[step.stage] || step.stage}</td>
                  <td>
                    {{
                      completed: "已完成",
                      failed: "失败",
                      running: "执行中",
                      interrupted: "已中断",
                    }[step.status] || step.status}
                    {step.error_code &&
                      ` · ${({ stage_failed: "阶段异常", stage_interrupted: "阶段中断", process_interrupted: "进程中断", tool_failed: "工具执行失败", all_search_providers_failed: "搜索服务均失败", page_timeout: "读取超时", page_http_error: "网页HTTP错误", page_network_error: "网页连接失败", text_too_short: "正文过短" } as Record<string, string>)[step.error_code] || step.error_code}`}
                  </td>
                  <td>{step.attempts}</td>
                  <td>{step.duration_ms} ms</td>
                  <td>
                    <code title={step.checkpoint_id}>
                      {step.checkpoint_id?.slice(0, 12) || "待完成 / 失败阶段"}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className={s.muted}>尚无阶段检查点。</p>
      )}
    </article>
  );
}
