import {test,expect} from '@playwright/test';
import {fixtureAccount,fixtureBase} from './fixture-api';
test('巡检快照人工采用为草稿并显示暂停发布提示',async({page,context},testInfo)=>{
  await fixtureAccount(context);
  await context.request.post(fixtureBase+'/auth/login',{data:{username:'fixtureadmin',password:'test123456'}});
  const task={id:'inspection-ui',document_id:'synthetic-doc',title:'合成来源巡检',target_revision:1,url:'https://example.org/guide',status:'completed',outcome:'changed',error_code:'',review_state:'unreviewed',attempts:1};
  for(const path of ['knowledge','knowledge-imports','knowledge-publications'])await context.route(`**/v2/admin/${path}`,route=>route.fulfill({json:{items:[]}}));
  await context.route('**/v2/admin/knowledge-inspections',route=>route.fulfill({json:{items:[task]}}));
  await context.route('**/v2/admin/knowledge-inspections/inspection-ui',route=>route.fulfill({json:{...task,before_text:'旧版网页正文',published_text:'知识库节选',snapshot:{text:'新版网页正文',url:task.url,authority:'unverified',published_at:'',collected_at:'2026-10-19'}}}));
  await context.route('**/v2/admin/knowledge-inspections/inspection-ui/review',async route=>{
    expect(route.request().postDataJSON()).toEqual({action:'apply'});task.review_state='applied';await route.fulfill({json:{status:'draft',review_state:'applied'}});
  });
  await page.goto('/workspace');
  await page.getByRole('button',{name:'后台管理',exact:true}).click();
  await page.getByRole('button',{name:'公共知识库',exact:true}).click();
  const panel=page.getByRole('region',{name:'资料更新巡检',exact:true});
  await panel.getByRole('button',{name:'查看巡检对比'}).click();
  const detail=page.getByRole('region',{name:'巡检快照对比'});
  await expect(detail).toContainText('旧版网页正文');await expect(detail).toContainText('新版网页正文');
  await expect(detail).toContainText('未提取到，需人工核对');
  await detail.scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('knowledge-inspection.png')});
  await detail.getByRole('button',{name:'采用为草稿（暂停当前发布）'}).click();
  await expect(panel).toContainText('当前资料暂停发布');
  await expect(detail.getByRole('button',{name:'采用为草稿（暂停当前发布）'})).toHaveCount(0);
});
