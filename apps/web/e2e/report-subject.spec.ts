import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

for (const role of ["user", "admin"]) {
  test(`${role}：无需对象可上传，核对报告后确认创建对象，空档案隐藏选择框`, async ({ page, context }, testInfo) => {
    await fixtureAccount(context);
    if (role === "admin") {
      await context.request.post(fixtureBase+"/auth/login", { data: { username: "fixtureadmin", password: "test123456" } });
      // This account and its synthetic data belong only to the isolated fixture server.
      for (const person of (await (await context.request.get(fixtureBase+"/v2/subjects")).json()).items)
        await context.request.delete(fixtureBase+"/v2/subjects/"+person.id);
    }
    await page.goto("/workspace");
    await expect(page.getByRole("textbox", { name: "输入复诊准备问题" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "选择复诊对象" })).toHaveCount(0);
    await page.getByRole("button", { name: "复诊档案", exact: true }).click();
    await expect(page.getByRole("button", { name: "新增复诊对象" })).toHaveCount(0);
    await page.getByRole("button", { name: "返回对话", exact: true }).click();
    await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
      name: "synthetic-person.txt", mimeType: "text/plain",
      buffer: Buffer.from("姓名：合成患者\n性别：男 年龄：68岁\n检查日期：2026-07-06\n报告日期：2026-07-07\n检查项目：胸部CT平扫+增强"),
    });
    await expect(page.getByLabel("本轮附件", { exact: true })).toContainText("synthetic-person.txt");
    expect((await (await context.request.get(fixtureBase+"/v2/subjects")).json()).items).toHaveLength(0);
    await page.getByRole("button", { name: "保存到复诊档案 synthetic-person.txt", exact: true }).click();
    const chooser = page.getByRole("dialog", { name: "保存到复诊档案", exact: true });
    await expect(chooser.getByLabel("新对象患者姓名")).toHaveValue("合成患者");
    await expect(chooser.getByLabel("新对象性别")).toHaveValue("男");
    await expect(chooser.getByLabel("新对象报告年龄")).toHaveValue("68岁");
    await chooser.getByLabel("新对象姓名 / 称呼").fill("父亲资料");
    await chooser.getByLabel("新对象与我的关系").fill("父亲");
    await page.screenshot({ path: testInfo.outputPath("confirm-report-subject.png") });
    expect((await (await context.request.get(fixtureBase+"/v2/subjects")).json()).items).toHaveLength(0);
    await chooser.getByRole("button", { name: "确认新建对象并核对报告", exact: true }).click();
    const review = page.getByRole("dialog", { name: "核对报告并保存", exact: true });
    await expect(review).toBeVisible();
    await expect(page.getByRole("combobox", { name: "选择复诊对象" })).toHaveCount(1);
    const people = (await (await context.request.get(fixtureBase+"/v2/subjects")).json()).items;
    expect(people).toHaveLength(1);
    expect(people[0].name).toBe("父亲资料"); expect(people[0].patient_name).toBe("合成患者");
    expect(people[0].birth_date).toBe(""); expect(people[0].diseases).toEqual([]);
    await review.getByRole("checkbox", { name: /我已核对患者/ }).check();
    await review.getByRole("button", { name: "确认并保存报告", exact: true }).click();
    await expect(review).not.toBeVisible();
    await page.getByRole("textbox", { name: "输入复诊准备问题" }).fill("这份报告检查项目是什么");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByText(/这份报告的检查项目是/)).toBeVisible();
    await page.getByRole("button", { name: "复诊档案", exact: true }).click();
    await expect(page.getByRole("heading", { name: /父亲资料/ })).toBeVisible();
    await page.getByRole("button", { name: "选择对象", exact: true }).click();
    await expect(page.getByRole("heading", { name: "synthetic-person.txt", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await expect(page.getByLabel("姓名 / 称呼", { exact: true })).toHaveValue("父亲资料");
    await expect(page.getByLabel("对象性别")).toHaveValue("男");
    await expect(page.getByLabel("出生日期", { exact: true })).toHaveValue("");
    await context.request.delete(fixtureBase+"/v2/subjects/"+people[0].id);
  });
}
