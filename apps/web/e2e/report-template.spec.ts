import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";
import { archiveAttachment } from "./archive-attachment";

for (const role of ["user", "admin"] as const) {
  test(`${role}：新建对象与影像报告核对、缺失项、补充字段和历史展示`, async ({
    page,
    context,
  }, testInfo) => {
    await fixtureAccount(context);
    if (role === "admin") {
      const login = await context.request.post(fixtureBase + "/auth/login", {
        data: { username: "fixtureadmin", password: "test123456" },
      });
      expect(login.ok()).toBeTruthy();
    }
    const subject = await (await context.request.post(fixtureBase+"/v2/subjects", { data: {
      name: "影像模板验收", patient_name: "合成患者", sex: "女", birth_date: "1990-01-02", age: "36岁",
      medical_history: "合成病史", allergies: "待核对", other_conditions: "影像复查", additional_info: "合成补充资料", diseases: [],
    } })).json();
    await page.goto("/workspace");
    await page.getByRole("combobox", { name: "选择复诊对象" }).selectOption(subject.id);
    await page.getByRole("button", { name: "复诊档案", exact: true }).click();
    await expect(page.getByRole("button", { name: "新增复诊对象" })).toHaveCount(0);
    const form = page.locator("form");
    expect(subject.diseases).toEqual([]);
    expect(subject.allergies).toBe("待核对");
    try {
      await page.locator("article").filter({ has: page.getByRole("heading", { name: /影像模板验收/ }) }).getByRole("button", { name: "编辑", exact: true }).click();
      await expect(form.getByLabel("出生日期", { exact: true })).toHaveValue(
        "1990-01-02",
      );
      await expect(form.getByLabel("过敏史", { exact: true })).toHaveValue(
        "待核对",
      );
      await form.getByRole("button", { name: "取消", exact: true }).click();
      await page.getByRole("button", { name: "返回对话", exact: true }).click();
      await page.getByRole("button", { name: "添加文件", exact: true }).click();
      await page.getByLabel("添加图片或报告", { exact: true }).setInputFiles({
        name: "synthetic-ct.txt",
        mimeType: "text/plain",
        buffer: Buffer.from(
          "合成医院\nCT检查报告单\n姓名：合成患者\n性别：女 年龄：36岁 科室：影像科\n检查日期：2026-07-06 14:41:14\n报告日期：2026-07-07 14:55:51\n检查项目：[薄层扫描、薄层扫描][胸部,CT平扫+增强]\n层厚：5mm\n影像学表现：\n合成影像所见，仅用于验收。\n影像学意见：\n合成报告意见。\n报告医生：合成医生",
        ),
      });
      await archiveAttachment(page, "synthetic-ct.txt");
      const dialog = page.getByRole("dialog", { name: "核对报告并保存" });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByLabel("检查项目", { exact: true })).toHaveValue(
        "胸部 CT 平扫＋增强",
      );
      await expect(dialog.getByLabel("检查部位", { exact: true })).toHaveCount(
        0,
      );
      await expect(
        dialog.getByLabel("检查方法 / 扫描方式", { exact: true }),
      ).toHaveCount(0);
      await expect(
        dialog.getByRole("heading", { name: "患者与就诊信息", exact: true }),
      ).toHaveCount(0);
      await expect(dialog.getByLabel("报告年龄", { exact: true })).toHaveCount(
        0,
      );
      await expect(dialog.getByLabel("审核医生", { exact: true })).toHaveValue(
        "",
      );
      await expect(
        dialog.getByLabel("审核医生", { exact: true }),
      ).toHaveAttribute("placeholder", "无");
      await expect(
        dialog.getByLabel("影像表现 / 检查所见", { exact: true }),
      ).toContainText("合成影像所见");
      await expect(
        dialog.getByRole("heading", { name: "其他补充信息", exact: true }),
      ).toHaveCount(0);
      await expect(
        dialog.getByRole("button", { name: "添加补充字段", exact: true }),
      ).toHaveCount(0);
      await dialog
        .getByLabel("影像表现 / 检查所见", { exact: true })
        .fill("用户核对后的合成所见。");
      await dialog
        .getByRole("button", { name: "保存草稿，稍后核对", exact: true })
        .click();
      await expect(dialog).not.toBeVisible();
      await archiveAttachment(page, "synthetic-ct.txt");
      await expect(
        dialog.getByLabel("影像表现 / 检查所见", { exact: true }),
      ).toHaveValue("用户核对后的合成所见。");
      await expect(
        dialog.getByRole("heading", { name: "其他补充信息", exact: true }),
      ).toHaveCount(0);
      await dialog
        .getByLabel("报告标题", { exact: true })
        .scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath("report-template.png"),
      });
      await dialog.getByRole("checkbox", { name: /我已核对患者/ }).check();
      await dialog
        .getByRole("button", { name: "确认并保存报告", exact: true })
        .click();
      await expect(dialog).not.toBeVisible();
      const documents = (
        await (
          await context.request.get(
            fixtureBase + "/v2/documents?subject=" + subject.id,
          )
        ).json()
      ).items;
      expect(documents[0].key_fields.details.patient_age).toBe("36岁");
      expect(documents[0].key_fields.details.patient_sex).toBe("女");
      expect(documents[0].key_fields.details.department).toBe("影像科");
      expect(documents[0].key_fields.details.examination_body_part).toBe(
        "胸部",
      );
      expect(documents[0].key_fields.details.examination_modality).toBe("CT");
      expect(documents[0].key_fields.details.examination_method).toBe(
        "平扫＋增强",
      );
      expect(
        documents[0].key_fields.details.examination_technical_details,
      ).toBe("薄层扫描");
      expect(
        documents[0].key_fields.additional_fields.some(
          (item: { label: string; value: string }) =>
            item.label === "层厚" && item.value === "5mm",
        ),
      ).toBeTruthy();
      await page.getByRole("button", { name: "复诊档案", exact: true }).click();
      await page.getByText("查看已核对的报告字段", { exact: true }).click();
      await expect(
        page.getByText("用户核对后的合成所见。", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "其他补充信息", exact: true }),
      ).toHaveCount(0);
    } finally {
      await context.request.delete(fixtureBase + "/v2/subjects/" + subject.id);
    }
  });
}
