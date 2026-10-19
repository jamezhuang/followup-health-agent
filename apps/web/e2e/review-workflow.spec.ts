import { test, expect } from "@playwright/test";
import { archiveAttachment } from "./archive-attachment";

test("报告弹窗暂存恢复、关键字段确认、自动归档、版本与医嘱待办", async ({
  page,
  context,
}, testInfo) => {
  const base = "http://127.0.0.1:8003";
  const account = "flow" + Date.now().toString().slice(-12);
  const login = await context.request.post(base + "/auth/register", {
    data: { username: account, password: "test123456" },
  });
  expect(login.ok()).toBeTruthy();
  await context.route("http://127.0.0.1:8002/**", async (route) => {
    const response = await route.fetch({
      url: route.request().url().replace(":8002", ":8003"),
    });
    await route.fulfill({ response });
  });
  const father = (
    await (
      await context.request.post(base + "/v2/subjects", {
        data: {
          name: "爸爸",
          patient_name: "张父",
          relation: "父亲",
          diseases: ["diabetes", "hypertension"],
        },
      })
    ).json()
  ).id;
  const mother = (
    await (
      await context.request.post(base + "/v2/subjects", {
        data: {
          name: "妈妈",
          patient_name: "李母",
          relation: "母亲",
          diseases: ["hypertension"],
        },
      })
    ).json()
  ).id;
  await page.goto("/workspace");
  await page
    .getByRole("combobox", { name: "选择复诊对象", exact: true })
    .selectOption(father);
  await page.getByRole("button", { name: "添加文件", exact: true }).click();
  await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
    name: "check.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(
      "姓名：张父\n报告日期：2026-10-11\n检查日期：2026-10-10\n空腹血糖 6.2\n收缩压 135 mmHg",
    ),
  });
  await archiveAttachment(page, "check.txt");
  let dialog = page.getByRole("dialog", { name: "核对报告并保存" });
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByLabel("检查 / 就诊日期", { exact: true }),
  ).toHaveValue("2026-10-10");
  await expect(dialog.getByLabel("报告日期", { exact: true })).toHaveValue(
    "2026-10-11",
  );
  const hospitalBox = await dialog
    .getByLabel("医院 / 医疗机构", { exact: true })
    .boundingBox();
  const reportDateBox = await dialog
    .getByLabel("报告日期", { exact: true })
    .boundingBox();
  expect(Math.abs(hospitalBox!.y - reportDateBox!.y)).toBeLessThan(1);
  expect(reportDateBox!.x).toBeGreaterThan(hospitalBox!.x);
  await dialog.getByLabel("报告日期", { exact: true }).fill("2026-10-12");
  await page.screenshot({
    path: testInfo.outputPath("separate-report-dates.png"),
  });
  await expect(
    dialog.getByRole("button", { name: "确认并保存报告", exact: true }),
  ).toBeDisabled();
  expect(
    (
      await (
        await context.request.get(base + "/v2/documents?subject=" + father)
      ).json()
    ).items,
  ).toHaveLength(0);
  await dialog
    .getByRole("button", { name: "采用 mmol/L", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "保存草稿，稍后核对", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await page.reload();
  await page
    .getByRole("combobox", { name: "选择复诊对象", exact: true })
    .selectOption(father);
  await page.getByTitle("check.txt", { exact: true }).click();
  await archiveAttachment(page, "check.txt");
  dialog = page.getByRole("dialog", { name: "核对报告并保存" });
  await expect(
    dialog.getByLabel("检查 / 就诊日期", { exact: true }),
  ).toHaveValue("2026-10-10");
  await expect(dialog.getByLabel("报告日期", { exact: true })).toHaveValue(
    "2026-10-12",
  );
  await expect(dialog.getByLabel("单位", { exact: true }).first()).toHaveValue(
    "mmol/L",
  );
  await dialog.getByRole("checkbox", { name: /我已核对患者/ }).check();
  await dialog
    .getByRole("button", { name: "确认并保存报告", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByLabel("本轮附件", { exact: true })).toContainText(
    "check.txt",
  );
  const records = (
    await (
      await context.request.get(base + "/v2/records?subject=" + father)
    ).json()
  ).items;
  expect(records).toHaveLength(2);
  expect(
    records.every((row: { date: string }) => row.date === "2026-10-10"),
  ).toBeTruthy();
  expect(
    records.find((row: { label: string }) => row.label === "空腹血糖")
      .unit_source,
  ).toBe("user_confirmed");
  await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
    name: "visit.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(
      "姓名：张父\n就诊日期：2026-10-17\n空腹血糖 6.3 mmol/L\n药品：合成验收用药\n医嘱：下次复诊携带血压记录",
    ),
  });
  await archiveAttachment(page, "visit.txt");
  dialog = page.getByRole("dialog", { name: "核对报告并保存" });
  await expect(dialog.getByLabel("药品名称", { exact: true })).toHaveValue(
    "合成验收用药",
  );
  await dialog.getByLabel("资料类型", { exact: true }).selectOption("followup");
  await dialog.getByRole("checkbox", { name: /同时更新当前复诊信息/ }).check();
  await dialog
    .getByRole("textbox", { name: /复诊信息摘要/ })
    .fill("本次已核对的复诊信息。");
  await dialog.getByRole("checkbox", { name: /我已核对以上事项/ }).check();
  await dialog.getByRole("checkbox", { name: /我已核对患者/ }).check();
  await page.screenshot({
    path: testInfo.outputPath("report-review-dialog.png"),
  });
  await dialog
    .getByRole("button", { name: "保存并更新复诊信息", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await page
    .getByRole("textbox", { name: "输入复诊准备问题", exact: true })
    .fill("比较爸爸两次空腹血糖的变化");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText(/空腹血糖：2026-10-10 的 6.2/)).toBeVisible();
  await expect(page.getByText(/生成后档案或资料已更新/)).toHaveCount(0);
  await page.getByRole("button", { name: "复诊档案", exact: true }).click();
  const reports = page.getByRole("region", { name: "复诊报告", exact: true });
  await expect(reports).toContainText("本次已核对的复诊信息。");
  await expect(
    reports.getByRole("heading", { name: "check.txt", exact: true }),
  ).toBeVisible();
  await expect(
    reports.getByRole("heading", { name: "visit.txt", exact: true }),
  ).toBeVisible();
  await expect(
    reports.getByRole("heading", { name: "下次复诊携带血压记录", exact: true }),
  ).toBeVisible();
  await reports.getByRole("button", { name: "标记完成", exact: true }).click();
  await expect(
    reports.getByRole("button", { name: "恢复待办", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("上传复诊报告", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(reports).toContainText("本次已核对的复诊信息。");
  await page.getByRole("button", { name: "返回对话" }).click();
  await page
    .getByRole("combobox", { name: "选择复诊对象", exact: true })
    .selectOption(mother);
  await page.getByRole("button", { name: "复诊档案", exact: true }).click();
  await expect(
    reports.getByRole("heading", { name: "visit.txt", exact: true }),
  ).toHaveCount(0);
  await expect(reports).toContainText("暂无历史报告");
  await page.screenshot({
    path: testInfo.outputPath("mother-independent-archive.png"),
  });
  await context.request.delete(base + "/v2/subjects/" + father);
  await context.request.delete(base + "/v2/subjects/" + mother);
});
