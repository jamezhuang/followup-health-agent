import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

test("批量上传中单个文件失败不阻断后续文件，重复提交复用任务", async ({page,context})=>{
  await fixtureAccount(context);
  await context.request.post(fixtureBase+"/auth/login",{data:{username:"fixtureadmin",password:"test123456"}});
  await page.goto("/workspace");
  await page.getByRole("button",{name:"后台管理",exact:true}).click();
  await page.getByRole("button",{name:"公共知识库",exact:true}).click();
  const prefix="合成批量验收"+Date.now();
  const valid = [1,2].map(i=>({name:`${prefix}-${i}.txt`,mimeType:"text/plain",buffer:Buffer.from(`${prefix} 第${i}份公开资料。`)}));
  const input=page.locator('input[type="file"]');
  await expect(input).toHaveAttribute("multiple","");
  await input.setInputFiles([valid[0],{name:prefix+".exe",mimeType:"application/octet-stream",buffer:Buffer.from("unsupported")},valid[1]]);
  const summary=page.getByRole("region",{name:"批量上传结果"});
  const tasks=page.getByRole("region",{name:"公共知识入库任务"});
  for(const file of valid) await expect(tasks.getByText(file.name,{exact:true}).locator("..")).toContainText("已完成");
  await expect(summary).toHaveCount(0);
  await input.setInputFiles(valid);
  await expect(summary).toHaveCount(0);
  const response=await context.request.get(fixtureBase+"/v2/admin/knowledge-imports");
  const rows=(await response.json()).items.filter((row:{filename:string})=>row.filename.startsWith(prefix));
  expect(rows).toHaveLength(2);
});

test("公共知识上传、核对发布、试检索和撤回", async ({ page, context }, testInfo) => {
  await fixtureAccount(context);
  await context.request.post(fixtureBase + "/auth/login", {
    data: { username: "fixtureadmin", password: "test123456" },
  });
  await page.goto("/workspace");
  await page.getByRole("button", { name: "后台管理", exact: true }).click();
  await page.getByRole("button", { name: "公共知识库", exact: true }).click();
  const title = "合成知识入库验收" + Date.now();
  await page.locator('input[type="file"]').setInputFiles({
    name: title + ".txt", mimeType: "text/plain", buffer: Buffer.from("合成资料：紫杉码头每天上午九时开放。"),
  });
  await expect(page.getByText("原件已留存并创建草稿", { exact: false })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "后台管理", exact: true }).click();
  await page.getByRole("button", { name: "公共知识库", exact: true }).click();
  await expect(page.getByRole("region", { name: "公共知识入库任务" })).toContainText(title);
  await page.getByRole("region", { name: "公共知识入库任务" }).getByText(title+".txt",{exact:true}).locator("../..").getByRole("button", { name: "查看入库资料", exact: true }).click();
  await expect(page.getByLabel("资料名称", { exact: true })).toBeVisible();
  await page.getByLabel("资料名称", { exact: true }).fill(title);
  await page.getByLabel("原始来源链接", { exact: true }).fill("https://example.org/synthetic-guide");
  await page.getByLabel("发布机构", { exact: true }).fill("合成验收机构");
  await page.getByLabel("发布日期", { exact: true }).fill("2026-10-19");
  await page.getByLabel("使用授权 / 许可依据", { exact: true }).fill("合成测试资料");
  await page.getByLabel("适用地区（以顿号分隔，可留空）", { exact: true }).fill("合成省、合成市");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  const card = page.locator("article").filter({ has: page.getByRole("heading", { name: title, exact: true }) });
  await expect(card.getByText("草稿", { exact: true })).toBeVisible();
  await card.getByRole("button", { name: "预览分块", exact: true }).click();
  await expect(page.getByRole("region", { name: "知识片段预览" })).toContainText("不参与问答");
  await page.getByRole("button", { name: "关闭预览", exact: true }).click();
  await card.getByRole("button", { name: "审核发布", exact: true }).click();
  await expect(card.getByText("已发布", { exact: true })).toBeVisible();
  await page.getByLabel("搜索资料",{exact:true}).fill(title);
  await page.getByLabel("发布状态",{exact:true}).selectOption("draft");
  await expect(card).toHaveCount(0);
  await page.getByLabel("发布状态",{exact:true}).selectOption("published");
  await expect(card).toBeVisible();
  await page.getByLabel("搜索资料",{exact:true}).fill("");
  await page.getByLabel("发布状态",{exact:true}).selectOption("");
  await page.getByRole("heading",{name:"公共知识库",exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:testInfo.outputPath("knowledge-workspace-overview.png")});
  await expect(page.getByRole("region",{name:"公共知识发布任务"})).toContainText(title);
  await expect(card.getByRole("link", { name: "下载原件" })).toBeVisible();
  await page.getByLabel("本地知识试检索", { exact: true }).fill("紫杉码头");
  await page.getByRole("button", { name: "试检索", exact: true }).click();
  const results = page.getByRole("region", { name: "知识片段预览" });
  await expect(results).toContainText("未调用外部搜索");
  await results.locator("summary").click();
  await expect(results).toContainText("每天上午九时开放");
  await page.screenshot({ path: testInfo.outputPath("knowledge-published-search.png"), fullPage: true });
  await card.getByRole("button", { name: "撤回", exact: true }).click();
  await expect(card.getByText("已撤回", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "试检索", exact: true }).click();
  await expect(results).toContainText("未找到相关片段");
});
