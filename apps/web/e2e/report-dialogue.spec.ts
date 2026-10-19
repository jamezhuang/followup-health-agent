import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";
import { archiveAttachment } from "./archive-attachment";

for (const role of ["user", "admin"]) {
  test(`${role}：报告追问直接回答，附件随对话保留`, async ({ page, context }, testInfo) => {
    await fixtureAccount(context);
    if (role === "admin") await context.request.post(fixtureBase + "/auth/login", {
      data: { username: "fixtureadmin", password: "test123456" },
    });
    const person = await (await context.request.post(fixtureBase + "/v2/subjects", {
      data: { name: "报告对话验收", patient_name: "合成患者", diseases: [] },
    })).json();
    await page.goto("/workspace");
    await page.getByRole("combobox", { name: "选择复诊对象" }).selectOption(person.id);
    await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
      name: "synthetic-dialogue.txt", mimeType: "text/plain",
      buffer: Buffer.from("姓名：合成患者\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强\n影像学表现：合成描述，仅用于测试。"),
    });
    const dialog = page.getByRole("dialog", { name: "核对报告并保存" });
    await expect(page.getByLabel("本轮附件", { exact: true })).toContainText("synthetic-dialogue.txt");
    await expect(dialog).toHaveCount(0);
    expect((await (await context.request.get(fixtureBase+"/v2/documents?subject="+person.id)).json()).items).toHaveLength(0);
    const input = page.getByRole("textbox", { name: "输入复诊准备问题" });
    await input.fill("我的检测报告检测项目是什么");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    const answer = page.getByText(/这份报告的检查项目是/);
    await expect(answer).toContainText("胸部 CT 平扫＋增强");
    await expect(page.getByRole("heading", { name: "复诊准备摘要" })).toHaveCount(0);
    const file = page.getByRole("link", { name: "查看附件 synthetic-dialogue.txt" });
    await expect(file).toBeVisible();
    const original = await context.request.get((await file.getAttribute("href"))!.replace(":8002", ":8003"));
    expect(original.ok()).toBeTruthy();
    expect(await original.text()).toContain("胸部CT平扫+增强");
    await input.fill("这个检查项目是什么");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByText(/这份报告的检查项目是/)).toHaveCount(2);
    await archiveAttachment(page, "synthetic-dialogue.txt");
    await dialog.getByRole("checkbox", { name: /我已核对患者/ }).check();
    await dialog.getByRole("button", { name: "确认并保存报告", exact: true }).click();
    await expect(dialog).not.toBeVisible();
    expect((await (await context.request.get(fixtureBase+"/v2/documents?subject="+person.id)).json()).items).toHaveLength(1);
    await expect(file).toHaveCount(1);
    await page.reload();
    await expect(page.getByTitle("我的检测报告检测项目是什么", { exact: true })).toBeVisible();
    await page.getByTitle("我的检测报告检测项目是什么", { exact: true }).click();
    await expect(file).toBeVisible();
    await expect(page.getByText(/这份报告的检查项目是/)).toHaveCount(2);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(file).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath("report-dialogue-mobile.png"), fullPage: true });
  });
}
