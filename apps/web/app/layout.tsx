import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CareBrief V2 · 慢病复诊工作台",
  description: "症状整理、资料检索与可追溯来源的 Agent 演示",
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
