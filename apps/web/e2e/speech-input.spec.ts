import { test, expect } from "@playwright/test";
import { fixtureAccount } from "./fixture-api";

test.beforeEach(async ({ context, page }) => {
  await fixtureAccount(context);
  await page.route("**/v2/recognition/capabilities", (route) =>
    route.fulfill({ json: { asr_ready: true } }),
  );
  // A synthetic tone exercises real browser recording and WAV conversion without
  // accessing a physical microphone or sending audio to Tencent.
  await page.addInitScript(() => {
    Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
      configurable: true,
      value: async () => {
        const audio = new AudioContext();
        await audio.resume();
        const oscillator = audio.createOscillator();
        const destination = audio.createMediaStreamDestination();
        oscillator.connect(destination);
        oscillator.start();
        destination.stream.getTracks().forEach((track) => {
          const stop = track.stop.bind(track);
          track.stop = () => {
            stop();
            oscillator.stop();
            void audio.close();
            sessionStorage.setItem("mic-stopped", "true");
          };
        });
        return destination.stream;
      },
    });
  });
});

test("录音转WAV后追加草稿，核对后手动发送", async ({ page }) => {
  let calls = 0;
  await page.route("**/v2/speech/recognize", async (route) => {
    calls++;
    const body = route.request().postDataBuffer()!;
    expect(body.includes(Buffer.from('filename="speech.wav"'))).toBeTruthy();
    const offset = body.indexOf(Buffer.from("RIFF"));
    expect(offset).toBeGreaterThan(0);
    expect(body.toString("ascii", offset + 8, offset + 12)).toBe("WAVE");
    expect(body.readUInt32LE(offset + 24)).toBe(16000);
    expect(body.readUInt16LE(offset + 22)).toBe(1);
    await route.fulfill({
      json: { text: "这是一段测试语音", requires_confirmation: true },
    });
  });
  await page.goto("/workspace");
  const input = page.getByRole("textbox", { name: "输入复诊准备问题" });
  await input.fill("已有草稿");
  await page.getByRole("button", { name: "语音输入", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "结束录音并识别" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "结束录音并识别" }),
  ).toContainText("1s");
  await page.getByRole("button", { name: "结束录音并识别" }).click();
  await expect(input).toHaveValue("已有草稿\n这是一段测试语音");
  await expect(page.getByText("已填入输入框，请核对后发送。")).toBeVisible();
  expect(calls).toBe(1);
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeEnabled();
  expect(await page.evaluate(() => sessionStorage.getItem("mic-stopped"))).toBe(
    "true",
  );
  await page.screenshot({
    path: "../../test-results/speech-input-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: "语音输入", exact: true }),
  ).toBeVisible();
  await page.screenshot({ path: "../../test-results/speech-input-mobile.png" });
});

test("取消录音释放麦克风且不上传", async ({ page }) => {
  let calls = 0;
  await page.route("**/v2/speech/recognize", async (route) => {
    calls++;
    await route.abort();
  });
  await page.goto("/workspace");
  await page.getByRole("button", { name: "语音输入", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "结束录音并识别" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "取消录音" }).click();
  await expect(page.getByText("已取消，录音不会填入消息。")).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "输入复诊准备问题" }),
  ).toHaveValue("");
  expect(calls).toBe(0);
  expect(await page.evaluate(() => sessionStorage.getItem("mic-stopped"))).toBe(
    "true",
  );
});

test("按住空格录音、松开识别，编辑文字时保留普通空格", async ({ page }) => {
  let calls = 0;
  await page.route("**/v2/speech/recognize", (route) => {
    calls++;
    return route.fulfill({ json: { text: "快捷键录音测试" } });
  });
  await page.goto("/workspace");
  await expect(
    page.getByRole("button", { name: "语音输入", exact: true }),
  ).toBeVisible();
  await page.keyboard.down("Space");
  await expect(
    page.getByRole("button", { name: "结束录音并识别" }),
  ).toContainText("1s");
  await page.keyboard.up("Space");
  const input = page.getByRole("textbox", { name: "输入复诊准备问题" });
  await expect(input).toHaveValue("快捷键录音测试");
  await input.fill("hello");
  await input.press("End");
  await input.press("Space");
  await expect(input).toHaveValue("hello ");
  await expect(
    page.getByRole("button", { name: "结束录音并识别" }),
  ).toHaveCount(0);
  expect(calls).toBe(1);
});

test("权限拒绝和后端未配置均提供可恢复提示", async ({ page }) => {
  await page.goto("/workspace");
  await page.evaluate(() =>
    Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
      value: async () => {
        throw new DOMException("denied", "NotAllowedError");
      },
    }),
  );
  await page.getByRole("button", { name: "语音输入", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "麦克风权限未开启" }),
  ).toBeVisible();
  await page.route("**/v2/recognition/capabilities", (route) =>
    route.fulfill({ json: { asr_ready: false } }),
  );
  await page.getByRole("button", { name: "语音输入", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "语音识别尚未就绪" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "语音输入", exact: true }),
  ).toBeEnabled();
});

test("识别服务失败保留已有草稿并释放发送按钮", async ({ page }) => {
  await page.route("**/v2/speech/recognize", (route) =>
    route.fulfill({
      status: 502,
      json: { detail: "腾讯云识别调用失败，请稍后重试。" },
    }),
  );
  await page.goto("/workspace");
  const input = page.getByRole("textbox", { name: "输入复诊准备问题" });
  await input.fill("保留这段文字");
  await page.getByRole("button", { name: "语音输入", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "结束录音并识别" }),
  ).toContainText("1s");
  await page.getByRole("button", { name: "结束录音并识别" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "腾讯云识别调用失败" }),
  ).toBeVisible();
  await expect(input).toHaveValue("保留这段文字");
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeEnabled();
});
