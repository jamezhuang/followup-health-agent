import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

test("管理员可定位失败阶段并从持久化检查点恢复，成功阶段不重复执行", async ({
  page,
  context,
  playwright,
}, testInfo) => {
  await fixtureAccount(context);
  const owner = await playwright.request.newContext({
    baseURL: fixtureBase,
    storageState: await context.storageState(),
  });
  try {
    const goal = "合成恢复验收" + Date.now();
    const created = await owner.post("/v2/runs", { data: { goal } });
    expect(created.ok()).toBeTruthy();
    const run = await created.json();
    await expect
      .poll(
        async () =>
          (await (await owner.get("/v2/runs/" + run.id)).json()).status,
      )
      .toBe("failed");
    expect(
      (await owner.post("/v2/admin/runs/" + run.id + "/resume")).status(),
    ).toBe(403);
    await context.request.post(fixtureBase + "/auth/login", {
      data: { username: "fixtureadmin", password: "test123456" },
    });
    await page.goto("/workspace");
    await page.getByRole("button", { name: "后台管理", exact: true }).click();
    const task = page.getByRole("row").filter({ hasText: run.id.slice(0, 12) });
    await expect(task).toContainText("执行故障");
    await task.getByRole("button", { name: "查看", exact: true }).click();
    const diagnostic = page.getByRole("article", { name: "任务故障诊断" });
    await expect(diagnostic).toContainText("答复结果：执行故障");
    await expect(diagnostic).toContainText("定位阶段：回答生成与核对");
    await expect(diagnostic).not.toContainText(goal);
    await expect(diagnostic).not.toContainText("SYNTHETIC-PRIVATE");
    await diagnostic.scrollIntoViewIfNeeded();
    await diagnostic.screenshot({
      path: testInfo.outputPath("failed-stage-checkpoint.png"),
    });
    await diagnostic
      .getByRole("button", { name: "从检查点恢复 / 重试失败步骤", exact: true })
      .click();
    await expect(diagnostic).toContainText("答复结果：已回答", {
      timeout: 15000,
    });
    const checkpoints = diagnostic.getByRole("table", {
      name: "持久化阶段检查点",
    });
    const intent = checkpoints.getByRole("row").filter({ hasText: "意图识别" });
    await expect(intent.locator("td").nth(2)).toHaveText("1");
    const answer = checkpoints
      .getByRole("row")
      .filter({ hasText: "回答生成与核对" });
    await expect(answer.locator("td").nth(2)).toHaveText("2");
    const conversation = await (
      await owner.get("/v2/conversations/" + run.conversation_id)
    ).json();
    expect(conversation.messages).toHaveLength(2);
    expect(conversation.messages[1].run_id).toBe(run.id);
    await diagnostic.screenshot({
      path: testInfo.outputPath("recovered-stage-checkpoint.png"),
    });
    await owner.delete("/v2/conversations/" + run.conversation_id);
  } finally {
    await owner.dispose();
  }
});
