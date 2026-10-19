import { expect, type BrowserContext } from "@playwright/test";
export const fixtureBase = "http://127.0.0.1:8003";
export async function fixtureAccount(context: BrowserContext) {
  const response = await context.request.post(fixtureBase + "/auth/register", {
    data: {
      username: "flow" + Date.now().toString().slice(-12),
      password: "test123456",
    },
  });
  expect(response.ok()).toBeTruthy();
  await context.route("http://127.0.0.1:8002/**", async (route) => {
    const response = await route.fetch({
      url: route.request().url().replace(":8002", ":8003"),
    });
    await route.fulfill({ response });
  });
}
