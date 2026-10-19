"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "./v2-api";
import s from "./v2.module.css";

// Tencent SentenceRecognition accepts WAV, but browsers commonly record WebM.
// Decode and resample locally to mono 16 kHz PCM; no audio leaves the browser
// until the user finishes recording.
async function speechWav(recording: Blob): Promise<Blob> {
  const context = new OfflineAudioContext(1, 1, 16000);
  const audio = await context.decodeAudioData(await recording.arrayBuffer());
  if (!audio.length || audio.duration > 60)
    throw new Error("录音须在60秒以内，请重新录制。");
  const buffer = new ArrayBuffer(44 + audio.length * 2);
  const view = new DataView(buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++)
      view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, buffer.byteLength - 8, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, audio.length * 2, true);
  const channels = Array.from({ length: audio.numberOfChannels }, (_, i) =>
    audio.getChannelData(i),
  );
  for (let i = 0; i < audio.length; i++) {
    const sample = Math.max(
      -1,
      Math.min(
        1,
        channels.reduce((sum, channel) => sum + channel[i], 0) /
          channels.length,
      ),
    );
    view.setInt16(44 + i * 2, sample * (sample < 0 ? 32768 : 32767), true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

type Phase = "idle" | "preparing" | "recording" | "recognizing";

export default function SpeechInput({
  disabled,
  onText,
  onBusyChange,
}: {
  disabled: boolean;
  onText: (text: string) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const token = useRef(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const request = useRef<AbortController | null>(null);
  const spaceHeld = useRef(false);
  const microphoneButton = useRef<HTMLButtonElement>(null);
  const interval = useRef<ReturnType<typeof setInterval> | undefined>(
    undefined,
  );
  const limit = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const timeout = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  function release() {
    clearInterval(interval.current);
    clearTimeout(limit.current);
    clearTimeout(timeout.current);
    if (recorder.current?.state === "recording") recorder.current.stop();
    stream.current?.getTracks().forEach((track) => track.stop());
    recorder.current = null;
    stream.current = null;
  }

  useEffect(
    () => () => {
      token.current++;
      request.current?.abort();
      release();
      onBusyChange(false);
    },
    [onBusyChange],
  );

  function cancel() {
    spaceHeld.current = false;
    token.current++;
    request.current?.abort();
    release();
    setPhase("idle");
    onBusyChange(false);
    setNotice("已取消，录音不会填入消息。");
  }

  async function start() {
    const current = ++token.current;
    setNotice("");
    setError("");
    setSeconds(0);
    setPhase("preparing");
    onBusyChange(true);
    const controller = new AbortController();
    request.current = controller;
    try {
      if (
        !navigator.mediaDevices?.getUserMedia ||
        typeof MediaRecorder === "undefined" ||
        typeof OfflineAudioContext === "undefined"
      )
        throw new Error(
          "当前浏览器无法录音，请用新版 Chrome 或 Edge，并通过本机地址或 HTTPS 访问。",
        );
      timeout.current = setTimeout(() => controller.abort(), 15000);
      const capabilities = await api<{ asr_ready: boolean }>(
        "/v2/recognition/capabilities",
        "GET",
        undefined,
        controller.signal,
      );
      clearTimeout(timeout.current);
      if (current !== token.current) return;
      if (!capabilities.asr_ready)
        throw new Error(
          "语音识别尚未就绪，请检查后端语音开关、腾讯云密钥和识别依赖，并重启后端。",
        );
      const microphone = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      if (current !== token.current) {
        microphone.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = microphone;
      const mimeType = [
        "audio/webm;codecs=opus",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ].find((type) => MediaRecorder.isTypeSupported(type));
      const capture = new MediaRecorder(
        microphone,
        mimeType ? { mimeType } : undefined,
      );
      recorder.current = capture;
      const chunks: Blob[] = [];
      capture.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };
      capture.onerror = () => {
        if (current !== token.current) return;
        token.current++;
        release();
        setPhase("idle");
        onBusyChange(false);
        setError("录音中断，请检查麦克风后重试。");
      };
      capture.onstop = async () => {
        if (current !== token.current) return;
        release();
        setPhase("recognizing");
        timeout.current = setTimeout(() => controller.abort(), 90000);
        try {
          const wav = await speechWav(
            new Blob(chunks, { type: capture.mimeType }),
          );
          if (current !== token.current) return;
          const form = new FormData();
          form.append("file", wav, "speech.wav");
          const result = await api<{ text: string }>(
            "/v2/speech/recognize",
            "POST",
            form,
            controller.signal,
          );
          if (current !== token.current) return;
          if (!result.text?.trim())
            throw new Error("没有识别到语音，请重新录制。");
          onText(result.text.trim());
          setNotice("已填入输入框，请核对后发送。");
        } catch (cause) {
          if (current === token.current)
            setError(
              controller.signal.aborted
                ? "语音识别超时，请重试。"
                : cause instanceof Error
                  ? cause.message
                  : "语音识别失败，请重试。",
            );
        } finally {
          if (current === token.current) {
            release();
            setPhase("idle");
            onBusyChange(false);
          }
        }
      };
      capture.start();
      setPhase("recording");
      const started = Date.now();
      interval.current = setInterval(
        () => setSeconds(Math.floor((Date.now() - started) / 1000)),
        250,
      );
      // Leave a margin below the provider's 60-second maximum.
      limit.current = setTimeout(() => {
        if (capture.state === "recording") capture.stop();
      }, 55000);
    } catch (cause) {
      if (current !== token.current) return;
      release();
      setPhase("idle");
      onBusyChange(false);
      const name = cause instanceof Error ? cause.name : "";
      setError(
        name === "NotAllowedError"
          ? "麦克风权限未开启，请在浏览器中允许麦克风访问后重试。"
          : name === "NotFoundError"
            ? "未找到麦克风，请连接设备后重试。"
            : name === "NotReadableError"
              ? "麦克风不可用或正被其他程序占用，请检查后重试。"
              : controller.signal.aborted
                ? "连接语音服务超时，请重试。"
                : cause instanceof Error
                  ? cause.message
                  : "无法开始录音，请重试。",
      );
    }
  }

  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      if (
        event.code !== "Space" ||
        event.isComposing ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey
      )
        return;
      if (spaceHeld.current) {
        event.preventDefault();
        return;
      }
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (
        disabled ||
        phase !== "idle" ||
        event.repeat ||
        document.querySelector('dialog[open], [aria-modal="true"]')
      )
        return;
      // Preserve typing, IME candidate selection and native control shortcuts.
      if (
        target !== microphoneButton.current &&
        target?.closest(
          'input, textarea, select, button, a, [contenteditable], [role="textbox"]',
        )
      )
        return;
      event.preventDefault();
      spaceHeld.current = true;
      void start();
    }
    function keyup(event: KeyboardEvent) {
      if (event.code !== "Space" || !spaceHeld.current) return;
      event.preventDefault();
      spaceHeld.current = false;
      if (recorder.current?.state === "recording") recorder.current.stop();
      else if (phase === "preparing") cancel();
    }
    function blur() {
      if (spaceHeld.current) cancel();
    }
    function visibility() {
      if (document.hidden) blur();
    }
    window.addEventListener("keydown", keydown);
    window.addEventListener("keyup", keyup);
    window.addEventListener("blur", blur);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("keydown", keydown);
      window.removeEventListener("keyup", keyup);
      window.removeEventListener("blur", blur);
      document.removeEventListener("visibilitychange", visibility);
    };
  });

  const active = phase !== "idle";
  return (
    <div className={s.speechInput}>
      <button
        ref={microphoneButton}
        type="button"
        className={`${s.speechButton} ${phase === "recording" ? s.speechRecording : ""}`}
        disabled={disabled || phase === "preparing" || phase === "recognizing"}
        aria-label={phase === "recording" ? "结束录音并识别" : "语音输入"}
        aria-pressed={phase === "recording"}
        title="点击录音，或在输入框外按住空格说话、松开识别"
        onClick={() =>
          phase === "recording" ? recorder.current?.stop() : void start()
        }
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          aria-hidden="true"
        >
          {phase === "recording" ? (
            <rect
              x="6"
              y="6"
              width="12"
              height="12"
              rx="2"
              fill="currentColor"
            />
          ) : (
            <>
              <rect x="9" y="2" width="6" height="12" rx="3" />
              <path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8" />
            </>
          )}
        </svg>
        {phase === "recording"
          ? `结束录音 ${seconds}s`
          : phase === "preparing"
            ? "准备录音…"
            : phase === "recognizing"
              ? "正在识别…"
              : "语音输入"}
      </button>
      {active && (
        <button type="button" className={s.speechButton} onClick={cancel}>
          取消录音
        </button>
      )}
      <span
        className={error ? s.error : s.speechNotice}
        role={error ? "alert" : "status"}
      >
        {error ||
          (phase === "recording"
            ? "55秒自动结束，完成后转为文字"
            : phase === "preparing"
              ? "请允许麦克风访问"
              : phase === "recognizing"
                ? "正在转文字，请稍候"
                : notice || "输入框外按住空格说话，松开识别")}
      </span>
    </div>
  );
}
