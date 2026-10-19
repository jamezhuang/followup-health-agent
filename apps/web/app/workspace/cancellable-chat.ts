import {API, ApiError, request} from "./workspace-api";
import type {Consultation} from "./workspace-api";

export type StopResult = {status:"cancelled"|"stopping"|"already_finished"|"failed"; run_id:string};

export function stopMessage(sessionId:string, requestId:string) {
  return request<StopResult>(`/sessions/${encodeURIComponent(sessionId)}/requests/${requestId}/cancel`,"POST");
}

export async function sendMessage(sessionId:string, requestId:string, message:string, signal:AbortSignal):Promise<Consultation> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort",abort,{once:true});
  if(signal.aborted)abort();
  const timer = setTimeout(abort,190000);
  try {
    const response = await fetch(`${API}/sessions/${encodeURIComponent(sessionId)}/messages?request_id=${requestId}`, {
      method:"POST", credentials:"include", cache:"no-store", signal:controller.signal,
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({message}),
    });
    const data = await response.json();
    if(!response.ok)throw new ApiError(data.error || data.detail || "本轮未完成。",data.run_id || "",data.code || "",response.status);
    return data as Consultation;
  } catch(error) {
    if(error instanceof ApiError)throw error;
    if(signal.aborted)throw new ApiError("已停止本次生成。","","request_cancelled");
    throw new ApiError(controller.signal.aborted ? "等待超时，请刷新会话核对后继续。" : "无法连接后端，请刷新会话核对。",
      "",controller.signal.aborted ? "timeout" : "network");
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort",abort);
  }
}
