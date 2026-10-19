import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const base = "http://127.0.0.1:8002";
const accounts = JSON.parse(
  fs.readFileSync(path.resolve("../api/.cache/initial-accounts.json"), "utf8"),
);

test("复诊工作台：资料隔离、摘要、导航、大屏与缩放布局", async ({
  page,
  context,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/workspace");
  await expect(
    page.getByRole("heading", { name: "登录复诊工作台" }),
  ).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("01-login.png") });
  const admin = accounts.find((x: { role: string }) => x.role === "admin");
  const response = await context.request.post(base + "/auth/login", {
    data: { username: admin.username, password: admin.password },
  });
  expect(response.status()).toBe(200);
  let subject = "";
  try {
    const create = await context.request.post(base + "/v2/subjects", {
      data: {
        name: "合成测试对象",
        relation: "本人",
        diseases: ["diabetes", "hypertension"],
        constraints: "仅整理已确认记录，不调整药物。",
      },
    });
    subject = (await create.json()).id;
    for (const [date, value] of [
      ["2026-09-01", "6.0"],
      ["2026-10-01", "6.2"],
    ]) {
      expect(
        (
          await context.request.post(base + "/v2/records", {
            data: {
              subject,
              date,
              kind: "observation",
              label: "空腹血糖",
              value,
              unit: "mmol/L",
            },
          })
        ).status(),
      ).toBe(200);
    }
    await page.reload();
    await page
      .getByRole("combobox", { name: "选择复诊对象" })
      .selectOption(subject);
    await expect(
      page.getByRole("heading", { name: "你好，一起准备好这次复诊。" }),
    ).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("02-workspace.png") });
    await page
      .getByRole("textbox", { name: "输入复诊准备问题" })
      .fill("请整理已确认的两次检查记录，准备复诊资料。");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "复诊准备摘要", exact: true }),
    ).toBeVisible({ timeout: 45000 });
    await expect(
      page.getByRole("cell", { name: "6.2", exact: true }),
    ).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("03-result.png") });
    const composer = page.locator("form").last();
    const result = page
      .locator("section")
      .filter({
        has: page.getByRole("heading", { name: "复诊准备摘要", exact: true }),
      })
      .last();
    const a = await composer.boundingBox(),
      b = await result.boundingBox();
    expect(Math.abs(a!.x - b!.x)).toBeLessThan(2);
    expect(Math.abs(a!.x + a!.width - (b!.x + b!.width))).toBeLessThan(2);
    await page.getByRole("button", { name: "收起侧边栏" }).click();
    await expect(
      page.getByRole("button", { name: "展开侧边栏" }),
    ).toBeVisible();
    await page.setViewportSize({ width: 1100, height: 800 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBeTruthy();
    await page.screenshot({
      path: testInfo.outputPath("04-collapsed-scaled.png"),
    });
    await page.getByRole("button", { name: "后台管理", exact: true }).click();
    await expect(
      page.getByRole("heading", {
        name: "最近复诊任务 · 含无模型调用的演示任务",
      }),
    ).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("05-dashboard.png") });
    await page
      .getByRole("button", { name: "查看", exact: true })
      .first()
      .click();
    await expect(
      page.getByRole("heading", { name: "Agent 执行链路" }),
    ).toBeVisible();
    await expect(page.getByText("逐项回读证据", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "公共知识库", exact: true }).click();
    await expect(page.getByRole("button", { name: "录入正文" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("06-knowledge.png") });
    expect(errors).toEqual([]);
  } finally {
    if (subject) await context.request.delete(base + "/v2/subjects/" + subject);
  }
});

test("普通用户看不到后台入口，直接调用后台接口被拒绝", async ({
  page,
  context,
}) => {
  const user = accounts.find((x: { role: string }) => x.role === "user");
  await context.request.post(base + "/auth/login", {
    data: { username: user.username, password: user.password },
  });
  await page.goto("/workspace?tab=runs");
  await expect(page.getByRole("textbox", { name: "输入复诊准备问题" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "后台管理", exact: true }),
  ).toHaveCount(0);
  expect(
    (await context.request.get(base + "/v2/admin/dashboard")).status(),
  ).toBe(403);
});

test("未选择复诊对象也能日常聊天，并保留多轮上下文", async ({
  page,
  context,
}, testInfo) => {
  const user = accounts.find((x: { role: string }) => x.role === "user");
  await context.request.post(base + "/auth/login", {
    data: { username: user.username, password: user.password },
  });
  const before = (
    await (await context.request.get(base + "/v2/conversations")).json()
  ).items.map((x: { id: string }) => x.id);
  try {
    await page.goto("/workspace");
    const subject = page.getByRole("combobox", { name: "选择复诊对象" });
    await expect(page.getByRole("textbox", { name: "输入复诊准备问题" })).toBeVisible();
    if (await subject.count()) await subject.selectOption("");
    const input = page.getByRole("textbox", { name: "输入复诊准备问题" });
    await input.fill("你好你能做什么？顺便记住我喜欢蓝色。");
    await expect(
      page.getByRole("button", { name: "发送", exact: true }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.locator('[class*="chatReply"]')).toHaveCount(1, {
      timeout: 45000,
    });
    await expect(
      page.getByRole("heading", { name: "复诊准备摘要", exact: true }),
    ).toHaveCount(0);
    await input.fill("我刚才说喜欢什么颜色？只回答颜色。");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.locator('[class*="chatReply"]')).toHaveCount(2, {
      timeout: 45000,
    });
    await expect(page.locator('[class*="chatReply"]').last()).toContainText(
      "蓝色",
    );
    await page.screenshot({ path: testInfo.outputPath("07-daily-chat.png") });
    await input.fill("抗生素是什么？只做一般知识科普，不分析个人报告。");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.locator('[class*="chatReply"]')).toHaveCount(3, {
      timeout: 45000,
    });
    const chats = (
      await (await context.request.get(base + "/v2/conversations")).json()
    ).items;
    const current = chats.find((x: { id: string }) => !before.includes(x.id));
    const saved = await (
      await context.request.get(base + "/v2/conversations/" + current.id)
    ).json();
    expect(saved.messages.at(-1).result.intent).toBe("daily");
    expect(saved.messages.at(-1).result.consultation_type).toBe("medical_education");
    expect(saved.messages.at(-1).result.records).toEqual([]);
    await page.screenshot({
      path: testInfo.outputPath("08-medical-education.png"),
    });
  } finally {
    const after = (
      await (await context.request.get(base + "/v2/conversations")).json()
    ).items;
    for (const row of after)
      if (!before.includes(row.id))
        await context.request.delete(base + "/v2/conversations/" + row.id);
  }
});
