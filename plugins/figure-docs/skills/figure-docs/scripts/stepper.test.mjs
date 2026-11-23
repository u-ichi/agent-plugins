import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import { check, build, preview } from "./stepper.mjs";

const here = new URL(".", import.meta.url).pathname;
const example = () => {
  const spec = JSON.parse(fs.readFileSync(`${here}examples/expense-approval.json`, "utf8"));
  spec.svg = fs.readFileSync(`${here}examples/expense-approval.svg`, "utf8");
  return spec;
};

test("見本の指定は検査を通る", () => {
  assert.deepEqual(check(example()), []);
});

test("論点が1つなら切り替えずに図1枚にするよう断る", () => {
  const spec = example();
  spec.steps = spec.steps.slice(0, 1);
  assert.match(check(spec).join("\n"), /論点が2つ未満/);
});

test("図に無い要素を論点で指せば断る", () => {
  const spec = example();
  spec.steps[1].focus.push("nowhere");
  assert.match(check(spec).join("\n"), /nowhere/);
});

test("色の値を直接書いた図は断る", () => {
  const spec = example();
  spec.svg = spec.svg.replace('class="c-blue"', 'fill="#ff0000" class="c-blue"');
  assert.match(check(spec).join("\n"), /色の値/);
});

let chromium;
try {
  chromium = createRequire(import.meta.url)("playwright").chromium;
} catch {}

test("ボタンで論点が切り替わり、スクリプトが動かなくても論点の一覧が読める", { skip: !chromium && "playwright が無い" }, async () => {
  const html = build(example());
  const browser = await chromium.launch();
  try {
  const page = await browser.newPage();
  const cls = async (id) => (await page.getAttribute(`[data-arrow="${id}"]`, "class")) ?? "";

  await page.setContent(preview(html, "light"));
  assert.match(await cls("bossok"), /fs-hot/);
  assert.doesNotMatch(await cls("auto"), /fs-hot/);
  assert.equal(await page.textContent(".fs-point"), "10万円を超えても、上長が承認すると部長を通らずに経理へ進む");
  assert.equal(await page.textContent(".fs-badge"), "金額を見ずに経理へ送る");

  await page.click("li[data-step='1'] button");
  assert.match(await cls("auto"), /fs-hot/);
  assert.doesNotMatch(await cls("bossok"), /fs-hot/);
  const pressed = (sel) => page.getAttribute(sel, "aria-pressed");
  assert.equal(await pressed("li[data-step='0'] button"), "false");
  assert.equal(await pressed("li[data-step='1'] button"), "true");

  await page.click("[data-all]");
  assert.equal(await page.locator(".fs-hot").count(), 0);
  assert.equal(await pressed("li[data-step='1'] button"), "false");
  assert.equal(await pressed("[data-all]"), "true");
  assert.match(await page.getAttribute('[data-box="head"]', "class"), /c-blue/);

  const noScript = await browser.newPage({ javaScriptEnabled: false });
  await noScript.setContent(preview(html, "light"));
  assert.equal(await noScript.locator(".pt:visible").count(), 2);
  } finally {
    await browser.close();
  }
});
