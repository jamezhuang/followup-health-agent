import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  workers: 1,
  webServer: {
    command:
      "..\\api\\.venv\\Scripts\\python.exe -m uvicorn tests.serve_review_fixture:app --app-dir ../api --host 127.0.0.1 --port 8003",
    url: "http://127.0.0.1:8003/health",
    reuseExistingServer: true,
    timeout: 30000,
  },
  use: {
    baseURL: "http://127.0.0.1:3002",
    viewport: { width: 1440, height: 1000 },
    browserName: "chromium",
    screenshot: "only-on-failure",
  },
  reporter: [["list"]],
  outputDir: "../../test-results/browser",
});
