import type { Metadata } from "next";
import WorkspaceClient from "./WorkspaceV2Client";
import ProportionalWorkspace from "./ProportionalWorkspace";

export const metadata: Metadata = {
  title: "CareBrief · 健康咨询工作台",
  description: "个人资料、可追溯咨询与医院知识查询",
};

export default function WorkspacePage() {
  return <ProportionalWorkspace><WorkspaceClient /></ProportionalWorkspace>;
}
