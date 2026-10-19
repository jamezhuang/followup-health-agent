import { test, expect } from "@playwright/test";
import fs from "node:fs";
import { fixtureAccount, fixtureBase } from "./fixture-api";

for (const role of ["user", "admin"]) {
  test(`${role}：复诊清单优先展示，复制导出及重开保留行动提醒`, async ({
    page,
    context,
  }, testInfo) => {
    await fixtureAccount(context);
    if (role === "admin")
      await context.request.post(fixtureBase + "/auth/login", {
        data: { username: "fixtureadmin", password: "test123456" },
      });
    const subject = (
      await (
        await context.request.post(fixtureBase + "/v2/subjects", {
          data: {
            name: "准备清单验收",
            patient_name: "合成患者",
            diseases: [],
          },
        })
      ).json()
    ).id;
    await page.addInitScript(() =>
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (text: string) =>
            sessionStorage.setItem("preparation-copy", text),
        },
      }),
    );
    try {
      await page.goto("/workspace");
      await page
        .getByRole("combobox", { name: "选择复诊对象" })
        .selectOption(subject);
      await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
        name: "synthetic-ct.txt",
        mimeType: "text/plain",
        buffer: Buffer.from(
          "姓名：合成患者\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强\n影像学表现：合成术后描述，仅用于验收。\n影像学意见：合成结论。",
        ),
      });
      await expect(page.getByLabel("本轮附件", { exact: true })).toContainText("synthetic-ct.txt");
      await expect(page.getByRole("dialog", { name: "核对报告并保存" })).toHaveCount(0);
      await page
        .getByRole("textbox", { name: "输入复诊准备问题" })
        .fill("请按这份报告帮我准备复诊，提醒需要携带的资料和遗漏事项");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      const checklist = page.getByRole("heading", { name: "需要准备的资料清单", exact: true });
      await expect(checklist).toBeVisible();
      const answer = page.locator("section").filter({ has: checklist }).last();
      await expect(answer).toContainText("对应影像与既往对比资料");
      await expect(answer).toContainText("相关手术或住院资料");
      await expect(answer).toContainText("2026-07-06 synthetic-ct.txt");
      await expect(answer).not.toContainText("本轮已提供");
      await expect(answer).not.toContainText("有来源的整理结论");
      await expect(answer.locator("ul > li").first()).toBeVisible();
      await expect(page.getByRole("heading", { name: "复诊时重点问医生", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "复制回答", exact: true }).click();
      const copied = await page.evaluate(() => sessionStorage.getItem("preparation-copy"));
      expect(copied).toContain("## 需要准备的资料清单");
      expect(copied).not.toContain("有来源的整理结论");
      const download = page.waitForEvent("download");
      await page.getByRole("button", { name: "导出摘要", exact: true }).click();
      const path = testInfo.outputPath("preparation.md");
      await (await download).saveAs(path);
      expect(fs.readFileSync(path, "utf8")).toBe(copied);
      await checklist.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath("preparation-checklist.png"),
      });
      const conversations = (
        await (
          await context.request.get(fixtureBase + "/v2/conversations")
        ).json()
      ).items;
      const conversation = conversations.find(
        (row: { subject: string }) => row.subject === subject,
      );
      await page.reload();
      await page.locator(`button[title="${conversation.title}"]`).click();
      await expect(checklist).toBeVisible();
      await expect(
        page.getByText(/当前实际用药清单：/),
      ).toBeVisible();
    } finally {
      await context.request.delete(fixtureBase + "/v2/subjects/" + subject);
    }
  });
}
