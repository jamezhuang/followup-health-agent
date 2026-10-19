import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

test("图片附件为紧凑缩略图，关闭与归档入口仍可操作", async ({ page, context }, testInfo) => {
  await fixtureAccount(context);
  const response = await context.request.post(fixtureBase+"/v2/chat-attachments", {
    multipart: { file: { name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("Synthetic visual fixture") } },
  });
  const attachment = await response.json();
  await page.goto("/workspace");
  const png = await page.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 180; canvas.height = 260;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, 180, 260);
    ctx.fillStyle = "#152749"; ctx.font = "12px sans-serif"; ctx.fillText("SYNTHETIC REPORT", 12, 26);
    for (let y = 48; y < 235; y += 15) { ctx.fillStyle = y % 2 ? "#abb5c5" : "#60708c"; ctx.fillRect(12, y, 150 - y % 30, 3); }
    return canvas.toDataURL("image/png").split(",")[1];
  });
  const title = "synthetic-long-report-name-for-compact-layout.png";
  // Recognition is tested by the API suite; this fixture isolates image layout.
  await context.route("**/v2/chat-attachments?*", route => route.fulfill({ json: { ...attachment, filename: title, title } }));
  await context.route(`**/v2/documents/${attachment.id}/file`, route => route.fulfill({ body: Buffer.from(png, "base64"), contentType: "image/png" }));
  const composer = page.locator("form").filter({ has: page.getByRole("textbox", { name: "输入复诊准备问题" }) });
  const before = await composer.boundingBox();
  await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({ name: title, mimeType: "image/png", buffer: Buffer.from(png, "base64") });
  const preview = page.getByRole("link", { name: `查看附件 ${title}` });
  await expect(preview.getByRole("img")).toBeVisible();
  const bounds = await preview.boundingBox();
  expect(bounds!.width).toBeLessThanOrEqual(84);
  expect(bounds!.height).toBeLessThanOrEqual(84);
  expect((await composer.boundingBox())!.height - before!.height).toBeLessThan(110);
  await expect(page.getByText("查看原图", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("compact-image-composer.png") });
  await page.getByRole("button", { name: `保存到复诊档案 ${title}`, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "保存到复诊档案", exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: `取消选用 ${title}`, exact: true }).click();
  await expect(preview).toHaveCount(0);
});
