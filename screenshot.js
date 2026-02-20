#!/usr/bin/env node
// Usage: node /usr/local/bin/screenshot.js <url> <output_path>

const { chromium } = require("playwright");

(async () => {
  const [, , url, outputPath] = process.argv;

  if (!url || !outputPath) {
    console.error("Usage: node screenshot.js <url> <output_path>");
    process.exit(1);
  }

  const browser = await chromium.launch({
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
    ],
  });

  const page = await browser.newPage();
  await page.goto(url, { waitUntil: "load" });
  await page.screenshot({ path: outputPath, type: "png" });
  await browser.close();
})();
