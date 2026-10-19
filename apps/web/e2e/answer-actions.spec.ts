import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("回答操作栏：复制、朗读、反馈持久化、重生成及统计", async ({
  page,
  context,
}, testInfo) => {
  const base = "http://127.0.0.1:8002";
  const user = JSON.parse(
    fs.readFileSync(
      path.resolve("../api/.cache/initial-accounts.json"),
      "utf8",
    ),
  ).find((row: { role: string }) => row.role === "user");
  await context.request.post(base + "/auth/login", {
    data: { username: user.username, password: user.password },
  });
  const before = new Set(
    (
      await (await context.request.get(base + "/v2/conversations")).json()
    ).items.map((row: { id: string }) => row.id),
  );
  await page.addInitScript(() => {
    // Contract tests avoid changing the system clipboard or playing real sound.
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          sessionStorage.setItem("test-copy", text);
        },
      },
    });
    Object.defineProperty(window, "speechSynthesis", {
      configurable: true,
      value: {
        speak: (utterance: SpeechSynthesisUtterance) =>
          sessionStorage.setItem("test-speech", utterance.text),
        cancel: () => {},
        getVoices: () => [],
      },
    });
  });
  const question = "你好，！。？";
  try {
    await page.goto("/workspace");
    await expect(page.getByRole("textbox", { name: "输入复诊准备问题" })).toBeVisible();
    const selector = page.getByRole("combobox", { name: "选择复诊对象" });
    if (await selector.count()) await selector.selectOption("");
    await page
      .getByRole("textbox", { name: "输入复诊准备问题" })
      .fill(question);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    const actions = page.getByLabel("回答操作", { exact: true });
    await expect(actions).toBeVisible();
    await expect(
      actions.getByText(/\d+ 字 · \d+\.\d 秒(?:生成|回复)/),
    ).toBeVisible();
    await actions
      .getByRole("button", { name: "复制回答", exact: true })
      .click();
    expect(
      await page.evaluate(() => sessionStorage.getItem("test-copy")),
    ).toContain("你好");
    await actions
      .getByRole("button", { name: "朗读回答", exact: true })
      .click();
    expect(
      await page.evaluate(() => sessionStorage.getItem("test-speech")),
    ).toContain("你好");
    await actions
      .getByRole("button", { name: "停止朗读", exact: true })
      .click();
    await expect(
      actions.getByRole("button", { name: "朗读回答", exact: true }),
    ).toBeVisible();
    await actions.getByRole("button", { name: "有帮助", exact: true }).click();
    await expect(
      actions.getByRole("button", { name: "有帮助", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    const conversations = (
      await (await context.request.get(base + "/v2/conversations")).json()
    ).items;
    const original = conversations.find(
      (row: { id: string; title: string }) =>
        !before.has(row.id) && row.title === question,
    );
    await page.reload();
    await page.locator(`button[title="${question}"]`).click();
    await expect(
      actions.getByRole("button", { name: "有帮助", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await actions
      .getByRole("button", { name: "没有帮助", exact: true })
      .click();
    await expect(
      actions.getByRole("button", { name: "有帮助", exact: true }),
    ).toHaveAttribute("aria-pressed", "false");
    await expect(
      actions.getByRole("button", { name: "没有帮助", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await actions
      .getByRole("button", { name: "重新生成", exact: true })
      .click();
    await expect(
      page.getByText("正在处理你的消息…", { exact: true }),
    ).toHaveCount(0);
    await expect(actions).toHaveCount(1);
    await expect(
      actions.getByRole("button", { name: "重新生成", exact: true }),
    ).toBeEnabled();
    const saved = await (
      await context.request.get(base + "/v2/conversations/" + original.id)
    ).json();
    expect(saved.messages).toHaveLength(2);
    expect(saved.messages[1].feedback).toBe("dislike");
    const after = (
      await (await context.request.get(base + "/v2/conversations")).json()
    ).items;
    expect(
      after.some((row: { title: string }) => row.title.includes("重新生成")),
    ).toBeTruthy();
    await page.screenshot({
      path: testInfo.outputPath("answer-actions.png"),
      fullPage: true,
    });
  } finally {
    const conversations = (
      await (await context.request.get(base + "/v2/conversations")).json()
    ).items;
    for (const row of conversations)
      if (!before.has(row.id))
        await context.request.delete(base + "/v2/conversations/" + row.id);
  }
});
