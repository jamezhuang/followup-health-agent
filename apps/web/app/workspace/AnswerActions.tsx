"use client";
import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./v2-api";
import type { Message } from "./v2-api";
import { answerText, spokenText } from "./answer-text";
import s from "./v2.module.css";

let playback: { id: string; stop: () => void } | null = null;
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      width="19"
      height="19"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}
export default function AnswerActions({
  message,
  disabled,
  onRegenerate,
}: {
  message: Message;
  disabled: boolean;
  onRegenerate: () => void;
}) {
  const id = useId();
  const [vote, setVote] = useState(message.feedback || null);
  const [voting, setVoting] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [copied, setCopied] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    setNotice("");
    setCopied(false);
  }, [message.run_id]);
  useEffect(() => {
    setVote(message.feedback || null);
  }, [message.feedback, message.run_id]);
  useEffect(
    () => () => {
      if (playback?.id === id) playback.stop();
    },
    [id, message.run_id],
  );
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);
  if (!message.result) return null;
  const text = answerText(message.result);
  const plain = spokenText(text);
  const count = Array.from(plain.replace(/\s/g, "")).length;
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error();
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setNotice("已复制回答");
    } catch {
      setNotice("复制失败，请允许浏览器访问剪贴板，或手动选择文字复制。");
    }
  }
  function read() {
    setNotice("");
    if (speaking) {
      playback?.stop();
      return;
    }
    if (
      !("speechSynthesis" in window) ||
      typeof SpeechSynthesisUtterance === "undefined"
    ) {
      setNotice("当前浏览器暂不支持朗读。");
      return;
    }
    playback?.stop();
    window.speechSynthesis.cancel();
    let stopped = false,
      position = 0;
    const chunks = plain.match(/[^。！？\n]{1,200}[。！？\n]?/g) || [plain];
    function stop() {
      stopped = true;
      window.speechSynthesis.cancel();
      setSpeaking(false);
      if (playback?.id === id) playback = null;
    }
    function next() {
      if (stopped || position >= chunks.length) {
        if (!stopped) stop();
        return;
      }
      const utterance = new SpeechSynthesisUtterance(chunks[position++]);
      utterance.lang = "zh-CN";
      utterance.rate = 1;
      const voices = window.speechSynthesis
        .getVoices()
        .filter((voice) => voice.lang.startsWith("zh"));
      const voice = voices.find((voice) => voice.localService) || voices[0];
      if (voice) utterance.voice = voice;
      utterance.onend = next;
      utterance.onerror = () => {
        if (!stopped) {
          stop();
          setNotice("朗读未完成，请检查浏览器语音支持后重试。");
        }
      };
      window.speechSynthesis.speak(utterance);
    }
    playback = { id, stop };
    setSpeaking(true);
    next();
  }
  async function feedback(next: "like" | "dislike") {
    if (!message.run_id || voting) return;
    setVoting(true);
    try {
      const result = await api<{ vote: "like" | "dislike" | null }>(
        `/v2/runs/${message.run_id}/feedback`,
        "PUT",
        { vote: vote === next ? null : next },
      );
      setVote(result.vote);
      setNotice(result.vote ? "反馈已保存" : "已取消反馈");
    } catch {
      setNotice("反馈保存失败，请重试。");
    } finally {
      setVoting(false);
    }
  }
  return (
    <div className={s.answerFooter}>
      <div className={s.answerActions} aria-label="回答操作">
        <button
          type="button"
          title={copied ? "已复制" : "复制回答"}
          aria-label="复制回答"
          onClick={() => void copy()}
        >
          <Icon>
            {copied ? (
              <path d="m5 12 4 4L19 6" />
            ) : (
              <>
                <rect x="8" y="8" width="12" height="13" rx="2" />
                <path d="M16 4V3H4v13h1" />
              </>
            )}
          </Icon>
        </button>
        <button
          type="button"
          title={speaking ? "停止朗读" : "朗读回答"}
          aria-label={speaking ? "停止朗读" : "朗读回答"}
          aria-pressed={speaking}
          onClick={read}
        >
          <Icon>
            {speaking ? (
              <rect x="6" y="6" width="12" height="12" rx="1" />
            ) : (
              <>
                <path d="m11 4-6 5H2v6h3l6 5V4Z" />
                <path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14" />
              </>
            )}
          </Icon>
        </button>
        <button
          type="button"
          title="有帮助"
          aria-label="有帮助"
          aria-pressed={vote === "like"}
          disabled={voting || !message.run_id}
          onClick={() => void feedback("like")}
        >
          <Icon>
            <path d="M7 10 11 3c2 0 3 1 2 4l-1 3h7a2 2 0 0 1 2 2l-2 8H7V10Z" />
            <path d="M3 10h4v10H3z" />
          </Icon>
        </button>
        <button
          type="button"
          title="没有帮助"
          aria-label="没有帮助"
          aria-pressed={vote === "dislike"}
          disabled={voting || !message.run_id}
          onClick={() => void feedback("dislike")}
        >
          <Icon>
            <path d="m7 14 4 7c2 0 3-1 2-4l-1-3h7a2 2 0 0 0 2-2l-2-8H7v10Z" />
            <path d="M3 4h4v10H3z" />
          </Icon>
        </button>
        <button
          type="button"
          title="重新生成（保留原回答）"
          aria-label="重新生成"
          disabled={disabled || !message.run_id}
          onClick={onRegenerate}
        >
          <Icon>
            <path d="M20 7a9 9 0 1 0 1 8" />
            <path d="M20 2v6h-6" />
          </Icon>
        </button>
        <span
          className={s.answerMetrics}
          title="字数按非空白字符统计；耗时为本轮实际处理总时间，含检索与核验。"
        >
          {count} 字 ·{" "}
          {typeof message.duration_ms === "number"
            ? `${(message.duration_ms / 1000).toFixed(1)} 秒${message.result.mode === "local" ? "回复" : "生成"}`
            : "耗时未知"}
        </span>
      </div>
      <span className={s.answerActionNotice} role="status" aria-live="polite">
        {notice}
      </span>
    </div>
  );
}
