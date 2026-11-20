// Headless screenshots for the advisor acceptance (evals/advisor_acceptance.py): each task's Pipeline at 1280 and
// 390 px, and the card of its first consult node, from a disposable server that signs a local browser in on load.
//
//   node evals/advisor_acceptance_shots.cjs <base URL> <output directory> <task id>...
//
// PLAYWRIGHT_CORE names the playwright-core module to load (default "playwright-core"); CHROMIUM, a browser
// executable (default: Playwright's own).
const { chromium } = require(process.env.PLAYWRIGHT_CORE || "playwright-core");

const [base, out, ...tasks] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM && { executablePath: process.env.CHROMIUM }) });
  const report = { shots: [], nodes: {}, errors: [] };
  for (const width of [1280, 390]) {
    const touch = width < 600;
    const context = await browser.newContext({ viewport: { width, height: touch ? 844 : 900 }, colorScheme: "dark",
                                               hasTouch: touch, isMobile: touch, deviceScaleFactor: touch ? 2 : 1 });
    const page = await context.newPage();
    page.on("pageerror", (e) => report.errors.push(`${width} ${e.message}`));
    page.on("console", (m) => m.type() === "error" && report.errors.push(`${width} console ${m.text()}`));
    await page.goto(base);
    await page.waitForTimeout(1000);
    for (const task of tasks) {
      await page.goto(`${base}#tasks/${task}?tab=pipeline`);
      await page.waitForSelector(".react-flow__node", { timeout: 20000 });
      await page.waitForTimeout(1200);
      const pipeline = `${out}/${task}-pipeline-${width}.png`;
      await page.screenshot({ path: pipeline });
      report.shots.push(pipeline);
      report.nodes[`${task}@${width}`] = await page.$$eval('.react-flow__node[data-id^="consult:"]',
        (nodes) => nodes.map((node) => node.textContent));
      const consult = page.locator('.react-flow__node[data-id^="consult:"]').first();
      if (await consult.count()) {
        if (touch) await consult.tap({ force: true }); else await consult.hover();
        await page.waitForTimeout(700);
        const card = `${out}/${task}-card-${width}.png`;
        await page.screenshot({ path: card });
        report.shots.push(card);
        if (touch) await page.keyboard.press("Escape"); else await page.mouse.move(2, 2);
      }
    }
    await context.close();
  }
  await browser.close();
  console.log(JSON.stringify(report));
})().catch((error) => { console.error(error); process.exit(1); });
