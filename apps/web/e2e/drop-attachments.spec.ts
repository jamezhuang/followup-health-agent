import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

test("未选择复诊对象也可拖入文件直接咨询，无须保存档案", async ({ page, context }) => {
  await fixtureAccount(context);
  await page.goto("/workspace");
  await expect(page.getByRole("combobox", { name: "选择复诊对象", exact: true })).toHaveCount(0);
  const data = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(
      new File(["Synthetic report"], "drop.txt", { type: "text/plain" }),
    );
    return transfer;
  });
  const area = page.locator("form").locator("..");
  await area.dispatchEvent("dragenter", { dataTransfer: data });
  await expect(page.getByText("松开即可添加图片或文件")).toBeVisible();
  await area.dispatchEvent("drop", { dataTransfer: data });
  await expect(page.getByLabel("本轮附件", { exact: true })).toContainText("drop.txt");
  await expect(page.getByRole("dialog", { name: "核对报告并保存" })).toHaveCount(0);
  await page.getByRole("textbox", { name: "输入复诊准备问题" }).fill("我的报告检查项目是什么");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText(/按附件自动识别内容/)).toBeVisible();
  await expect(page.getByRole("link", { name: "查看附件 drop.txt" })).toBeVisible();
  expect((await (await context.request.get(fixtureBase+"/v2/subjects")).json()).items).toHaveLength(0);
  await expect(
    page.getByLabel("添加图片或报告", { exact: true }),
  ).toBeEnabled();
  await data.dispose();
});

test("拖入文档可直接问答，图片失败显示具体原因和文件名", async ({
  page,
  context,
}) => {
  await fixtureAccount(context);
  const response = await context.request.post(fixtureBase + "/v2/subjects", {
    data: { name: "拖入验收（虚构）", diseases: ["hypertension"] },
  });
  const subject = (await response.json()).id;
  try {
    await page.goto("/workspace");
    await page
      .getByRole("combobox", { name: "选择复诊对象", exact: true })
      .selectOption(subject);
    const data = await page.evaluateHandle(() => {
      const transfer = new DataTransfer();
      transfer.items.add(
        new File(["Synthetic report"], "drag.txt", { type: "text/plain" }),
      );
      transfer.items.add(
        new File(["Synthetic image"], "drag.png", { type: "image/png" }),
      );
      return transfer;
    });
    const request = page.waitForRequest(
      (req) =>
        req.method() === "POST" && req.url().includes("/v2/chat-attachments?"),
    );
    await page
      .locator("form")
      .locator("..")
      .dispatchEvent("drop", { dataTransfer: data });
    expect(
      (await request)
        .postDataBuffer()!
        .includes(Buffer.from('filename="drag.txt"')),
    ).toBeTruthy();
    await expect(page.getByLabel("本轮附件", { exact: true })).toContainText("drag.txt");
    await expect(page.getByRole("dialog", { name: "核对报告并保存" })).toHaveCount(0);
    // The isolated fixture deliberately disables cloud OCR.
    await expect(page.locator('main [role="alert"]')).toContainText("drag.png");
    await expect(page.locator('main [role="alert"]')).toContainText(
      "尚未启用或未配置凭据",
    );
    await data.dispose();
  } finally {
    await context.request.delete(fixtureBase + "/v2/subjects/" + subject);
  }
});
