import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase as base } from "./fixture-api";

test("已有报告兼容、两次当前信息版本、历史查阅与原文件保留", async ({
  page,
  context,
}, testInfo) => {
  await fixtureAccount(context);
  const subject = (
    await (
      await context.request.post(base + "/v2/subjects", {
        data: { name: "历史报告验收（虚构）", diseases: ["hypertension"] },
      })
    ).json()
  ).id;
  try {
    let current = "";
    for (const [name, summary, update] of [
      ["first.txt", "Synthetic first information", true],
      ["archive.md", "Synthetic archive only", false],
      ["latest.txt", "Synthetic latest information", true],
    ] as const) {
      const parsed = await (
        await context.request.post(
          base + "/v2/followup-reports/parse?subject=" + subject,
          {
            multipart: {
              file: {
                name,
                mimeType: "text/plain",
                buffer: Buffer.from(summary),
              },
            },
          },
        )
      ).json();
      const response = await context.request.post(
        base + "/v2/followup-reports",
        {
          data: {
            ...parsed,
            subject,
            summary,
            confirmed: true,
            update_current: update,
            expected_current_id: current,
          },
        },
      );
      expect(response.ok()).toBeTruthy();
      current =
        (
          await (
            await context.request.get(
              base + "/v2/followup-reports?subject=" + subject,
            )
          ).json()
        ).current?.id || "";
    }
    await page.goto("/workspace");
    await page
      .getByRole("combobox", { name: "选择复诊对象", exact: true })
      .selectOption(subject);
    await page.getByRole("button", { name: "复诊档案", exact: true }).click();
    const section = page.getByRole("region", { name: "复诊报告", exact: true });
    await expect(section.locator("article").first()).toContainText(
      "Synthetic latest information",
    );
    await section.getByText("查看以前的复诊信息", { exact: true }).click();
    await expect(section.locator("article").first()).toContainText(
      "Synthetic first information",
    );
    for (const name of ["first.txt", "archive.md", "latest.txt"])
      await expect(
        section.getByRole("heading", { name, exact: true }),
      ).toBeVisible();
    await expect(section.locator('input[type="file"]')).toHaveCount(0);
    const old = section
      .locator("article")
      .filter({
        has: page.getByRole("heading", { name: "first.txt", exact: true }),
      });
    await old.getByText("查看报告内容", { exact: true }).click();
    await expect(old).toContainText("Synthetic first information");
    const saved = await (
      await context.request.get(
        base + "/v2/followup-reports?subject=" + subject,
      )
    ).json();
    expect(saved.items).toHaveLength(3);
    expect(saved.history).toHaveLength(2);
    const original = await context.request.get(
      base + "/v2/followup-reports/" + saved.items[2].id + "/file",
    );
    expect((await original.body()).toString()).toBe(
      "Synthetic first information",
    );
    await page.screenshot({
      path: testInfo.outputPath("followup-reports.png"),
    });
  } finally {
    await context.request.delete(base + "/v2/subjects/" + subject);
  }
});
