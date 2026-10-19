import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase as base } from "./fixture-api";

test("批量报告直接用于对话、附件关联、识别失败与上传布局", async ({
  page,
  context,
}, testInfo) => {
  await fixtureAccount(context);
  const subject = (
    await (
      await context.request.post(base + "/v2/subjects", {
        data: { name: "附件验收（虚构）", diseases: ["hypertension"] },
      })
    ).json()
  ).id;
  try {
    await page.goto("/workspace");
    await page
      .getByRole("combobox", { name: "选择复诊对象", exact: true })
      .selectOption(subject);
    await expect(
      page.getByRole("button", { name: "选择资料", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("combobox", { name: "上传报告类型" }),
    ).toHaveCount(0);
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      page.getByRole("button", { name: "添加文件", exact: true }).click(),
    ]);
    const uploads = page.getByRole("region", {
      name: "上传对话附件",
      exact: true,
    });
    await chooser.setFiles([
      {
        name: "report.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("Synthetic report"),
      },
      {
        name: "history.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("Synthetic history"),
      },
    ]);
    await expect(page.getByRole("dialog", { name: "核对报告并保存" })).toHaveCount(0);
    const attachments = page.getByLabel("本轮附件", { exact: true });
    await expect(attachments).toContainText("report.txt");
    await expect(attachments).toContainText("history.md");
    const saved = (
      await (
        await context.request.get(base + "/v2/documents?subject=" + subject)
      ).json()
    ).items;
    expect(saved).toHaveLength(0);
    const conversations = (await (await context.request.get(base+"/v2/conversations")).json()).items;
    const pending = (await (await context.request.get(base+"/v2/conversations/"+conversations[0].id)).json()).pending_attachments;
    expect(pending).toHaveLength(2);
    await uploads.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
      name: "scan.png",
      mimeType: "image/png",
      buffer: Buffer.from("Synthetic image"),
    });
    await expect(page.locator('main [role="alert"]')).toContainText(
      "图片文字识别暂不可用",
    );
    expect(
      (
        await (
          await context.request.get(
            base + "/v2/report-reviews?subject=" + subject,
          )
        ).json()
      ).items,
    ).toHaveLength(0);
    const plus = await page
      .getByRole("button", { name: "添加文件", exact: true })
      .boundingBox();
    const voice = await page
      .getByRole("button", { name: "语音输入", exact: true })
      .boundingBox();
    expect(Math.abs(plus!.y - voice!.y)).toBeLessThan(1);
    await expect(
      page.getByText("点击选择，或拖入图片和文件", { exact: true }),
    ).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("chat-upload.png") });
    await page.route("**/v2/runs", (route) =>
      route.fulfill({
        json: {
          id: "synthetic-run",
          status: "completed",
          conversation_id: "",
          result: null,
        },
      }),
    );
    const submitted = page.waitForRequest(
      (request) =>
        request.url().endsWith("/v2/runs") && request.method() === "POST",
    );
    await page.getByRole("button", { name: "发送", exact: true }).click();
    const payload = (await submitted).postDataJSON();
    expect(new Set(payload.document_ids)).toEqual(
      new Set(pending.map((row: { id: string }) => row.id)),
    );
    await expect(page.getByLabel("本轮附件", { exact: true })).toHaveCount(0);
  } finally {
    await context.request.delete(base + "/v2/subjects/" + subject);
  }
});
