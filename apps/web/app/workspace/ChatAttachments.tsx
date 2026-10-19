"use client";
import { useEffect, useImperativeHandle, useRef, useState } from "react";
import type { Ref } from "react";
import { api } from "./v2-api";
import type { Conversation, DocumentRow, Subject } from "./v2-api";
import s from "./v2.module.css";

export type AttachmentHandle = { add: (files: File[]) => Promise<void>; openPicker: () => void };
export default function ChatAttachments({ subject, conversationId, disabled, onAdded, onPendingChange, fail, ref }: {
  subject: Subject | null; conversationId: string; disabled: boolean;
  onAdded: (row: DocumentRow, conversation: Conversation) => void;
  onPendingChange: (pending: boolean) => void; fail: (error: unknown) => void; ref?: Ref<AttachmentHandle>;
}) {
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const uploading = useRef(false);
  useImperativeHandle(ref, () => ({ add, openPicker: () => {
    if (!disabled && !busy && !uploading.current) fileInput.current?.click();
  } }));
  useEffect(() => { onPendingChange(busy); return () => onPendingChange(false); }, [busy, onPendingChange]);
  async function add(files: File[]) {
    if (!files.length) return;
    if (disabled || busy || uploading.current) { fail(new Error("正在处理消息或附件，请完成后再添加文件。")); return; }
    const unsupported = files.find(f => !/\.(jpe?g|png|webp|bmp|pdf|docx|txt|md)$/i.test(f.name));
    if (unsupported) { fail(new Error(`不支持「${unsupported.name}」的格式，请使用图片、PDF、DOCX、TXT 或 MD。`)); return; }
    if (files.length > 9 || files.reduce((sum, f) => sum + f.size, 0) > 45 * 1024 * 1024 || files.some(f => !f.size || f.size > 8 * 1024 * 1024)) {
      fail(new Error("最多9个文件，单个文件须非空且不超过8MB，总大小不超过45MB。")); return;
    }
    uploading.current = true; setBusy(true);
    let currentConversation = conversationId;
    try {
      for (const file of files) {
        try {
          const body = new FormData(); body.append("file", file);
          const query = new URLSearchParams({ subject: subject?.id || "", conversation_id: currentConversation });
          const row = await api<DocumentRow & { conversation: Conversation }>(`/v2/chat-attachments?${query}`, "POST", body);
          currentConversation = row.conversation.id; onAdded(row, row.conversation);
        } catch (error) { fail(new Error(`「${file.name}」上传失败：${error instanceof Error ? error.message : "请重试。"}`)); }
      }
    } finally { uploading.current = false; setBusy(false); }
  }
  return <section aria-label="上传对话附件">
    <input ref={fileInput} type="file" hidden multiple aria-label="添加图片或报告"
      accept=".jpg,.jpeg,.png,.webp,.bmp,.pdf,.docx,.txt,.md" disabled={disabled || busy}
      onChange={event => { void add(Array.from(event.target.files || [])); event.target.value = ""; }} />
    {busy && <p className={s.attachmentProgress} role="status">正在读取附件，完成后可直接提问…</p>}
  </section>;
}
