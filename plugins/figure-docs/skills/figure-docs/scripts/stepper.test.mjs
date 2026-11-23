import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import { check, build, preview } from "./stepper.mjs";

const here = new URL(".", import.meta.url).pathname;
const example = () => {
  const spec = JSON.parse(fs.readFileSync(`${here}examples/login-lock.json`, "utf8"));
  spec.svg = fs.readFileSync(`${here}examples/login-lock.svg`, "utf8");
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

test("四角が7つあれば断る", () => {
  const spec = example();
  const extra = [1, 2, 3].map((n) => `<g data-box="x${n}" class="c-gray"><rect/></g>`).join("");
  spec.svg = spec.svg.replace("</svg>", `${extra}</svg>`);
  assert.match(check(spec).join("\n"), /四角が 7 個/);
});

test("図に無い要素を論点で指せば断る", () => {
  const spec = example();
  spec.steps[1].focus.push("nowhere");
  assert.match(check(spec).join("\n"), /nowhere/);
});

test("色の値を直接書いた図は断る", () => {
  const spec = example();
  spec.svg = spec.svg.replace('class="c-gray"', 'fill="#ff0000" class="c-gray"');
  assert.match(check(spec).join("\n"), /色の値/);
});

let chromium;
try {
  chromium = createRequire(import.meta.url)("playwright").chromium;
} catch {}

test("ボタンで論点が切り替わり、スクリプトが動かなくても論点の一覧が読める", { skip: !chromium && "playwright が無い" }, async () => {
  const html = build(example());
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const cls = (id) => page.getAttribute(`[data-arrow="${id}"]`, "class");

  await page.setContent(preview(html, "light"));
  assert.match(await cls("lockedin"), /fs-hot/);
  assert.match(await cls("unlock"), /fs-dim/);
  assert.equal(await page.textContent(".fs-point"), "ロック中でも正しく入力するとログインできてしまう");
  assert.equal(await page.textContent(".fs-badge"), "ここから入れてしまう");

  await page.click("[data-go='1']");
  assert.match(await cls("unlock"), /fs-hot/);
  assert.match(await cls("lockedin"), /fs-dim/);
  assert.equal(await page.textContent(".fs-pos"), "2 / 2");

  await page.click("[data-all]");
  assert.equal(await page.locator(".fs-dim").count(), 0);

  const noScript = await browser.newPage({ javaScriptEnabled: false });
  await noScript.setContent(preview(html, "light"));
  assert.equal(await noScript.locator(".pt:visible").count(), 2);
  await browser.close();
});
