#!/usr/bin/env node
// 同じ図の上の論点を「戻る／次へ」で切り替える HTML の断片を作る。
// 使い方: node stepper.mjs SPEC.json [--out FILE] [--preview DIR]
import fs from "node:fs";
import path from "node:path";

const MAX_BOXES = 6;
const HEX_OR_RGB = /#[0-9a-fA-F]{3,8}\b|rgba?\(/;

export function check(spec) {
  const errors = [];
  const svg = spec.svg ?? "";
  if (!spec.title) errors.push("title がありません。");
  if (!svg.trim().startsWith("<svg")) errors.push("svg が <svg> で始まっていません。");
  if (HEX_OR_RGB.test(svg)) errors.push("svg に色の値（#… か rgb(…)）があります。色は class で指定します。");
  const boxes = [...svg.matchAll(/data-box="([^"]+)"/g)].map((m) => m[1]);
  const arrows = [...svg.matchAll(/data-arrow="([^"]+)"/g)].map((m) => m[1]);
  const ids = new Set([...boxes, ...arrows]);
  if (boxes.length > MAX_BOXES) errors.push(`四角が ${boxes.length} 個あります。${MAX_BOXES} 個までにするか、範囲を分けた図にします。`);
  const steps = spec.steps ?? [];
  if (steps.length < 2) errors.push("論点が2つ未満です。論点が1つなら切り替えずに図1枚にします。");
  steps.forEach((s, k) => {
    const n = k + 1;
    if (!s.name) errors.push(`論点${n}に name がありません。`);
    if (!s.point) errors.push(`論点${n}に point（図の下に出す結論1行）がありません。`);
    const focus = s.focus ?? [];
    if (focus.length === 0) errors.push(`論点${n}の focus が空です。`);
    for (const id of focus) if (!ids.has(id)) errors.push(`論点${n}の focus の ${id} が svg の data-box / data-arrow にありません。`);
    if (s.here && !boxes.includes(s.here)) errors.push(`論点${n}の here の ${s.here} が svg の data-box にありません。`);
    if (s.here && !focus.includes(s.here)) errors.push(`論点${n}の here の ${s.here} が focus に入っていません。`);
    if (s.mark && !s.here) errors.push(`論点${n}に mark がありますが、札を付ける四角（here）がありません。`);
  });
  return errors;
}

const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function build(spec) {
  const steps = spec.steps.map((s) => ({ focus: s.focus, here: s.here ?? null, mark: s.mark ?? null }));
  const list = spec.steps
    .map((s, k) => `<li data-step="${k}"><button type="button">${k + 1} ${esc(s.name)}</button><span class="pt">${esc(s.point)}</span></li>`)
    .join("");
  const source = spec.source
    ? `<div class="fs-src"><b>${esc(spec.source.label)}</b>${esc(spec.source.text)}</div>`
    : "";
  const lead = spec.lead ? `<div class="fs-lead">${esc(spec.lead)}</div>` : "";
  return `<style>
.fs{font-family:"Noto Sans JP","Hiragino Sans","Yu Gothic",Meiryo,sans-serif;color:var(--text-primary)}
.fs h3{margin:0 0 2px;font-size:16px}
.fs-lead{font-size:12px;color:var(--text-secondary);margin-bottom:6px}
.fs-src{font-size:13px;margin-bottom:8px}.fs-src b{font-size:11px;font-weight:600;border:1px solid var(--border);border-radius:8px;padding:1px 6px;margin-right:6px;color:var(--text-secondary)}
.fs ol{list-style:none;margin:0 0 4px;padding:0;display:flex;flex-wrap:wrap;gap:6px}
.fs button{font:inherit;font-size:12px;border:1px solid var(--border);background:transparent;color:var(--text-primary);border-radius:12px;padding:3px 10px;cursor:pointer}
.fs li.on button{background:var(--surface-1);font-weight:600;border-color:var(--text-primary)}
.fs .pt{display:block;font-size:12px;color:var(--text-secondary);margin:2px 0 0 4px}
.fs.js .pt{display:none}
.fs svg{display:block;max-width:100%;height:auto;margin:6px auto}
.fs svg [data-box] text:not(.fs-badge){font-size:15px;font-weight:700}
.fs svg .fs-pill{fill:var(--surface-1);stroke:var(--border)}
.fs svg [data-arrow] path{stroke:var(--fs-line)}
.fs svg [data-arrow] text{fill:var(--fs-line)}
.fs svg .fs-hot path{stroke-width:2px}
.fs svg .fs-hot text{font-weight:700}
.fs svg .fs-hot .fs-pill{stroke:var(--fs-line);stroke-width:1.2px}
.fs.fs-step svg [data-box].fs-on :is(rect,polygon,circle):not(.fs-badge-bg){stroke-width:2px}
.fs.fs-step svg [data-box].fs-here :is(rect,polygon,circle):not(.fs-badge-bg){stroke-width:3.5px}
.fs svg .fs-badge-bg{stroke:none}
.fs svg .fs-badge{font-size:11px;font-weight:700}
.fs-point{text-align:center;font-weight:700;font-size:16px;min-height:22px;margin-top:4px}
.fs-nav{display:none;justify-content:center;align-items:center;gap:6px;margin-top:6px;font-size:12px;color:var(--text-secondary)}
.fs.js .fs-nav{display:flex}
</style>
<div class="fs">
<h3>${esc(spec.title)}</h3>${lead}${source}
<ol>${list}</ol>
${spec.svg}
<div class="fs-point"></div>
<div class="fs-nav"><button type="button" data-go="-1">戻る</button><span class="fs-pos"></span><button type="button" data-go="1">次へ</button><button type="button" data-all>図ぜんぶを見る</button></div>
</div>
<script>
(()=>{const roots=document.querySelectorAll(".fs:not(.js)");const root=roots[roots.length-1];if(!root)return;root.classList.add("js");
const steps=${JSON.stringify(steps)};const svg=root.querySelector("svg");const items=[...root.querySelectorAll("li[data-step]")];
const NS="http://www.w3.org/2000/svg";let i=0,all=false;
const line={};for(const c of ["c-blue","c-coral","c-gray"]){const g=document.createElementNS(NS,"g");g.setAttribute("class",c);const r=document.createElementNS(NS,"rect");g.appendChild(r);svg.appendChild(g);line[c]=getComputedStyle(r).stroke;g.remove();}
svg.querySelectorAll("[data-box]").forEach(g=>{g.dataset.color=[...g.classList].find(x=>x.startsWith("c-"))??"";});
svg.querySelectorAll("[data-arrow] text").forEach(t=>{const b=t.getBBox();const r=document.createElementNS(NS,"rect");r.setAttribute("class","fs-pill");r.setAttribute("x",b.x-5);r.setAttribute("y",b.y-2);r.setAttribute("width",b.width+10);r.setAttribute("height",b.height+4);r.setAttribute("rx",4);t.before(r);});
const recolor=(g,c)=>{[...g.classList].filter(x=>x.startsWith("c-")).forEach(x=>g.classList.remove(x));if(c)g.classList.add(c);};
function draw(){root.classList.toggle("fs-step",!all);items.forEach((li,k)=>li.classList.toggle("on",!all&&k===i));
svg.querySelectorAll(".fs-badge,.fs-badge-bg").forEach(e=>e.remove());
svg.querySelectorAll("[data-box],[data-arrow]").forEach(g=>{g.classList.remove("fs-on","fs-hot","fs-here");const isBox=g.dataset.box!==undefined;
if(all){if(isBox)recolor(g,g.dataset.color);else g.style.setProperty("--fs-line",line["c-blue"]);return;}
const id=isBox?g.dataset.box:g.dataset.arrow;const on=steps[i].focus.includes(id);
if(isBox){recolor(g,on?"c-coral":"c-gray");if(on)g.classList.add("fs-on");}
else{g.style.setProperty("--fs-line",line[on?"c-coral":"c-gray"]);if(on)g.classList.add("fs-hot");}
if(id===steps[i].here){g.classList.add("fs-here");if(steps[i].mark){const b=g.getBBox();const t=document.createElementNS(NS,"text");t.setAttribute("class","fs-badge");t.setAttribute("x",b.x+6);t.setAttribute("y",b.y+b.height+15);t.textContent=steps[i].mark;g.appendChild(t);const w=t.getBBox().width;const bg=document.createElementNS(NS,"rect");bg.setAttribute("class","fs-badge-bg");bg.setAttribute("x",b.x);bg.setAttribute("y",b.y+b.height+3);bg.setAttribute("width",w+12);bg.setAttribute("height",16);bg.setAttribute("rx",3);t.before(bg);}}});
root.querySelector(".fs-point").textContent=all?"":items[i].querySelector(".pt").textContent;
root.querySelector(".fs-pos").textContent=all?"全体":(i+1)+" / "+steps.length;}
items.forEach((li,k)=>li.querySelector("button").onclick=()=>{i=k;all=false;draw();});
root.querySelectorAll("[data-go]").forEach(b=>b.onclick=()=>{all=false;i=Math.min(steps.length-1,Math.max(0,i+(+b.dataset.go)));draw();});
root.querySelector("[data-all]").onclick=()=>{all=true;draw();};draw();})();
</script>`;
}

// 道具の外で表示を確かめるためのページ。道具が決める色の変数を、明るい画面と暗い画面の2通りで仮に置く。
const THEMES = {
  light: "--text-primary:#1d2430;--text-secondary:#5d6778;--text-muted:#9aa4b5;--text-accent:#2563eb;--surface-1:#f8f9fb;--border:#d5dae2;--blue-fill:#dbe8fd;--blue-line:#2563eb;--blue-text:#1d3f8f;--coral-fill:#fde5d6;--coral-line:#c2410c;--coral-text:#9a3412;--gray-fill:#f1f3f6;--gray-line:#9aa4b5;--green-fill:#dcf3e3;--green-line:#15803d;--green-text:#14532d;background:#ffffff",
  dark: "--text-primary:#e8ebf0;--text-secondary:#a3acba;--text-muted:#5d6778;--text-accent:#8db4f7;--surface-1:#252a33;--border:#3d4450;--blue-fill:#1c2e4d;--blue-line:#5b8ef0;--blue-text:#c6d8fb;--coral-fill:#4a2616;--coral-line:#f8b48a;--coral-text:#fbd0b4;--gray-fill:#2b3039;--gray-line:#5d6778;--green-fill:#173a24;--green-line:#4ade80;--green-text:#bbf7d0;background:#1a1d23",
};
export function preview(fragment, theme) {
  return `<!doctype html><meta charset="utf-8"><style>body{${THEMES[theme]};margin:0;padding:16px;width:600px}
.t{font-size:14px;fill:var(--text-primary)}.ts{font-size:12px;fill:var(--text-secondary)}.th{font-size:14px;font-weight:500;fill:var(--text-primary)}
.arr{stroke:var(--text-secondary);fill:none}.c-blue>:is(rect,polygon){fill:var(--blue-fill);stroke:var(--blue-line)}.c-blue>text{fill:var(--blue-text)}.c-coral>:is(rect,polygon){fill:var(--coral-fill);stroke:var(--coral-line)}.c-coral>text{fill:var(--coral-text)}.c-gray>:is(rect,polygon){fill:var(--gray-fill);stroke:var(--gray-line)}.c-gray>text{fill:var(--text-muted)}.c-green>:is(rect,polygon){fill:var(--green-fill);stroke:var(--green-line)}.c-green>text{fill:var(--green-text)}</style>${fragment}`;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [file, ...rest] = process.argv.slice(2);
  if (!file) {
    console.error("使い方: node stepper.mjs SPEC.json [--out FILE] [--preview DIR]");
    process.exit(2);
  }
  const opt = (name) => { const k = rest.indexOf(name); return k < 0 ? null : rest[k + 1]; };
  const spec = JSON.parse(fs.readFileSync(file, "utf8"));
  if (spec.svgFile) spec.svg = fs.readFileSync(path.resolve(path.dirname(file), spec.svgFile), "utf8");
  const errors = check(spec);
  if (errors.length) {
    console.error(errors.join("\n"));
    process.exit(1);
  }
  const html = build(spec);
  if (opt("--out")) fs.writeFileSync(opt("--out"), html); else process.stdout.write(html);
  const dir = opt("--preview");
  if (dir) {
    fs.mkdirSync(dir, { recursive: true });
    for (const theme of Object.keys(THEMES)) fs.writeFileSync(path.join(dir, `${theme}.html`), preview(html, theme));
  }
  console.error(`${Buffer.byteLength(html)} バイト。道具の text 引数の例：${spec.title}（論点${spec.steps.length}つ：${spec.steps.map((s) => s.name).join("、")}）`);
}
