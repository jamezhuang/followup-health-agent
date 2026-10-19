import { expect, type Page } from "@playwright/test";
export async function archiveAttachment(page: Page, title: string) {
  await page.getByRole("button", { name: `保存到复诊档案 ${title}`, exact: true }).click();
  const chooser = page.getByRole("dialog", { name: "保存到复诊档案", exact: true });
  await expect(chooser).toBeVisible();
  await chooser.getByRole("button", { name: "核对并保存", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "核对报告并保存", exact: true })).toBeVisible();
}
