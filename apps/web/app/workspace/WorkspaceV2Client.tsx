"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import AssistantWelcome, {
  SidebarToggle,
  useConsultationLayout,
} from "../components/AssistantWelcome";
import { api } from "./v2-api";
import type {
  Account,
  Subject,
  Conversation,
  Message,
  Run,
  Result,
  DocumentRow,
} from "./v2-api";
import { HealthPanel, AdminPanel, ResultCard, diseases } from "./V2Panels";
import s from "./v2.module.css";
import ChatAttachments from "./ChatAttachments";
import type { AttachmentHandle } from "./ChatAttachments";
import AnswerActions from "./AnswerActions";
import SourceSummary from "./SourceSummary";
import SpeechInput from "./SpeechInput";
import MessageAttachments from "./MessageAttachments";
import ArchiveAttachmentDialog from "./ArchiveAttachmentDialog";
import { answerText } from "./answer-text";

type Tab = "chat" | "profile" | "runs";
const tabs: Record<Tab, string> = {
  chat: "慢病复诊",
  profile: "复诊档案",
  runs: "后台管理",
};

function exportResult(result: Result) {
  const text = answerText(result);
  const url = URL.createObjectURL(
    new Blob([text], { type: "text/markdown;charset=utf-8" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = "复诊准备摘要.md";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function WorkspaceV2Client() {
  const [user, setUser] = useState<Account | null>(null),
    [boot, setBoot] = useState(true),
    [error, setError] = useState("");
  const [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    [register, setRegister] = useState(false),
    [busy, setBusy] = useState(false);
  const fail = useCallback(
    (e: unknown) =>
      setError(e instanceof Error ? e.message : "请求未完成，请重试。"),
    [],
  );
  useEffect(() => {
    let alive = true;
    api<{ user: Account | null }>("/auth/me")
      .then((x) => {
        if (alive) setUser(x.user);
      })
      .catch(fail)
      .finally(() => {
        if (alive) setBoot(false);
      });
    return () => {
      alive = false;
    };
  }, [fail]);
  async function login(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const account = await api<Account>(
        register ? "/auth/register" : "/auth/login",
        "POST",
        { username, password },
      );
      setPassword("");
      setUser(account);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  if (boot) return <div className={s.empty}>正在连接 v2 复诊工作台…</div>;
  if (!user)
    return (
      <main className={s.login}>
        <section className={s.loginIntro}>
          <AssistantWelcome
            onChoose={() =>
              document
                .querySelector<HTMLInputElement>(
                  'input[autocomplete="username"]',
                )
                ?.focus()
            }
          />
          <h1>把零散记录，整理成一次有准备的复诊。</h1>
          <p className={s.muted}>多病种记录 · 个人资料核对 · 可追溯证据</p>
        </section>
        <form onSubmit={login}>
          <span className={s.badge}>CareBrief V2</span>
          <h2>{register ? "创建账号" : "登录复诊工作台"}</h2>
          <p className={s.muted}>
            账号和密码均为 6–20 位字母或数字，允许纯字母、纯数字。
          </p>
          <label className={s.field}>
            账号
            <input
              autoComplete="username"
              required
              minLength={6}
              maxLength={20}
              pattern="[A-Za-z0-9]{6,20}"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label className={s.field}>
            密码
            <input
              type="password"
              autoComplete={register ? "new-password" : "current-password"}
              required
              minLength={6}
              maxLength={20}
              pattern="[A-Za-z0-9]{6,20}"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <p className={s.error} role="alert">
              {error}
            </p>
          )}
          <div className={s.row}>
            <button className={s.primary} disabled={busy}>
              {busy ? "处理中…" : register ? "注册并登录" : "登录"}
            </button>
            <button
              type="button"
              className={s.secondary}
              onClick={() => {
                setRegister(!register);
                setError("");
              }}
            >
              {register ? "已有账号" : "创建用户账号"}
            </button>
          </div>
        </form>
      </main>
    );
  return (
    <SignedIn
      key={user.id}
      user={user}
      onLogout={async () => {
        try {
          await api("/auth/logout", "POST");
          setUser(null);
          setError("");
        } catch (e) {
          fail(e);
        }
      }}
    />
  );
}

function SignedIn({
  user,
  onLogout,
}: {
  user: Account;
  onLogout: () => Promise<void>;
}) {
  const layout = useConsultationLayout();
  const [tab, setTab] = useState<Tab>("chat"),
    [error, setError] = useState("");
  const [subjects, setSubjects] = useState<Subject[]>([]),
    [subjectId, setSubjectId] = useState("");
  const [history, setHistory] = useState<Conversation[]>([]),
    [conversation, setConversation] = useState<Conversation | null>(null);
  const [search, setSearch] = useState(""),
    [question, setQuestion] = useState("");
  const [active, setActive] = useState<Run | null>(null),
    [submitting, setSubmitting] = useState(false);
  const [pendingGoal, setPendingGoal] = useState("");
  const [pendingFiles, setPendingFiles] = useState<Message["attachments"]>([]);
  const [archiveFile, setArchiveFile] = useState<string | null>(null);
  const [archivedFiles, setArchivedFiles] = useState<string[]>([]);
  const [attachmentsPending, setAttachmentsPending] = useState(false);
  const [speechBusy, setSpeechBusy] = useState(false);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const dragDepth = useRef(0);
  const attachmentInput = useRef<AttachmentHandle>(null);
  const submittedGoal = useRef("");
  const [documents, setDocuments] = useState<DocumentRow[]>([]),
    [selected, setSelected] = useState<string[]>([]);
  const messages = useRef<HTMLDivElement>(null),
    input = useRef<HTMLTextAreaElement>(null);
  const subject = subjects.find((x) => x.id === subjectId) || null;
  const running = submitting || active?.status === "running";
  const fail = useCallback(
    (e: unknown) => setError(e instanceof Error ? e.message : "操作未完成。"),
    [],
  );
  const refreshSubjects = useCallback(async () => {
    const rows = (await api<{ items: Subject[] }>("/v2/subjects")).items;
    setSubjects(rows);
    const remembered =
      localStorage.getItem(`carebrief-v2-subject:${user.id}`) || "";
    setSubjectId((old) =>
      rows.some((x) => x.id === old)
        ? old
        : rows.some((x) => x.id === remembered)
          ? remembered
          : rows[0]?.id || "",
    );
  }, [user.id]);
  useEffect(() => {
    if (subjectId)
      localStorage.setItem(`carebrief-v2-subject:${user.id}`, subjectId);
  }, [subjectId, user.id]);
  const refreshHistory = useCallback(
    async () =>
      setHistory(
        (await api<{ items: Conversation[] }>("/v2/conversations")).items,
      ),
    [],
  );
  useEffect(() => {
    void Promise.all([refreshSubjects(), refreshHistory()]).catch(fail);
    const initial = new URLSearchParams(location.search).get("tab");
    if (initial === "tracking") {
      setTab("chat");
      const url = new URL(location.href);
      url.searchParams.set("tab", "chat");
      window.history.replaceState({}, "", url);
    }
    if (
      initial &&
      initial in tabs &&
      (initial !== "runs" || user.role === "admin")
    )
      setTab(initial as Tab);
  }, [refreshSubjects, refreshHistory, fail, user.role]);
  useEffect(() => {
    let alive = true;
    setDocuments(old => old.filter(d => d.kind === "chat_attachment" && d.subject === subjectId));
    if (subjectId)
      api<{ items: DocumentRow[] }>(`/v2/documents?subject=${subjectId}`)
        .then((x) => {
          if (alive) setDocuments(old => [...old.filter(d => d.kind === "chat_attachment" && d.subject === subjectId), ...x.items.filter((x) => x.confirmed)]);
        })
        .catch(fail);
    return () => {
      alive = false;
    };
  }, [subjectId, tab, fail]);
  useEffect(() => {
    messages.current?.scrollTo({
      top: messages.current.scrollHeight,
      behavior: "instant",
    });
  }, [conversation?.messages.length, active?.status]);
  useEffect(() => {
    if (!active || active.status !== "running") return;
    let alive = true,
      timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const row = await api<Run>(`/v2/runs/${active!.id}`);
        if (!alive) return;
        setActive(row);
        if (row.status !== "running") {
          setConversation(
            await api<Conversation>(`/v2/conversations/${row.conversation_id}`),
          );
          await refreshHistory();
          if (row.status !== "completed") setQuestion(submittedGoal.current);
          setPendingGoal("");
          if (row.status === "failed" || row.status === "interrupted")
            setError(
              "本次整理未完成，请稍后重试。已保存的资料仍可在档案中查阅。",
            );
          return;
        }
      } catch (e) {
        if (alive) fail(e);
      }
      if (alive) timer = setTimeout(() => void poll(), 1000);
    }
    void poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [active?.id, active?.status, refreshHistory, fail]);
  function navigate(next: Tab) {
    if (attachmentsPending && next !== "chat") {
      setError("请等待附件读取完成后再离开对话。");
      return;
    }
    if (next === "runs" && user.role !== "admin") return;
    setTab(next);
    setError("");
    const url = new URL(location.href);
    url.searchParams.set("tab", next);
    window.history.replaceState({}, "", url);
  }
  function chooseSubject(id: string) {
    if (running || attachmentsPending || speechBusy) return;
    setSubjectId(id);
    if (!id) localStorage.removeItem(`carebrief-v2-subject:${user.id}`);
    setConversation(null);
    setActive(null);
    setQuestion("");
    setSelected([]);
  }
  function newConversation() {
    if (running || attachmentsPending || speechBusy) return;
    setConversation(null);
    setActive(null);
    setQuestion("");
    setSelected([]);
    navigate("chat");
  }
  async function openConversation(row: Conversation) {
    if (running || attachmentsPending || speechBusy) return;
    try {
      const found = await api<Conversation>(`/v2/conversations/${row.id}`);
      setSubjectId(found.subject);
      setConversation(found);
      setDocuments(old => [...(found.pending_attachments || []), ...old.filter(d => !(found.pending_attachments || []).some(p => p.id === d.id))]);
      setSelected((found.pending_attachments || []).map(d => d.id));
      setActive(null);
      navigate("chat");
    } catch (e) {
      fail(e);
    }
  }
  async function send(e?: FormEvent) {
    e?.preventDefault();
    const goal =
      question.trim() ||
      (selected.length
        ? "请解读本轮上传的报告，说明检查项目、主要结果及报告意见。"
        : "");
    if (!goal || running || attachmentsPending || speechBusy) return;
    if (goal.length > 4000) {
      setError("消息超过4000字，请精简后再发送。");
      return;
    }
    if (selected.length > 10) {
      setError("本轮最多选用10份资料，请取消部分附件后发送。");
      return;
    }
    setError("");
    setSubmitting(true);
    submittedGoal.current = goal;
    setPendingGoal(goal);
    setPendingFiles(selected.map(id => {
      const document = documents.find(d => d.id === id);
      return { id, title: document?.title || "报告附件", filename: document?.filename || document?.title || "报告附件", temporary: document?.kind === "chat_attachment" };
    }));
    try {
      const row = await api<Run>("/v2/runs", "POST", {
        subject: subject?.id || "",
        conversation_id: conversation?.id || "",
        goal,
        document_ids: selected,
      });
      setActive(row);
      setSelected([]);
      setQuestion("");
      await refreshHistory();
    } catch (e) {
      setPendingGoal("");
      fail(e);
    } finally {
      setSubmitting(false);
    }
  }
  async function cancel() {
    if (!active) return;
    try {
      setActive(await api<Run>(`/v2/runs/${active.id}/cancel`, "POST"));
      setQuestion(submittedGoal.current);
      setPendingGoal("");
    } catch (e) {
      fail(e);
    }
  }
  async function regenerateAnswer(message: Message, index: number) {
    if (running || attachmentsPending || !message.run_id || !conversation)
      return;
    const goal = conversation.messages[index - 1]?.content || "";
    setSubmitting(true);
    setError("");
    submittedGoal.current = goal;
    try {
      const row = await api<Run>(
        `/v2/runs/${message.run_id}/regenerate`,
        "POST",
      );
      const branch = await api<Conversation>(
        `/v2/conversations/${row.conversation_id}`,
      );
      setConversation(branch);
      setActive(row);
      setPendingGoal(goal);
      setSelected([]);
      await refreshHistory();
    } catch (error) {
      setPendingGoal("");
      fail(error);
    } finally {
      setSubmitting(false);
    }
  }
  const examples = [
    "帮我整理这次复诊需要带的资料。",
    "对比上次与本次检查，列出变化和缺项。",
    "把近期症状和用药记录整理给医生。",
  ];
  return (
    <div className={s.app} data-collapsed={layout.collapsed}>
      {!layout.collapsed && (
        <aside className={s.side}>
          <div className={s.brand}>
            <span className={s.logo} role="img" aria-label="小熊健康助手">
              <img
                src="/images/bear-assistant.png"
                alt=""
                aria-hidden="true"
                width="84"
                height="84"
              />
            </span>
            <div>
              <strong>CareBrief</strong>
              <small>让健康对话，更自然 · V2</small>
            </div>
            <SidebarToggle
              collapsed={false}
              onClick={layout.toggleSidebar}
              controls="v2-sidebar-content"
            />
          </div>
          <button
            className={s.newButton}
            disabled={!!running || attachmentsPending}
            onClick={newConversation}
          >
            <svg
              width="21"
              height="21"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z" />
            </svg>
            新对话
          </button>
          <input
            className={s.search}
            type="search"
            aria-label="搜索对话"
            placeholder="搜索对话"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <section
            className={s.histories}
            id="v2-sidebar-content"
            aria-label="历史对话"
          >
            <small>复诊对话</small>
            {history
              .filter((h) => h.title.includes(search))
              .map((row) => (
                <div
                  className={s.history}
                  key={row.id}
                  data-active={conversation?.id === row.id}
                >
                  <button
                    disabled={!!running || attachmentsPending}
                    title={row.title}
                    onClick={() => void openConversation(row)}
                  >
                    ◯　{row.title}
                  </button>
                  <button
                    className={s.iconButton}
                    disabled={!!running || attachmentsPending}
                    aria-label={`删除对话 ${row.title}`}
                    onClick={async () => {
                      if (!confirm("删除此对话及其复诊摘要？")) return;
                      try {
                        await api(`/v2/conversations/${row.id}`, "DELETE");
                        if (conversation?.id === row.id) newConversation();
                        await refreshHistory();
                      } catch (e) {
                        fail(e);
                      }
                    }}
                  >
                    ×
                  </button>
                </div>
              ))}
            {!history.length && (
              <p className={s.muted}>开始一次复诊准备后，对话会保存在这里。</p>
            )}
          </section>
          <div className={s.account}>
            <span>
              <strong>{user.username}</strong>
              <small>{user.role === "admin" ? "管理员" : "普通用户"}</small>
            </span>
            <button
              className={s.iconButton}
              disabled={!!running || attachmentsPending}
              onClick={() => void onLogout()}
            >
              退出
            </button>
          </div>
        </aside>
      )}
      <main className={s.main}>
        <header className={s.header}>
          <div className={s.title}>
            {layout.collapsed && (
              <SidebarToggle
                collapsed
                onClick={layout.toggleSidebar}
                controls="v2-sidebar-content"
              />
            )}
            {tab === "chat" ? (
              <div className={s.headerContext}>
                {subjects.length > 0 && <label>
                  复诊对象
                  <select
                    aria-label="选择复诊对象"
                    disabled={!!running || attachmentsPending}
                    value={subjectId}
                    onChange={(e) => chooseSubject(e.target.value)}
                  >
                    <option value="">请选择</option>
                    {subjects.map((x) => (
                      <option value={x.id} key={x.id}>
                        {x.name}{x.relation ? ` · ${x.relation}` : ""}
                      </option>
                    ))}
                  </select>
                </label>}
                {subject && (
                  <span
                    title={subject.diseases.map((d) => diseases[d]).join(" · ")}
                  >
                    {subject.diseases.map((d) => diseases[d]).join(" · ")}
                  </span>
                )}
              </div>
            ) : (
              <button
                className={`${s.iconButton} ${s.backToChat}`}
                onClick={() => navigate("chat")}
              >
                <span aria-hidden="true">‹</span>返回对话
              </button>
            )}
          </div>
          <nav className={s.nav} aria-label="工作台导航">
            <button
              aria-current={tab === "profile" ? "page" : undefined}
              onClick={() => navigate("profile")}
            >
              复诊档案
            </button>
            {user.role === "admin" && (
              <button
                aria-current={tab === "runs" ? "page" : undefined}
                onClick={() => navigate("runs")}
              >
                后台管理
              </button>
            )}
          </nav>
        </header>
        {error && (
          <div className={s.error} role="alert">
            {error}
            <button
              className={s.iconButton}
              aria-label="关闭错误提示"
              onClick={() => setError("")}
            >
              ×
            </button>
          </div>
        )}
        {tab === "chat" ? (
          <section className={`${s.chat} ${!conversation?.messages.length && !running ? s.chatEmpty : ""}`}>
            <div ref={messages} className={s.messages}>
              <div className={s.column}>
                {!conversation?.messages.length && !running && (
                  <div className={s.welcome}>
                    <AssistantWelcome
                      onChoose={(text) => {
                        setQuestion(text);
                        input.current?.focus();
                      }}
                    />
                    <div className={s.chips}>
                      {examples.map((text) => (
                        <button
                          key={text}
                          onClick={() => {
                            setQuestion(text);
                            input.current?.focus();
                          }}
                        >
                          {text}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {conversation?.messages.map((m: Message, i) =>
                  m.role === "user" ? (
                    <article key={i} className={`${s.message} ${s.user}`} aria-label="你的消息">
                      <MessageAttachments files={m.attachments?.map(file => ({ ...file, archived: file.archived || archivedFiles.includes(file.id) }))}
                        onArchive={setArchiveFile} disabled={!!running || attachmentsPending || speechBusy} />
                      <div className={s.userBubble}>{m.content}</div>
                    </article>
                  ) : m.result ? (
                    <article className={s.message} key={i}>
                      <SourceSummary result={m.result} />
                      <small>
                        {m.result.kind === "chat"
                          ? "CareBrief"
                          : m.result.preparation_checklist?.length ? "CareBrief · 复诊准备" : "CareBrief · 资料整理"}
                      </small>
                      <ResultCard
                        result={m.result}
                        onExport={() => exportResult(m.result!)}
                      />
                      <AnswerActions
                        message={m}
                        disabled={!!running || attachmentsPending || speechBusy}
                        onRegenerate={() => void regenerateAnswer(m, i)}
                      />
                    </article>
                  ) : null,
                )}
                {running && pendingGoal && (
                  <article className={`${s.message} ${s.user}`} aria-label="你的消息">
                    <MessageAttachments files={pendingFiles} />
                    <div className={s.userBubble}>{pendingGoal}</div>
                  </article>
                )}
                {running && (
                  <div className={s.status} role="status">
                    {active?.kind === "chat"
                      ? "正在回复…"
                      : "正在处理你的消息…"}
                  </div>
                )}
                {active?.status === "cancelled" && (
                  <p className={s.muted}>本轮已停止，未保存未完成的摘要。</p>
                )}
              </div>
            </div>
            <div
              className={`${s.composerArea} ${draggingFiles ? s.composerDragOver : ""}`}
              onDragEnter={(event) => {
                if (!event.dataTransfer.types.includes("Files")) return;
                event.preventDefault();
                dragDepth.current++;
                setDraggingFiles(true);
              }}
              onDragOver={(event) => {
                if (!event.dataTransfer.types.includes("Files")) return;
                event.preventDefault();
                event.dataTransfer.dropEffect =
                  running || attachmentsPending || speechBusy ? "none" : "copy";
              }}
              onDragLeave={(event) => {
                event.preventDefault();
                dragDepth.current = Math.max(0, dragDepth.current - 1);
                if (!dragDepth.current) setDraggingFiles(false);
              }}
              onDrop={(event) => {
                if (!event.dataTransfer.types.includes("Files")) return;
                event.preventDefault();
                dragDepth.current = 0;
                setDraggingFiles(false);
                if (running || attachmentsPending || speechBusy) {
                  fail(
                    new Error("请先完成当前消息、录音或附件处理，再拖入文件。"),
                  );
                  return;
                }
                void attachmentInput.current?.add(
                  Array.from(event.dataTransfer.files),
                );
              }}
            >
              {draggingFiles && (
                <div className={s.dropHint} role="status">
                  {running || attachmentsPending || speechBusy
                    ? "请先完成当前操作"
                    : "松开即可添加图片或文件"}
                </div>
              )}
              <form className={s.composer} onSubmit={send}>
                {!!selected.length && (
                  <div aria-label="本轮附件">
                    <MessageAttachments files={selected.map(id => {
                      const doc = documents.find(d => d.id === id);
                      return { id, title: doc?.title || "报告附件", filename: doc?.filename || doc?.title || "报告附件",
                        temporary: doc?.kind === "chat_attachment", archived: archivedFiles.includes(id) };
                    })} onArchive={setArchiveFile} disabled={!!running || attachmentsPending || speechBusy}
                      onRemove={id => {
                        const doc = documents.find(d => d.id === id);
                        if (doc?.kind !== "chat_attachment") { setSelected(old => old.filter(value => value !== id)); return; }
                        setAttachmentsPending(true);
                        void api(`/v2/chat-attachments/${id}`, "DELETE").then(() => {
                          setSelected(old => old.filter(value => value !== id));
                          setDocuments(old => old.filter(d => d.id !== id));
                        }).catch(fail).finally(() => setAttachmentsPending(false));
                      }} />
                  </div>
                )}
                <textarea
                  ref={input}
                  aria-label="输入复诊准备问题"
                  placeholder={
                    subject
                      ? "描述症状、询问健康知识，或添加图片、检查报告…"
                      : "可以直接聊天、询问健康知识，或告诉我你需要什么帮助…"
                  }
                  value={question}
                  maxLength={4000}
                  disabled={!!running}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => {
                    if (
                      e.key === "Enter" &&
                      !e.shiftKey &&
                      !e.nativeEvent.isComposing
                    ) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <footer>
                  <div className={s.composerTools}>
                    <button
                      type="button"
                      className={s.addAttachment}
                      aria-label="添加文件"
                      disabled={!!running || speechBusy}
                      title="添加图片或文件，也可拖入输入框"
                      onClick={() => attachmentInput.current?.openPicker()}
                    >
                      <svg
                        width="22"
                        height="22"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        aria-hidden="true"
                      >
                        <path d="M12 5v14M5 12h14" />
                      </svg>
                    </button>
                    <SpeechInput
                      key={`${subjectId}:${conversation?.id || "new"}`}
                      disabled={!!running}
                      onBusyChange={setSpeechBusy}
                      onText={(text) => {
                        setQuestion(
                          (old) =>
                            `${old}${old && !/\s$/.test(old) ? "\n" : ""}${text}`,
                        );
                        input.current?.focus();
                      }}
                    />
                  </div>
                  <div className={s.row}>
                    {running ? (
                      <button
                        type="button"
                        className={s.primary}
                        onClick={() => void cancel()}
                      >
                        停止
                      </button>
                    ) : (
                      <button
                        className={s.primary}
                        disabled={
                          (!question.trim() && !selected.length) ||
                          attachmentsPending ||
                          speechBusy
                        }
                      >
                        发送
                      </button>
                    )}
                  </div>
                </footer>
                <p className={s.composerHint}>
                  {attachmentsPending
                    ? "正在处理附件…"
                    : "可拖入图片或文件 · Enter 发送，Shift + Enter 换行"}
                </p>
              </form>
              <div id="chat-upload-panel">
                <ChatAttachments
                  ref={attachmentInput}
                  key={subject?.id || "no-subject"}
                  subject={subject}
                  conversationId={conversation?.id || ""}
                  disabled={!!running || speechBusy}
                  onPendingChange={setAttachmentsPending}
                  fail={fail}
                  onAdded={(row, attachedConversation) => {
                    setConversation(attachedConversation);
                    void refreshHistory();
                    setDocuments((old) => [
                      row,
                      ...old.filter((doc) => doc.id !== row.id),
                    ]);
                    setSelected((old) =>
                      old.includes(row.id) ? old : [...old, row.id],
                    );
                  }}
                />
                {archiveFile && <ArchiveAttachmentDialog key={archiveFile} id={archiveFile} subjects={subjects}
                  initialSubject={subjectId} onClose={() => setArchiveFile(null)}
                  onSubjectCreated={person => setSubjects(old => [person, ...old.filter(p => p.id !== person.id)])}
                  onSaved={document => {
                    setArchivedFiles(old => [...old, archiveFile]);
                    setDocuments(old => [document, ...old.filter(d => d.id !== document.id)]);
                  }} />}
              </div>
            </div>
          </section>
        ) : (
          <section className={s.content}>
            {tab === "profile" && (
              <HealthPanel
                subjects={subjects}
                subject={subject}
                onChanged={refreshSubjects}
                onSelect={chooseSubject}
                fail={fail}
              />
            )}{" "}
            {tab === "runs" && user.role === "admin" && (
              <AdminPanel fail={fail} onReturnChat={() => navigate("chat")} />
            )}
          </section>
        )}
      </main>
    </div>
  );
}
