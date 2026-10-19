export type User = { id: string; email: string };
export type HistoryItem = { id: string; title: string; updated_at: string };
export type Message = { role: "user" | "assistant"; content: string };
export type Source = {
  id: string; title: string; institution: string; url: string; text: string;
  kind: string; published_at?: string | null; collected_at?: string;
};
export type Claim = { text: string; evidence_id: string; quote: string };
export type Turn = {
  run_id: string; summary: string; intent?: string; department?: string;
  provider_mode?: "live" | "demo"; sources: Source[]; claims: Claim[];
  risk_notice?: string | null; limitation?: string; retrieval_notice?: string;
};
export type Session = {
  id: string; messages: Message[]; facts: Record<string, string>;
  last_turn: Turn | null; last_result: Turn | null;
};
export type Health = { provider_mode: "live" | "demo"; status?: string };
export type Integration = {
  provider_mode: string; retriever_class: string; planning_enabled: boolean;
  rerank_enabled: boolean; search_available: boolean; detailed_search_available: boolean; note: string;
};
export type RunEvent = {
  time?: string; stage: string; status?: string; tool?: string; mode?: string;
  source_count?: number; duration_ms?: number; error_code?: string;
};

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message: string, public status: number, public code = "", public runId = "") {
    super(message);
    this.name = "ApiError";
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 180_000): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(API_BASE + path, { ...init, credentials: "include", signal: controller.signal });
    if (response.status === 204) return undefined as T;
    let data: unknown;
    try { data = await response.json(); }
    catch { throw new ApiError("后端返回了无法解析的响应，请确认连接的是 FastAPI 服务。", response.status, "invalid_response"); }
    if (!response.ok) {
      const body = record(data) ? data : {};
      const message = typeof body.error === "string" ? body.error : typeof body.detail === "string" ? body.detail
        : response.status === 422 ? "输入字段不符合后端要求，请检查内容和接口。" : "请求失败，请查看后端日志。";
      throw new ApiError(message, response.status, typeof body.code === "string" ? body.code : "",
        typeof body.run_id === "string" ? body.run_id : "");
    }
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) throw new ApiError("等待响应超时；后端可能仍在处理，请刷新确认后再发送。", 0, "timeout");
    throw new ApiError("无法连接后端。请检查服务启动、API 地址和跨域配置。", 0, "network");
  } finally { clearTimeout(timeout); }
}

export function jsonBody(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function decodeTurn(value: unknown): Turn | null {
  if (value === null || value === undefined) return null;
  if (!record(value) || typeof value.run_id !== "string" || typeof value.summary !== "string"
      || !Array.isArray(value.sources) || !Array.isArray(value.claims)) {
    throw new ApiError("回答结构与页面约定不一致，请检查后端版本。", 200, "invalid_response");
  }
  if (!value.sources.every(s => record(s) && ["id", "title", "institution", "url", "text", "kind"].every(k => typeof s[k] === "string"))
      || !value.claims.every(c => record(c) && ["text", "evidence_id", "quote"].every(k => typeof c[k] === "string"))) {
    throw new ApiError("来源或引文结构不完整。", 200, "invalid_response");
  }
  return value as Turn;
}

export function decodeSession(value: unknown): Session {
  if (!record(value) || typeof value.id !== "string" || !Array.isArray(value.messages)
      || !value.messages.every(m => record(m) && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")) {
    throw new ApiError("会话响应与页面约定不一致，请检查后端版本。", 200, "invalid_response");
  }
  return { id: value.id, messages: value.messages as Message[],
    facts: record(value.facts) ? Object.fromEntries(Object.entries(value.facts).filter((pair): pair is [string, string] => typeof pair[1] === "string")) : {},
    last_turn: decodeTurn(value.last_turn), last_result: decodeTurn(value.last_result) };
}

export const chatApi = {
  health: () => request<Health>("/health", {}, 15_000),
  me: () => request<{ user: User | null }>("/auth/me", {}, 15_000),
  history: () => request<{ items: HistoryItem[] }>("/sessions", {}, 15_000),
  authenticate: (registering: boolean, email: string, password: string) =>
    request<User>(registering ? "/auth/register" : "/auth/login", jsonBody("POST", { email, password }), 30_000),
  logout: () => request<void>("/auth/logout", { method: "POST" }, 15_000),
  create: async () => decodeSession(await request<unknown>("/sessions", { method: "POST" }, 30_000)),
  read: async (id: string) => decodeSession(await request<unknown>(`/sessions/${encodeURIComponent(id)}`, {}, 15_000)),
  send: async (id: string, message: string) => decodeSession(await request<unknown>(
    `/sessions/${encodeURIComponent(id)}/messages`, jsonBody("POST", { message }))),
  integration: (token: string) => request<Integration>("/dev/chat-integration", { headers: { "X-Dev-Token": token } }, 15_000),
  events: (runId: string, token: string) => request<{ events: RunEvent[] }>(
    `/dev/runs/${encodeURIComponent(runId)}`, { headers: { "X-Dev-Token": token } }, 15_000),
};

export function sessionStorageKey(userId: string) { return "carebrief-v1-session-" + userId; }

export function cleanCitationIds(text: string) {
  return text.replace(/\[(?:KB-[A-Za-z0-9_-]+|(?:PUMCH|NHC|GENERAL|GD|WEB|SHANGJIN)-[A-Za-z0-9_-]+)\]/g, "");
}

export function sourceUrl(value: string): string | undefined {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : undefined; }
  catch { return undefined; }
}
