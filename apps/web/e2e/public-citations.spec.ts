import {test,expect} from "@playwright/test";
import {fixtureAccount} from "./fixture-api";

test("已核验引用可点击并展开原文，未通过引用不显示",async({page,context},testInfo)=>{
  await fixtureAccount(context);
  const base={kind:"chat",intent:"daily",consultation_type:"medical_education",claims:[],questions:[],gaps:[],sources:[],records:[],mode:"live",notice:"",iterations:0,rejected_claims:0,stop_reason:"chat_completed"};
  const citation={source_id:"original:2:0:0",label:"S1",quote:"合成路10号。",url:"https://example.org/guide",title:"合成交通指南",location:"第1页"};
  const conversation={id:"citation-fixture",subject:"",revision:1,title:"合成引用展示验收",pending_attachments:[],messages:[
    {role:"user",content:"合成地址在哪里？"},
    {role:"assistant",run_id:"citation-fixture-1",result:{...base,content:"合成地址是合成路10号。[S1]",citations:[citation],citation_review:{status:"passed"}}},
    {role:"user",content:"未通过的引用呢？"},
    {role:"assistant",run_id:"citation-fixture-2",result:{...base,content:"这条引用没有通过。[S2]",citations:[{...citation,label:"S2"}],citation_review:{status:"failed"}}},
  ]};
  await context.route("**/v2/conversations",route=>route.fulfill({json:{items:[conversation]}}));
  await context.route("**/v2/conversations/citation-fixture",route=>route.fulfill({json:conversation}));
  await page.goto("/workspace");
  await page.getByRole("button",{name:"◯　合成引用展示验收",exact:true}).click();
  await expect(page.getByRole("link",{name:"S1",exact:true})).toHaveAttribute("href",citation.url);
  await page.getByText("查看引用原文（1条）",{exact:true}).click();
  await expect(page.locator("blockquote")).toContainText(citation.quote);
  await expect(page.getByRole("link",{name:"S2",exact:true})).toHaveCount(0);
  await expect(page.getByText("[S2]",{exact:false})).toHaveCount(0);
  await page.screenshot({path:testInfo.outputPath("public-citations.png")});
});
