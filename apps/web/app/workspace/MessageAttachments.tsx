"use client";
import { useState } from "react";
import { API } from "./v2-api";
import type { Message } from "./v2-api";
import s from "./v2.module.css";

function Attachment({ file, onArchive, onRemove, disabled }: {
  file: NonNullable<Message["attachments"]>[number]; onArchive?: (id: string) => void;
  onRemove?: (id: string) => void; disabled?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const image = /\.(jpe?g|png|webp)$/i.test(file.filename);
  const url = `${API}/v2/documents/${encodeURIComponent(file.id)}/file`;
  return (
    <div className={`${s.messageAttachmentItem} ${onRemove ? s.attachmentCompact : ""}`}><a className={s.messageAttachment} href={url} target="_blank" rel="noreferrer"
      aria-label={`查看附件 ${file.title}`} title={file.title}>
      {image && !failed ? (
        // User uploads are served through an authenticated endpoint, without optimization.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt={file.title} loading="lazy" onError={() => setFailed(true)} />
      ) : <span className={s.attachmentFileType}>{file.filename.split(".").pop()?.toUpperCase() || "文件"}</span>}
      <span className={s.attachmentName}>{file.title}</span>
    </a>
    {file.temporary && (file.archived ? <span className={s.attachmentSaved} aria-label="已存入复诊档案" title="已存入复诊档案">✓</span> : onArchive &&
      <button type="button" className={s.attachmentArchive} disabled={disabled} onClick={() => onArchive(file.id)}
        title="保存到复诊档案" aria-label={`保存到复诊档案 ${file.title}`}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M12 3v11m-4-4 4 4 4-4M4 15v5h16v-5" />
        </svg>
      </button>)}
    {onRemove && <button type="button" className={s.attachmentRemove} disabled={disabled}
      title="移除附件" aria-label={`取消选用 ${file.title}`} onClick={() => onRemove(file.id)}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
    </button>}
    </div>
  );
}

export default function MessageAttachments({ files, onArchive, onRemove, disabled }: {
  files: Message["attachments"]; onArchive?: (id: string) => void; onRemove?: (id: string) => void; disabled?: boolean;
}) {
  if (!files?.length) return null;
  return <div className={s.messageAttachments} aria-label="本轮上传的附件">
    {files.map(file => <Attachment key={file.id} file={file} onArchive={onArchive} onRemove={onRemove} disabled={disabled} />)}
  </div>;
}
