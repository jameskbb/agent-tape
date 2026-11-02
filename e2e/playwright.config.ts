import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT ?? 8799);

export default defineConfig({
  testDir: "./tests",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      // SwiftShader GL keeps headless screenshots working under WSL2 and GPU-less CI.
      args: ["--disable-gpu", "--use-gl=swiftshader", "--disable-software-rasterizer"],
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "../scripts/e2e-server.sh",
    url: `http://127.0.0.1:${PORT}/api/v1/ready`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
    env: { E2E_PORT: String(PORT) },
  },
});
