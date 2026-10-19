"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import styles from "./assistant.module.css";

export function useConsultationLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  useEffect(() => {
    try { setCollapsed(localStorage.getItem("carebrief-sidebar-collapsed") === "true"); } catch { /* Storage is optional. */ }
  }, []);
  function toggleSidebar() {
    setCollapsed(value => {
      try { localStorage.setItem("carebrief-sidebar-collapsed", String(!value)); } catch { /* Storage is optional. */ }
      return !value;
    });
  }
  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "s") {
        event.preventDefault(); toggleSidebar();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, []);
  return { collapsed, toggleSidebar, sourcesOpen, toggleSources: () => setSourcesOpen(value => !value) };
}

export function SidebarToggle({ collapsed, onClick, controls }: { collapsed: boolean; onClick: () => void; controls: string }) {
  return <button type="button" className={styles.toggle} onClick={onClick} aria-controls={controls} aria-expanded={!collapsed} aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"} title={`${collapsed ? "展开" : "收起"}侧边栏 · Ctrl+Shift+S`}><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/><path d={collapsed ? "m13 9 3 3-3 3" : "m16 9-3 3 3 3"}/></svg></button>;
}

export default function AssistantWelcome({ onChoose }: { onChoose: (text: string) => void }) {
  return <section className={styles.welcome} aria-label="CareBrief 智能健康助手">
    <div className={styles.scene}>
      <div className={styles.halo}/><div className={styles.orbit}/>
      <Image
        className={styles.mascot}
        src="/images/bear-assistant.png"
        alt={"\u6234\u8033\u9ea6\u3001\u7a7f\u767d\u5927\u8902\uff0c\u62ff\u7740\u5065\u5eb7\u5e73\u677f\u6325\u624b\u7684 AIQ \u5c0f\u718a\u5065\u5eb7\u52a9\u624b"}
        width={360}
        height={360}
        sizes="(max-width: 600px) 260px, 320px"
        priority
      />
      <button type="button" className={`${styles.skill} ${styles.left}`} onClick={() => onChoose("我想描述一下最近的症状，请帮我整理就诊前需要补充的信息。 ")}><span aria-hidden="true">＋</span><div>聊聊症状<small>把不舒服说清楚</small></div></button>
      <button type="button" className={`${styles.skill} ${styles.right}`} onClick={() => onChoose("我想了解如何阅读检查报告，需要提供哪些信息？")}><span aria-hidden="true">▤</span><div>看懂报告<small>一起梳理检查信息</small></div></button>
    </div>
    <div className={styles.kicker}><span/> YOUR HEALTH COMPANION</div>
    <h2>你好，一起准备好这次复诊。</h2>
    <p>我是 CareBrief，你的 AI 健康助手。<br className={styles.mobileBreak}/>整理近期记录、对比检查，把想问医生的事准备好。</p>
  </section>;
}
