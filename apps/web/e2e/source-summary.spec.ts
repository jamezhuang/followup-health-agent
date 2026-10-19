import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("检索来源按问题显示、资料去重、展开引用及旧记录兼容", async ({
  page,
  context,
}, testInfo) => {
  const account = JSON.parse(
    fs.readFileSync(
      path.resolve("../api/.cache/initial-accounts.json"),
      "utf8",
    ),
  ).find((row: { role: string }) => row.role === "user");
  await context.request.post("http://127.0.0.1:8002/auth/login", {
    data: { username: account.username, password: account.password },
  });
  const source = {
    id: "doc-a:1:0",
    document_id: "doc-a",
    kind: "public",
    title: "模拟资料甲",
    location: "第1页",
    text: "仅用于布局验收的合成原文。",
    url: "https://example.org/guide-a",
  };
  const other = {
    ...source,
    id: "doc-b:1:0",
    document_id: "doc-b",
    title: "模拟资料乙",
    url: "https://example.org/guide-b",
  };
  const result = {
    kind: "followup",
    mode: "demo",
    records: [],
    claims: [
      { text: "布局验收结论。", evidence_id: source.id, quote: source.text },
    ],
    sources: [source],
    search_sources: [
      source,
      { ...source, id: "doc-a:1:800", location: "第2页" },
      other,
    ],
    search_queries: ["模拟关键词甲", "模拟关键词乙", "模拟关键词甲"],
    gaps: [],
    questions: [],
    notice: "合成测试资料",
    rejected_claims: 0,
    iterations: 1,
    stop_reason: "test",
  };
  const conversation = {
    id: "source-layout-test",
    subject: "",
    title: "来源布局验收（模拟）",
    revision: 1,
    updated_at: new Date().toISOString(),
    messages: [
      { role: "user", content: "请整理这份模拟资料。" },
      { role: "assistant", run_id: "synthetic-a", result },
      { role: "user", content: "查看旧记录。" },
      {
        role: "assistant",
        run_id: "synthetic-b",
        result: {
          ...result,
          search_queries: undefined,
          search_sources: undefined,
        },
      },
      { role: "user", content: "你好。" },
      {
        role: "assistant",
        run_id: "synthetic-c",
        result: {
          ...result,
          kind: "chat",
          content: "你好！",
          sources: [],
          claims: [],
          search_queries: [],
          search_sources: [],
        },
      },
    ],
  };
  await page.route("**/v2/conversations", (route) =>
    route.fulfill({ json: { items: [conversation] } }),
  );
  await page.route("**/v2/conversations/source-layout-test", (route) =>
    route.fulfill({ json: conversation }),
  );
  await page.goto("/workspace");
  await page.locator('button[title="来源布局验收（模拟）"]').click();
  const sources = page.getByLabel("本次回答依据", { exact: true });
  await expect(sources).toHaveCount(2);
  await expect(sources.first().locator(":scope > summary")).toHaveText(
    "查看本次回答依据›",
  );
  await expect(sources.nth(1).locator(":scope > summary")).toHaveText(
    "查看本次回答依据›",
  );
  await expect(
    sources.first().getByText("模拟资料甲", { exact: true }),
  ).not.toBeVisible();
  const question = page
    .locator("article")
    .filter({ has: page.getByText("请整理这份模拟资料。", { exact: true }) });
  const a = await sources.first().boundingBox(),
    b = await question.boundingBox();
  const answer = await page
    .locator("section")
    .filter({
      has: page
        .getByRole("heading", { name: "复诊准备摘要", exact: true })
        .first(),
    })
    .last()
    .boundingBox();
  expect(b!.y + b!.height).toBeLessThanOrEqual(a!.y);
  expect(a!.y + a!.height).toBeLessThanOrEqual(answer!.y);
  expect(Math.abs(a!.x - answer!.x)).toBeLessThan(1);
  await sources.first().locator(":scope > summary").click();
  await expect(
    sources.first().getByRole("link", { name: "模拟资料甲" }),
  ).toHaveCount(1);
  await expect(
    sources.first().getByText("模拟关键词乙", { exact: true }),
  ).toHaveCount(0);
  await sources
    .first()
    .locator("details > summary")
    .filter({ hasText: "第1页" })
    .first()
    .click();
  await expect(
    sources.first().getByText("回答引用原文", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("本轮专业资料来源", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.getByText("核对引用原文", { exact: true })).toHaveCount(0);
  await sources.first().scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("sources-above-answer.png"),
    fullPage: true,
  });
});
