import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

test("历史报告标明所属档案，切换对象不混入旧报告，姓名不一致保留核对提示", async ({
  page,
  context,
}, testInfo) => {
  await fixtureAccount(context);
  async function person(name: string, patient_name: string, relation: string) {
    const response = await context.request.post(fixtureBase + "/v2/subjects", {
      data: { name, patient_name, relation, diseases: [] },
    });
    expect(response.ok()).toBeTruthy();
    return (await response.json()).id as string;
  }
  const father = await person("父亲档案", "合成父亲", "父亲");
  const mother = await person("母亲档案", "合成母亲", "母亲");
  const empty = await person("空档案", "合成空对象", "");
  async function report(
    subject: string,
    filename: string,
    patient: string,
    mismatch = false,
  ) {
    const response = await context.request.post(
      fixtureBase + "/v2/report-reviews?subject=" + subject,
      {
        multipart: {
          file: {
            name: filename,
            mimeType: "text/plain",
            buffer: Buffer.from(
              `姓名：${patient}\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT\n影像学意见：合成报告，仅用于测试。`,
            ),
          },
        },
      },
    );
    expect(response.ok()).toBeTruthy();
    const review = await response.json();
    const url = fixtureBase + "/v2/report-reviews/" + review.id + "/confirm";
    if (mismatch)
      expect(
        (
          await context.request.post(url, {
            data: { revision: review.revision, confirmed: true },
          })
        ).status(),
      ).toBe(422);
    expect(
      (
        await context.request.post(url, {
          data: {
            revision: review.revision,
            confirmed: true,
            identity_confirmed: mismatch,
          },
        })
      ).ok(),
    ).toBeTruthy();
  }
  await report(father, "father-ct.txt", "合成父亲");
  await report(mother, "mother-ct.txt", "合成母亲旧名", true);
  await page.goto("/workspace");
  await page
    .getByRole("combobox", { name: "选择复诊对象" })
    .selectOption(father);
  await page.getByRole("button", { name: "复诊档案", exact: true }).click();
  const panel = page.getByRole("region", { name: "复诊报告", exact: true });
  await expect(panel).toContainText("正在查看：父亲档案（父亲） 的复诊档案");
  const fatherCard = panel
    .locator("article")
    .filter({
      has: page.getByRole("heading", { name: "father-ct.txt", exact: true }),
    });
  await expect(fatherCard).toContainText("所属档案：父亲档案");
  await expect(fatherCard).toContainText("报告患者：合成父亲");
  await expect(
    panel.getByRole("heading", { name: "mother-ct.txt", exact: true }),
  ).toHaveCount(0);
  await panel.screenshot({
    path: testInfo.outputPath("father-archive-ownership.png"),
  });

  // Hold an old request so a rapid subject switch cannot relabel its response.
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let held = false;
  await context.route(
    `**/v2/followup-reports?subject=${mother}`,
    async (route) => {
      held = true;
      await gate;
      const response = await route.fetch({
        url: route.request().url().replace(":8002", ":8003"),
      });
      await route.fulfill({ response });
    },
  );
  const motherButton = page
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: /母亲档案/ }) })
    .getByRole("button", { name: "选择对象", exact: true });
  await motherButton.click();
  await expect.poll(() => held).toBe(true);
  await expect(panel).toContainText("正在加载该对象的报告与复诊记录");
  await expect(
    panel.getByRole("heading", { name: "father-ct.txt", exact: true }),
  ).toHaveCount(0);
  await page
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: /父亲档案/ }) })
    .getByRole("button", { name: "选择对象", exact: true })
    .click();
  const oldResponse = page.waitForResponse((r) =>
    r.url().includes(`/v2/followup-reports?subject=${mother}`),
  );
  release();
  await oldResponse;
  await expect(fatherCard).toBeVisible();
  await expect(panel).toContainText("正在查看：父亲档案");
  await expect(
    panel.getByRole("heading", { name: "mother-ct.txt", exact: true }),
  ).toHaveCount(0);

  await motherButton.click();
  const motherCard = panel
    .locator("article")
    .filter({
      has: page.getByRole("heading", { name: "mother-ct.txt", exact: true }),
    });
  await expect(motherCard).toContainText("所属档案：母亲档案");
  await expect(motherCard).toContainText("报告患者：合成母亲旧名");
  await expect(motherCard).toContainText(
    "报告患者姓名与档案登记姓名「合成母亲」不同",
  );
  await expect(
    panel.getByRole("heading", { name: "father-ct.txt", exact: true }),
  ).toHaveCount(0);
  await panel.screenshot({
    path: testInfo.outputPath("mother-archive-ownership.png"),
  });

  await page
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: "空档案", exact: true }) })
    .getByRole("button", { name: "选择对象", exact: true })
    .click();
  await expect(panel).toContainText("「空档案」暂无已归档报告");
  await expect(panel.getByRole("heading", { name: /ct\.txt/ })).toHaveCount(0);
  for (const id of [father, mother, empty])
    await context.request.delete(fixtureBase + "/v2/subjects/" + id);
});
