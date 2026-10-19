import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

for (const role of ["user", "admin"]) {
  test(`${role}：日常跟踪入口移除，旧链接回到对话`, async ({
    page,
    context,
  }, testInfo) => {
    await fixtureAccount(context);
    if (role === "admin") {
      const login = await context.request.post(fixtureBase + "/auth/login", {
        data: { username: "fixtureadmin", password: "test123456" },
      });
      expect(login.ok()).toBeTruthy();
    }
    await page.goto("/workspace?tab=tracking");
    await expect(
      page.getByRole("textbox", { name: "输入复诊准备问题" }),
    ).toBeVisible();
    await expect(page).toHaveURL(/tab=chat/);
    await expect(
      page.getByRole("button", { name: "日常药物和症状跟踪", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "日常药物和症状跟踪", exact: true }),
    ).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath("compact-workspace.png"),
    });
    await page.getByRole("button", { name: "复诊档案", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "复诊档案", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "添加记录", exact: true }),
    ).toHaveCount(0);
    if (role === "admin")
      await expect(
        page.getByRole("button", { name: "后台管理", exact: true }),
      ).toBeVisible();
  });
}
