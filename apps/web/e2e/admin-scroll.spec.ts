import { test, expect } from "@playwright/test";
import { fixtureAccount, fixtureBase } from "./fixture-api";

test("完整ID精确定位显示已知和未知消耗，可重试指定任务",async({page,context},testInfo)=>{
  await fixtureAccount(context);
  await context.request.post(fixtureBase+"/auth/login",{data:{username:"fixtureadmin",password:"test123456"}});
  const identifier="synthetic-exact-task";
  let status="failed";
  await context.route("**/v2/admin/tasks/**",route=>{
    if(!route.request().url().includes(`/tasks/${identifier}?`))return route.fulfill({status:404,json:{detail:"未找到任务，请确认完整ID和任务类型。"}});
    return route.fulfill({json:{task:{id:identifier,type:"publication",status,stage:status,error_code:status==="failed"?"publication_failed":"",attempts:1},
      usage:{calls:3,known_usage_calls:2,unknown_usage_calls:1,known_total_tokens:32,known_input_tokens:30,known_output_tokens:2,
        model_request_sum_ms:300,unknown_duration_calls:1,includes_retries:true,price_status:"not_calculated"},
      usage_attribution:"task_id",legacy_document_calls_present:true,lifecycle_elapsed_ms:5000}});
  });
  await context.route(`**/v2/admin/knowledge-publications/${identifier}/retry`,route=>{status="queued";return route.fulfill({status:202,json:{id:identifier,status}});});
  await page.goto("/workspace");
  await page.getByRole("button",{name:"后台管理",exact:true}).click();
  const panel=page.getByRole("region",{name:"任务精确定位"});
  await panel.getByLabel("完整任务 ID",{exact:true}).fill(identifier);
  await panel.getByRole("button",{name:"查询任务",exact:true}).click();
  await expect(panel).toContainText("已知 Token：32");
  await expect(panel).toContainText("用量未知 1 次");
  await expect(panel).toContainText("累计耗时：300 ms");
  await expect(panel).toContainText("不是完整历史消耗");
  await page.screenshot({path:testInfo.outputPath("task-lookup.png")});
  await panel.getByRole("button",{name:"重试此任务",exact:true}).click();
  await expect(panel).toContainText("排队中");
  await panel.getByLabel("完整任务 ID",{exact:true}).fill("synthetic-prefix");
  await panel.getByRole("button",{name:"查询任务",exact:true}).click();
  await expect(panel.getByRole("alert")).toContainText("未找到任务");
  await expect(panel.getByText("已知 Token：32",{exact:false})).toHaveCount(0);
});

test("运行观测汇总知识任务和引用检查，并能进入知识任务处理",async({page,context},testInfo)=>{
  await fixtureAccount(context);
  await context.request.post(fixtureBase+"/auth/login",{data:{username:"fixtureadmin",password:"test123456"}});
  // This case is a mocked navigation check; keep its destination requests local too.
  for(const path of ["knowledge","knowledge-imports","knowledge-publications","knowledge-inspections"]){
    await context.route(`**/v2/admin/${path}`,route=>route.fulfill({json:{items:[]}}));
  }
  await context.route("**/v2/admin/dashboard",route=>route.fulfill({json:{
    updated_at:"2026-10-19T07:00:00Z",window:"合成观测验收",metrics:{calls:0,success_rate:null,known_tokens:0,unknown_usage:0,p50_ms:null,p95_ms:null},
    failures:{},agents:{},trend:[],calls:[],runs:[],
    knowledge_observability:{queues:{import:{queued:2,processing:1,completed:3,failed:1},publication:{queued:0,processing:0,completed:2,failed:1}},
      recent_failures:[{id:"synthetic-import-failure",type:"import",error_code:"utf8_required",attempts:1,updated_at:"2026-10-19T07:00:00Z"}],
      citations:{window:"最近100条对话任务",run_count:7,attempted:5,passed:2,pass_rate:.4,
        statuses:{passed:1,repaired:1,failed:1,failed_after_answer_review:1,unavailable:1,not_applicable:1,not_recorded:1},
        answer_kinds:{partial:3},recent_failures:[{id:"synthetic-citation-failure",status:"failed_after_answer_review",updated_at:"2026-10-19T07:00:00Z"}]}}
  }}));
  await page.goto("/workspace");
  await page.getByRole("button",{name:"后台管理",exact:true}).click();
  const panel=page.getByRole("region",{name:"知识任务与引用观测"});
  await expect(panel).toContainText("引用核验通过率：40.0%");
  await expect(panel).toContainText("不代表事实正确率");
  await expect(panel).toContainText("文件不是UTF-8编码");
  await expect(panel).toContainText("事实修正后引用失效");
  await expect(panel.getByRole("button",{name:"查看执行链路"})).toBeVisible();
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({path:testInfo.outputPath("knowledge-observability.png")});
  await panel.getByRole("button",{name:"管理知识任务 / 重试",exact:true}).click();
  await expect(page.getByRole("heading",{name:"公共知识库",exact:true})).toBeVisible();
});

test("后台长列表在各自区域内滚动，表头保留且不撑高统计卡片", async ({ page, context }, testInfo) => {
  await fixtureAccount(context);
  await context.request.post(fixtureBase + "/auth/login", { data: { username: "fixtureadmin", password: "test123456" } });
  const data={
    updated_at:"2026-10-18T13:00:00Z",window:"合成滚动验收",metrics:{calls:80,success_rate:.9,known_tokens:1000,unknown_usage:0,p50_ms:100,p95_ms:300},
    agents:Object.fromEntries(Array.from({length:35},(_,i)=>[`合成 Agent ${i+1}`,i+1])),
    failures:Object.fromEntries(Array.from({length:35},(_,i)=>[`合成失败类型 ${i+1}`,i+1])),
    trend:Array.from({length:12},(_,i)=>({hour:`2026-10-18T${String(i).padStart(2,'0')}`,count:i+1})),
    calls:Array.from({length:80},(_,i)=>({id:`call-${i}`,run_id:`synthetic-run-${i}`,agent:"合成 Agent",purpose:"daily_chat",model:"synthetic",attempt:1,started_at:"2026-10-18T13:00:00Z",status:"ok",input_tokens:10,output_tokens:10,total_tokens:20,duration_ms:100})),
    runs:Array.from({length:80},(_,i)=>({id:`synthetic-run-${i}`,created_at:"2026-10-18T13:00:00Z",status:"completed",mode:"local",duration_ms:100,error_code:""})),
  };
  await context.route('**/v2/admin/dashboard',route=>route.fulfill({json:data}));
  await page.goto('/workspace');
  await page.getByRole('button',{name:'后台管理',exact:true}).click();
  await expect(page.getByRole('heading',{name:'每小时调用量',exact:true})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath('observation-dashboard-overview.png')});
  for (const name of ['Agent 调用分布列表','失败原因分布列表','调用明细滚动区域','最近任务滚动区域']) {
    const region=page.getByRole('region',{name,exact:true});
    await expect(region).toBeVisible();
    const dimensions=await region.evaluate(el=>({height:el.clientHeight,total:el.scrollHeight}));
    expect(dimensions.height).toBeLessThanOrEqual(name.includes('分布')?251:481);
    expect(dimensions.total).toBeGreaterThan(dimensions.height);
    await region.focus();
    await region.press('End');
    await expect.poll(()=>region.evaluate(el=>el.scrollTop)).toBeGreaterThan(0);
  }
  const card=page.locator('article').filter({has:page.getByRole('heading',{name:'Agent 调用分布',exact:true})});
  expect((await card.boundingBox())!.height).toBeLessThan(450);
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({path:testInfo.outputPath('admin-local-scrollbars.png')});
  await page.setViewportSize({width:390,height:844});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

