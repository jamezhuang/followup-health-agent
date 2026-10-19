export const API = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8002"
).replace(/\/+$/, "");
export type Account = { id: string; username: string; role: "admin" | "user" };
export type Subject = {
  id: string;
  name: string;
  relation: string;
  diseases: string[];
  constraints: string;
  revision: number;
  patient_name?: string;
  sex?: string;
  birth_date?: string;
  age?: string;
  medical_history?: string;
  allergies?: string;
  other_conditions?: string;
  additional_info?: string;
};
export type RecordRow = {
  id: string;
  subject: string;
  date: string;
  kind: string;
  label: string;
  value: string;
  unit: string;
  revision: number;
  reference_range?: string;
  source_document_id?: string;
};
export type Piece = { location: string; text: string };
export type DocumentRow = {
  original_filename?: string;
  index_summary?: { chunks: number; vector_chunks: number; persisted: boolean };
  kind?: string;
  filename?: string;
  id: string;
  subject: string;
  title: string;
  pieces: Piece[];
  diseases: string[];
  confirmed: boolean;
  report_type?: "inspection" | "followup" | null;
  url: string;
  institution: string;
  published_at: string;
  collected_at?: string;
  source_type?: "article" | "directory_snapshot";
  regions?: string[];
  rights: string;
  revision: number;
  status?: string;
  created_at?: string;
  followup_date?: string;
  key_fields?: ReportFields;
};
export type ReportObservation = {
  label: string;
  value: string;
  unit: string;
  reference_range: string;
  location: string;
  quote: string;
};
export type ReportMedication = {
  name: string;
  strength: string;
  dose: string;
  frequency: string;
  effective_date: string;
  location: string;
  quote: string;
};
export type DoctorTask = {
  content: string;
  due_date: string;
  location: string;
  quote: string;
};
export type ReportFields = {
  patient_name: string;
  examination_date: string;
  report_date: string;
  institution: string;
  summary: string;
  observations: ReportObservation[];
  medications: ReportMedication[];
  doctor_tasks: DoctorTask[];
  details: ReportDetails;
  additional_fields: AdditionalReportField[];
};
export type ReportDetails = Record<
  | "report_title"
  | "patient_sex"
  | "patient_age"
  | "department"
  | "patient_number"
  | "outpatient_number"
  | "inpatient_number"
  | "examination_number"
  | "imaging_number"
  | "application_number"
  | "ward"
  | "bed_number"
  | "examination_name"
  | "examination_body_part"
  | "examination_method"
  | "examination_modality"
  | "examination_technical_details"
  | "examination_original"
  | "examination_time"
  | "report_time"
  | "review_datetime"
  | "clinical_history"
  | "comparison_info"
  | "findings"
  | "impression"
  | "recommendations"
  | "referring_doctor"
  | "reporting_doctor"
  | "reviewing_doctor"
  | "technician",
  string
>;
export type AdditionalReportField = {
  label: string;
  value: string;
  location: string;
  quote: string;
};
export type ReportReview = {
  id: string;
  subject: string;
  revision: number;
  title: string;
  filename: string;
  report_type: "inspection" | "followup";
  status: string;
  pieces: Piece[];
  fields: ReportFields;
  update_current: boolean;
  save_tasks: boolean;
  expected_current_id: string;
  created_at: string;
  latest_information?: {
    id: string;
    summary: string;
    title: string;
    created_at: string;
  } | null;
  previous_information?: {
    id: string;
    summary: string;
    title: string;
    created_at: string;
  } | null;
};
export type Evidence = {
  id: string;
  title: string;
  location: string;
  text: string;
  url: string;
  kind: string;
  published_at?: string;
  document_id?: string;
  collected_at?: string;
};
export type Result = {
  material_notice?: string;
  material_status?: { has_unconfirmed_reports: boolean; document_ids: string[] };
  preparation_checklist?: {
    id: string;
    title: string;
    action: string;
    status: "provided" | "to_confirm" | "reminder";
    document_ids: string[];
  }[];
  preparation_scope_note?: string;
  patient_notes?: string[];
  preparation_suggestions?: string[];
  search_queries?: string[];
  search_sources?: Evidence[];
  // New responses have two primary branches; retain legacy values for saved conversations.
  intent?: "daily" | "followup" | "medical_education" | "hospital";
  consultation_type?: "general" | "medical_education" | "hospital";
  operation?:
    | "answer"
    | "compare"
    | "prepare"
    | "update"
    | "upload"
    | "archive"
    | "retrieve";
  kind?: "chat" | "followup";
  content?: string;
  citations?: {source_id:string;label:string;quote:string;url:string;title:string;location:string}[];
  citation_review?: {status:string;answer_kind?:string};
  service_information?: string;
  claims: { text: string; evidence_id: string; quote: string }[];
  questions: string[];
  gaps: string[];
  sources: Evidence[];
  records: RecordRow[];
  comparisons?: {
    label: string;
    unit: string;
    previous_date: string;
    previous_value: string;
    current_date: string;
    current_value: string;
    delta: string;
    source_ids: string[];
  }[];
  rejected_claims: number;
  mode: string;
  notice: string;
  iterations: number;
  stop_reason: string;
  stale?: boolean;
};
export type Message = {
  attachments?: {
    id: string;
    title: string;
    filename: string;
    temporary?: boolean;
    archived?: boolean;
  }[];
  role: string;
  content?: string;
  result?: Result;
  run_id?: string;
  duration_ms?: number | null;
  feedback?: "like" | "dislike" | null;
};
export type Conversation = {
  pending_attachments?: DocumentRow[];
  id: string;
  subject: string;
  title: string;
  messages: Message[];
  updated_at: string;
  revision: number;
};
export type Run = {
  kind?: "chat" | "followup";
  id: string;
  subject: string;
  status: string;
  conversation_id: string;
  result: Result | null;
  error_code: string;
  duration_ms: number;
};
export type Call = {
  id: string;
  run_id: string;
  agent: string;
  purpose: string;
  model: string;
  attempt: number;
  started_at: string;
  status: string;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  estimated_input_tokens?: number;
  estimate_method?: string;
  duration_ms?: number;
  failure_reason?: string;
};
export type TaskUsage = {calls:number;known_usage_calls:number;unknown_usage_calls:number;known_total_tokens:number;
  known_input_tokens:number;known_output_tokens:number;model_request_sum_ms:number;unknown_duration_calls:number;includes_retries:boolean;price_status:string};
export type TaskLookup = {task:{id:string;type:"run"|"import"|"publication"|"inspection";status:string;stage?:string;error_code?:string;attempts?:number;
  document_id?:string;target_revision?:number;completed_chunks?:number;total_chunks?:number;duration_ms?:number;citation_status?:string};
  usage:TaskUsage;usage_attribution:string;legacy_document_calls_present:boolean;lifecycle_elapsed_ms:number|null};
export type Dashboard = {
  knowledge_observability?: {
    queues: Record<"import"|"publication",Record<"queued"|"processing"|"completed"|"failed",number>> & {inspection?:Record<"queued"|"processing"|"completed"|"failed",number>};
    recent_failures: {id:string;type:"import"|"publication"|"inspection";error_code:string;attempts:number;updated_at:string}[];
    citations: {window:string;run_count:number;attempted:number;passed:number;pass_rate:number|null;
      statuses:Record<string,number>;answer_kinds:Record<string,number>;
      recent_failures:{id:string;status:string;updated_at:string}[]};
  };
  updated_at: string;
  window: string;
  metrics: {
    calls: number;
    success_rate: number | null;
    known_tokens: number;
    unknown_usage: number;
    p50_ms: number | null;
    p95_ms: number | null;
  };
  failures: Record<string, number>;
  agents: Record<string, number>;
  trend: { hour: string; count: number }[];
  calls: Call[];
  runs: {
    id: string;
    created_at: string;
    status: string;
    mode: string;
    duration_ms: number;
    error_code: string;
    diagnosis?: RunDiagnosis;
    resume_count?: number;
  }[];
};
export type RunDiagnosis = {
  outcome: string;
  reason: string;
  failed_stage: string;
  retrieval: Record<string, number | string | Record<string, number>>;
  resume_count: number;
};
export type Trace = {
  usage_summary?: TaskUsage;
  run_id?: string;
  diagnosis?: RunDiagnosis;
  checkpoints?: {
    step_id: string;
    stage: string;
    status: string;
    attempts: number;
    updated_at: string;
    checkpoint_id: string;
    error_code: string;
    duration_ms: number;
  }[];
  recovery?: {
    can_resume: boolean;
    resume_count: number;
    max_resumes: number;
    available: boolean;
  };
  stages: {
    time: string;
    agent: string;
    action: string;
    round?: number;
    reason?: string;
    stop_reason?: string;
    source_count?: number;
    browser_reads?: number;
    failed_pages?: number;
    status?: string;
    provider_attempts?: {
      provider: string;
      raw_count?: number;
      candidate_count?: number;
      error?: string;
      retryable?: boolean;
      http_status?: number | null;
    }[];
  }[];
  calls: Call[];
};
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const form = body instanceof FormData;
  const response = await fetch(API + path, {
    method,
    credentials: "include",
    cache: "no-store",
    signal,
    ...(body === undefined
      ? {}
      : {
          body: form ? body : JSON.stringify(body),
          headers: form ? {} : { "Content-Type": "application/json" },
        }),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "输入格式不正确或请求未完成，请检查后重试。",
    );
  return data as T;
}
export function safeUrl(url: string) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}
