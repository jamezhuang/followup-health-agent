export const API = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
export type User = {id: string; email: string; username: string; role: "admin" | "user"};
export type Source = {id: string; title: string; institution: string; url: string; text: string; kind: string; collected_at?: string; published_at?: string | null};
export type Turn = {run_id: string; summary: string; department?: string; sources: Source[]; claims: {text: string; evidence_id: string; quote: string}[]; risk_notice?: string; follow_up_questions?: string[]; limitation?: string};
export type Consultation = {id: string; messages: {role: string; content: string}[]; facts: Record<string,string>; last_turn?: Turn | null; last_result?: Turn | null};
export type History = {id: string; title: string; updated_at: string};
export type Piece = {location: string; text: string};
export type Doc = {id: string; name: string; suffix: string; size: number; status: string; revision: number; error_code: string; chunk_count: number; created_at: string; pieces?: Piece[]};
export type Profile = Record<string, {value: string; source: string; confirmed_at: string}>;
export type Run = {id: string; session_id: string; status: string; error_code: string; duration_ms: number; created_at: string};
export type Event = {time?: string; stage: string; status?: string; tool?: string; error_code?: string; duration_ms?: number; source_count?: number};
export type RunDetail = {id: string; status: string; error_code: string; events: Event[]; analysis: {error_code:string; category:string; suggestion:string}[]};

export class ApiError extends Error {
  constructor(message: string, public runId = "", public code = "", public status = 0) {super(message);}
}

export async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 190000);
  try {
    const form = body instanceof FormData;
    const response = await fetch(API + path, {method, credentials: "include", cache: "no-store", signal: controller.signal,
      ...(body === undefined ? {} : {body: form ? body : JSON.stringify(body),
        headers: form ? {} : {"Content-Type": "application/json"}})});
    if (response.status === 204) return undefined as T;
    const data = await response.json();
    if (!response.ok) throw new ApiError(typeof data.detail === "string" ? data.detail : data.error || "请求未完成，请检查输入或后端状态。", data.run_id || "", data.code || "", response.status);
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(controller.signal.aborted ? "等待超时，后端可能仍在处理；请刷新会话后确认。" : "无法连接后端，请检查启动和跨域配置。", "", controller.signal.aborted ? "timeout" : "network");
  } finally {clearTimeout(timer);}
}

export function sourceHref(source: Source): string | undefined {
  if (source.kind === "private_document" && /^\/workspace\/documents\/[a-f0-9]{32}\/file$/.test(source.url)) return API + source.url;
  try {const u = new URL(source.url); return ["https:","http:"].includes(u.protocol) ? u.href : undefined;} catch {return undefined;}
}

export function cleanText(text: string): string {
  return text.replace(/\[(?:KB|PUMCH|NHC|GD|WEB|GENERAL|PRIV|SHANGJIN)-[A-Za-z0-9_-]+\]/g, "");
}

export const statusNames: Record<string,string> = {
  queued:"等待解析", parsing:"正在解析", awaiting_confirmation:"等待确认", indexing:"建立索引中",
  ready:"可用于咨询", failed:"处理失败", running:"运行中", ok:"已完成",
};
export const errorNames: Record<string,string> = {
  ocr_required:"存在扫描页或无正文页面，需要 OCR 后重新上传。", utf8_required:"请将文本文件另存为 UTF-8。",
  parse_failed:"解析失败，请检查文件是否损坏。", index_interrupted:"服务重启中断任务，可点击重试。",
  index_model_changed:"模型配置改变，需恢复配置或重新确认建立索引。", missing_key:"未配置模型密钥。",
  encrypted_pdf:"请先解除 PDF 密码。", provider_http:"模型服务请求失败，请检查配置和额度。",
};
