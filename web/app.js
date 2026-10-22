/* ForestMerge — клиент. Без сборки: MapLibre GL из web/vendor (локально, без CDN), данные из /api. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: d }));
const fmtA = (a) => (a == null ? "—" : a < 0.1 ? fmt(a, 4) : fmt(a, 2));

function toLogin() {
  location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
}

async function api(url, opts = {}) {
  const r = await fetch(url, { credentials: "same-origin", ...opts });
  if (r.status === 401) { toLogin(); throw new Error("нужно войти"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* не JSON */ }
    throw new Error(msg);
  }
  return r.json();
}
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

let CFG = null;     // /api/config
let PID = null;     // текущий проект
let DATA = null;    // результат расчёта
let VIEW = "fragments";
let SEL = null;     // {kind: 'frag'|'after'|'before', id}
let markers = [];

const CLASS = {
  small:   { c: "#f59e0b", t: "мельче минимума → объединение" },
  tech:    { c: "#dc2626", t: "технологический осколок" },
  ok:      { c: "#16a34a", t: "отделённая часть ≥ минимума → новый выдел" },
  main:    { c: "#7dd3fc", t: "основная часть исходного выдела" },
  cut:     { c: "#8d8d8d", t: "вырубка (внутри лесосеки)" },
  thinned: { c: "#c6a700", t: "пройдено выборочной рубкой" },
  kept:    { c: "#a5b4fc", t: "контур выдела, не задетый рубкой" },
};
const FLAG_RU = { needs_review: ["review", "проверить"], fallback: ["fallback", "ослабленное правило"], user_override: ["user", "вручную"],
  kept_small_p197: ["small", "оставлен (п.197)"], isolated: ["small", "нет соседей"] };
const TERM_RU = { composition: "состав", dominant_species: "порода", age: "возраст", age_group: "гр. возраста", density: "полнота",
  bonitet: "бонитет", diameter: "диаметр", tlu: "ТЛУ", forest_type: "тип леса", origin: "происхожд.", stock: "запас/га", land: "кат. земель" };

// ---------------------------------------------------------------- карта
// Карта не обязательна. Если библиотека не загрузилась или в браузере нет WebGL2 (удалённый рабочий стол,
// виртуалка, отключённое аппаратное ускорение) — параметры, расчёт, журнал, ТО и экспорт работают без неё.
const EMPTY = { type: "FeatureCollection", features: [] };
let map = null;
let mapError = null;
try {
  if (new URLSearchParams(location.search).has("nomap")) throw new Error("карта отключена параметром ?nomap");
  if (typeof maplibregl === "undefined") throw new Error("не загрузилась библиотека карты (maplibre-gl.js)");
  map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap" },
        sat: { type: "raster", tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], tileSize: 256, maxzoom: 18, attribution: "Esri World Imagery" },
      },
      layers: [
        { id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.4, "raster-opacity": 0.85 } },
        { id: "sat", type: "raster", source: "sat", layout: { visibility: "none" } },
      ],
    },
    center: [41.965, 61.662], zoom: 12.5,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.on("error", (e) => console.warn("карта:", e && e.error ? e.error.message : e));   // недоступные тайлы подложки — не фатально
} catch (e) {
  map = null;
  mapError = e && e.message ? e.message : String(e);
  console.error("карта недоступна:", e);
  $("#layerCtl").hidden = true;
  $("#map").innerHTML = `<div class="map-off"><div><b>Карта недоступна в этом браузере.</b><br>${esc(mapError)}<br><br>
    Расчёт, карточки решений (вкладка «Журнал»), таблица ТО и экспорт работают и без карты.
    Для карты нужен браузер с WebGL2: свежий Chrome / Yandex / Firefox / Edge с включённым аппаратным ускорением.</div></div>`;
}

const mapReady = map ? new Promise((res) => map.on("load", res)) : Promise.resolve();
mapReady.then(() => {
  if (!map) return;
  for (const s of ["before", "lesoseki", "fragments", "after", "sel"]) map.addSource(s, { type: "geojson", data: EMPTY });
  map.addLayer({ id: "before-fill", type: "fill", source: "before", paint: { "fill-color": "#000", "fill-opacity": 0.01 } });
  map.addLayer({ id: "before-line", type: "line", source: "before", paint: { "line-color": "#334033", "line-width": 1.1, "line-opacity": 0.8 } });
  map.addLayer({ id: "frag-fill", type: "fill", source: "fragments", paint: {
    "fill-color": ["match", ["get", "class"], ...Object.entries(CLASS).flatMap(([k, v]) => [k, v.c]), "#999"],
    "fill-opacity": ["match", ["get", "class"], "cut", 0.35, "kept", 0.3, "main", 0.3, 0.65] } });
  map.addLayer({ id: "frag-line", type: "line", source: "fragments", paint: { "line-color": "#3b2f12", "line-width": 0.6, "line-opacity": 0.6 } });
  map.addLayer({ id: "ls-fill", type: "fill", source: "lesoseki", paint: { "fill-color": "#e4572e", "fill-opacity": 0.08 } });
  map.addLayer({ id: "ls-line", type: "line", source: "lesoseki", paint: { "line-color": "#c2410c", "line-width": 2, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "after-fill", type: "fill", source: "after", paint: {
    "fill-color": ["case", ["!=", ["get", "flags"], ""], "#f97316", ["==", ["get", "kind"], "cut"], "#9ca3af", ["get", "changed"], "#2563eb", "#000"],
    "fill-opacity": ["case", ["!=", ["get", "flags"], ""], 0.3, ["==", ["get", "kind"], "cut"], 0.3, ["get", "changed"], 0.16, 0.01] } });
  map.addLayer({ id: "after-line", type: "line", source: "after", paint: { "line-color": "#1e3a8a", "line-width": 1.6 } });
  map.addLayer({ id: "sel-line", type: "line", source: "sel", paint: { "line-color": "#d946ef", "line-width": 3.5 } });
  setView(VIEW);
  map.on("click", onMapClick);
  for (const l of ["frag-fill", "after-fill", "before-fill"]) {
    map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
  }
});

function vis(id, on) { if (map && map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none"); }
function fitTo(fc, opts) { if (map) map.fitBounds(bboxOf(fc), opts); }

function setView(v) {
  VIEW = v;
  $$("#layerCtl .seg button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  vis("before-fill", v === "before"); vis("before-line", v !== "after");
  vis("frag-fill", v === "fragments"); vis("frag-line", v === "fragments");
  vis("after-fill", v === "after"); vis("after-line", v === "after");
  const ls = $("#tglLs").checked;
  vis("ls-fill", ls && v !== "after"); vis("ls-line", ls);
  drawLabels();
  drawLegend();
}

function drawLegend() {
  const el = $("#legend");
  if (VIEW === "fragments") {
    el.innerHTML = Object.values(CLASS).map((x) => `<div><i style="background:${x.c}"></i>${x.t}</div>`).join("");
  } else if (VIEW === "after") {
    el.innerHTML = `<div><i style="background:#2563eb55"></i>изменённый выдел</div><div><i style="background:#9ca3af88"></i>вырубка</div>
      <div><i style="background:#f9731688"></i>требует проверки</div><div><i style="background:#fff;border-color:#1e3a8a"></i>без изменений</div>`;
  } else {
    el.innerHTML = `<div><i style="background:#fff;border-color:#334033"></i>исходная выдельная сеть</div><div><i style="background:#e4572e33;border-color:#c2410c"></i>лесосеки</div>`;
  }
}

function drawLabels() {
  markers.forEach((m) => m.remove());
  markers = [];
  if (!map || !DATA || !$("#tglLabels").checked) return;
  const fc = VIEW === "after" ? DATA.layers.after : DATA.layers.before;
  for (const f of fc.features) {
    const p = f.properties;
    if (!p.lp) continue;
    const el = document.createElement("div");
    el.className = "vlabel" + (VIEW === "after" && p.kind === "cut" ? " cut" : VIEW === "after" && p.changed ? " new" : "");
    el.textContent = p.label_name;
    markers.push(new maplibregl.Marker({ element: el }).setLngLat(p.lp).addTo(map));
  }
}

function bboxOf(fc) {
  let b = [180, 90, -180, -90];
  const walk = (c) => {
    if (typeof c[0] === "number") { b = [Math.min(b[0], c[0]), Math.min(b[1], c[1]), Math.max(b[2], c[0]), Math.max(b[3], c[1])]; return; }
    c.forEach(walk);
  };
  fc.features.forEach((f) => f.geometry && walk(f.geometry.coordinates));
  return b;
}

function highlight(feature) {
  const src = map && map.getSource("sel");
  if (src) src.setData(feature ? { type: "FeatureCollection", features: [feature] } : EMPTY);
}

function onMapClick(e) {
  const layer = VIEW === "fragments" ? "frag-fill" : VIEW === "after" ? "after-fill" : "before-fill";
  const f = map.queryRenderedFeatures(e.point, { layers: [layer] })[0];
  if (!f) return;
  if (VIEW === "fragments") selectFragment(f.properties.uid);
  else if (VIEW === "after") selectAfter(f.properties.label_name);
  else selectBefore(f.properties.label_name);
}

// ---------------------------------------------------------------- данные
function decisionOf(uid) { return DATA.decisions.find((d) => d.fragment === uid); }
function fragFeature(uid) { return DATA.layers.fragments.features.find((f) => f.properties.uid === uid); }
function afterFeature(vid) { return DATA.layers.after.features.find((f) => f.properties.label_name === vid); }

function setBusy(b) { $("#busy").hidden = !b; $("#btnRun").disabled = b; }

async function loadProjects(selectId) {
  const list = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = list.length ? list.map((p) => `<option value="${p.id}">${esc(p.name)} · ${p.id}</option>`).join("") : `<option value="">нет проектов</option>`;
  if (selectId) sel.value = selectId;
  return list;
}

async function openProject(pid) {
  PID = pid;
  setBusy(true);
  try { render(await api(`/api/projects/${pid}/result`)); }
  catch (e) { alert("Ошибка: " + e.message); }
  finally { setBusy(false); }
}

async function runProject(extra = {}) {
  if (!PID) return;
  setBusy(true);
  try {
    const body = { params: collectParams(), use_models: $("[name=use_models]").checked, ...extra };
    render(await post(`/api/projects/${PID}/run`, body));
  } catch (e) { alert("Ошибка расчёта: " + e.message); }
  finally { setBusy(false); }
}

async function sendDecision(fragment, action, target) {
  setBusy(true);
  try {
    render(await post(`/api/projects/${PID}/decisions`, { fragment, action, target }), true);
    selectFragment(fragment);
  } catch (e) { alert("Ошибка: " + e.message); }
  finally { setBusy(false); }
}

function render(data, keepView) {
  const first = !DATA || DATA.project?.id !== data.project?.id;
  DATA = data;
  mapReady.then(() => {
    if (!map) return;
    map.getSource("before").setData(data.layers.before);
    map.getSource("lesoseki").setData(data.layers.lesoseki);
    map.getSource("fragments").setData(data.layers.fragments);
    map.getSource("after").setData(data.layers.after);
    highlight(null);
    if (first) fitTo(data.layers.before, { padding: 30, duration: 0 });
    setView(VIEW);
  });
  fillParams(data);
  renderMetrics(data);
  renderJournal();
  renderTable();
  if (!keepView) {
    $("#tab-card").innerHTML = map
      ? `<p class="muted pad">Кликните фрагмент или выдел на карте. Решений: ${data.decisions.length}.</p>`
      : `<p class="muted pad">Карта недоступна — решения смотрите во вкладке «Журнал», выделы — во вкладке «ТО». Решений: ${data.decisions.length}.</p>`;
    if (!map) showTab("journal");
  }
}

// ---------------------------------------------------------------- итоги
function renderMetrics(d) {
  const m = d.metrics;
  const box = (v, label, cls = "") => `<div class="metric ${cls}"><b>${v}</b><span>${label}</span></div>`;
  $("#metrics").innerHTML = [
    box(`${m.n_vydels_before} → ${m.n_vydels_after}`, "выделов до → после"),
    box(m.rank, "разряд таксации"),
    box(`${m.n_fragments_small_before} → ${m.n_small_after}`, "фрагментов < мин. площади", m.n_small_after ? "bad" : "good"),
    box(m.n_tech_slivers, "техосколков (правка контура)"),
    box(m.n_merges, "объединений"),
    box(m.n_needs_review + m.n_kept_small, "требуют проверки", m.n_needs_review + m.n_kept_small ? "warn" : "good"),
    box(fmt(m.area_diff_ha, 5), "баланс площади, га", Math.abs(m.area_diff_ha) < 0.01 ? "good" : "bad"),
    box(fmt(m.stock_balance_m3, 0), "баланс запаса, м³", Math.abs(m.stock_balance_m3) < 0.005 * m.stock_before_m3 ? "good" : "warn"),
    box(fmt(m.stock_removed_inventory_m3, 0), "снято по ТО, м³"),
    box(m.harvested_volume_m3 == null ? "—" : fmt(m.harvested_volume_m3, 0), "заготовлено (5_3), м³"),
  ].join("") + `<p class="muted small" style="grid-column:1/-1;margin:2px 0 0">Время расчёта ${fmt(d.elapsed_s)} с ·
    ${d.models ? "модели: " + Object.entries(d.models).filter(([k, v]) => v === true || (Array.isArray(v) && v.length)).map(([k]) => k).join(", ") || "нет" : "только правила"}</p>`;
  $("#warnings").innerHTML = d.warnings.length ? `<ul class="warn-list">${d.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
}

// ---------------------------------------------------------------- карточки
function badges(flags) {
  return (flags || []).map((f) => { const [c, t] = FLAG_RU[f] || ["", f]; return `<span class="badge ${c}">${t}</span>`; }).join("");
}

function selectFragment(uid) {
  showTab("card");
  const f = fragFeature(uid);
  highlight(f);
  const d = decisionOf(uid);
  const p = f?.properties || {};
  const el = $("#tab-card");
  if (!d) {
    el.innerHTML = `<div class="dc"><h2>Фрагмент ${esc(uid)}</h2>
      <div class="badges"><span class="badge">${esc(CLASS[p.class]?.t || p.class)}</span></div>
      <div class="kv"><div>Исходный выдел</div><div>${esc(p.parent)}</div><div>Лесосека</div><div>${esc(p.ls_id || "—")}</div>
      <div>Площадь</div><div>${fmtA(p.area_ha)} га</div><div>Мин. площадь</div><div>${p.min_area_ha ?? "—"} га</div>
      <div>Характеристика</div><div>${esc(p.label)}</div><div>Итоговый выдел</div><div>${p.out_vid ? `<a href="#" data-vid="${esc(p.out_vid)}">№ ${esc(p.out_vid)}</a>` : "—"}</div></div>
      <p class="muted">Решение не требовалось: ${p.class === "ok" ? "фрагмент не меньше минимальной площади и стал отдельным выделом" : p.class === "cut" ? "часть вырубки" : p.class === "main" ? "основная часть исходного выдела, сохраняет его номер" : "часть исходного выдела"}.</p></div>`;
    bindVidLinks(el);
    return;
  }
  renderDecision(d, el);
}

function renderDecision(d, el) {
  const isTech = d.type === "tech";
  const ch = d.chosen;
  let html = `<div class="dc"><h2>${isTech ? "Техосколок" : "Фрагмент"} ${esc(d.fragment)} <span class="muted small">${d.id}</span></h2>
    <div class="badges"><span class="badge ${isTech ? "tech" : "small"}">${isTech ? "технологический осколок" : d.type === "keep" ? "оставлен" : "объединение"}</span>${badges(d.flags)}</div>
    <div class="kv">
      <div>Исходный выдел</div><div>${esc((d.fragment_parents || []).join(", "))}</div>
      <div>Площадь / минимум</div><div>${fmtA(d.fragment_area_ha)} га${d.min_area_ha != null ? ` / ${d.min_area_ha} га (${d.rank}-й разряд)` : ""}</div>
      ${d.fragment_label ? `<div>Характеристика</div><div>${esc(d.fragment_label)}</div>` : ""}
      <div>Итог</div><div>${d.target_out_vid ? `присоединён к <a href="#" data-vid="${esc(d.target_out_vid)}">№ ${esc(d.target_out_vid)}</a>` : d.fragment_out_vid ? `выдел <a href="#" data-vid="${esc(d.fragment_out_vid)}">№ ${esc(d.fragment_out_vid)}</a>` : "—"}</div>
    </div>
    <h4>Обоснование</h4><div class="expl">${esc(d.explanation)}</div>
    ${d.explanation_llm ? `<h4>Текст для пояснительной записки (LLM)</h4><div class="expl llm">${esc(d.explanation_llm)}</div>` : ""}`;
  if (!isTech && ch && ch.checks && ch.checks.length) {
    html += `<h4>Допуски при объединении (п.218 → п.215)</h4><ul class="checks">${ch.checks.map((c) =>
      `<li><span class="${c.ok ? "y" : "n"}">${c.ok ? "✓" : "✗"}</span>${esc(c.text)}<span class="p">${esc(c.para)}</span></li>`).join("")}</ul>`;
  }
  if (!isTech && d.candidates && d.candidates.length) {
    const hasMl = d.candidates.some((c) => c.score_ml != null);
    html += `<h4>Смежные выделы-кандидаты</h4><table><thead><tr><th></th><th>Сосед</th><th class="num">Граница</th><th class="num">Дист.</th><th class="num">Наруш.</th><th class="num">Балл</th>${hasMl ? '<th class="num">ML</th>' : ""}</tr></thead><tbody>`;
    for (const c of [...d.candidates].sort((a, b) => (a.compatible === b.compatible ? (a.cost ?? 99) - (b.cost ?? 99) : a.compatible ? -1 : 1))) {
      const chosen = d.target === c.uid;
      const vid = DATA.out_vid[c.uid] || c.vid || c.uid;
      const sep = (c.separation || []).map((k) => TERM_RU[k] || k).join(", ");
      html += `<tr class="${chosen ? "chosen" : ""} ${c.compatible ? "" : "incompat"}" title="${esc(c.compatible ? (sep ? "различия выше порогов: " + sep : "различий выше порогов нет") : c.compat.text)}">
        <td><input type="radio" name="cand" value="${esc(c.uid)}" ${chosen ? "checked" : ""}></td>
        <td>№ ${esc(vid)}<div class="muted small">${esc(c.label)}${c.fallback ? " · ослабл." : ""}${c.compatible ? "" : " · " + esc(c.compat.text)}</div></td>
        <td class="num">${fmt((c.shared_frac || 0) * 100, 0)}%</td>
        <td class="num">${c.distance != null ? fmt(c.distance) : "—"}</td>
        <td class="num">${c.n_viol != null ? c.n_viol : "—"}</td>
        <td class="num">${c.cost != null ? fmt(c.cost) : "—"}</td>
        ${hasMl ? `<td class="num">${c.score_ml != null ? fmt(c.score_ml) : "—"}</td>` : ""}</tr>`;
    }
    html += `</tbody></table>
      <div class="actions">
        <button class="primary" data-act="accept">Принять решение</button>
        <button data-act="merge">Присоединить к выбранному</button>
        <button data-act="keep">Оставить отдельным (п.197)</button>
        ${(d.flags || []).includes("user_override") ? '<button class="danger" data-act="reset">Отменить ручное</button>' : ""}
      </div><p class="muted small">Любое действие сохраняется как метка для дообучения ранжировщика (M3).</p>`;
  }
  html += "</div>";
  el.innerHTML = html;
  bindVidLinks(el);
  $$("[data-act]", el).forEach((b) => b.addEventListener("click", () => {
    const act = b.dataset.act;
    const tgt = $("input[name=cand]:checked", el)?.value;
    if (act === "merge" && !tgt) return alert("Выберите соседа в таблице");
    sendDecision(d.fragment, act, act === "merge" ? tgt : undefined);
  }));
}

function bindVidLinks(el) {
  $$("a[data-vid]", el).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); setView("after"); selectAfter(a.dataset.vid); }));
}

const KEY_FIELDS = [
  ["Площадь выдела", "Площадь, га"], ["Общий запас лесных насаждений", "Запас, м³"], ["Общий запас единичных деревьев", "Запас ед. деревьев, м³"],
  ["Наименование вида лесных земель", "Вид земель"], ["Преобладающая порода", "Преобл. порода"], ["Класс бонитета", "Бонитет"],
  ["Наименование типа лесорастительных условий", "ТЛУ"], ["Наименование типа леса", "Тип леса"], ["Год посадки (начала формирования)", "Год формирования"],
];

function toRow(rows, vid) { return rows.find((r) => String(r["Лесоустроительный номер выдела"]) === String(vid)); }

function layerSummary(rows, vid) {
  const lay = rows.filter((r) => String(r["Лесоустроительный номер выдела"]) === String(vid));
  const byL = {};
  for (const r of lay) byL[r["Номер лесного яруса"]] = r;
  return Object.values(byL).map((r) => `ярус ${r["Номер лесного яруса"]}: <b>${esc(r["Состав лесного насаждения"])}</b>, H ${fmt(r["Средняя высота яруса лесного насаждения в метрах"], 1)} м, полнота ${fmt(r["Полнота яруса"])}, ${fmt(r["Средний запас древесины лесного насаждения на 1 гектар"], 0)} м³/га`).join("<br>") || "—";
}

function selectAfter(vid) {
  showTab("card");
  const f = afterFeature(vid);
  highlight(f);
  const p = f?.properties || {};
  const after = toRow(DATA.tables.after, vid) || {};
  const parents = (p.parents || "").split(",").filter(Boolean);
  const merged = DATA.decisions.filter((d) => d.type === "merge" && d.target_out_vid === vid);
  let html = `<div class="dc"><h2>Выдел № ${esc(vid)}</h2><div class="badges">
    <span class="badge ${p.changed ? "ok" : ""}">${p.kind === "cut" ? "вырубка" : p.changed ? "изменён" : "без изменений"}</span>${badges((p.flags || "").split(",").filter(Boolean))}</div>
    <div class="kv"><div>Из исходных</div><div>${parents.map((x) => esc(x)).join(", ") || "—"}</div><div>Лесосеки</div><div>${esc(p.ls_ids || "—")}</div></div>
    <h4>Таксационная характеристика: после ${parents.length === 1 ? "/ до" : ""}</h4><table><thead><tr><th>Показатель</th><th>После</th>${parents.length === 1 ? "<th>До</th>" : ""}</tr></thead><tbody>`;
  const before = parents.length === 1 ? toRow(DATA.tables.before, parents[0]) || {} : null;
  for (const [k, lbl] of KEY_FIELDS) {
    const a = after[k], b = before ? before[k] : undefined;
    const changed = before && String(a ?? "") !== String(b ?? "");
    html += `<tr><td>${lbl}</td><td class="${changed ? "changed" : ""}">${esc(typeof a === "number" ? fmt(a, 4) : a ?? "—")}</td>${before ? `<td>${esc(typeof b === "number" ? fmt(b, 4) : b ?? "—")}</td>` : ""}</tr>`;
  }
  html += `</tbody></table><h4>Ярусы (после)</h4><div class="small">${layerSummary(DATA.tables.layers_after, vid)}</div>`;
  if (merged.length) {
    html += `<h4>Присоединённые фрагменты (${merged.length})</h4>` + merged.map((d) =>
      `<div class="jitem" data-frag="${esc(d.fragment)}"><div class="t">${esc(d.fragment)} · ${fmtA(d.fragment_area_ha)} га ${badges(d.flags)}</div><div class="s">${esc(d.fragment_label || "")}</div></div>`).join("");
  }
  if (parents.length > 1) {
    html += `<h4>Исходные выделы</h4>` + parents.map((pv) => { const r = toRow(DATA.tables.before, pv) || {};
      return `<div class="small">№ ${esc(pv)}: ${esc(r["Наименование вида лесных земель"] || "нет ТО")}, ${esc(r["Преобладающая порода"] || "")}, ${fmt(r["Площадь выдела"], 2)} га, ${fmt(r["Общий запас лесных насаждений"], 0)} м³ — ${layerSummary(DATA.tables.layers_before, pv)}</div>`; }).join("");
  }
  html += "</div>";
  const el = $("#tab-card");
  el.innerHTML = html;
  $$("[data-frag]", el).forEach((x) => x.addEventListener("click", () => { setView("fragments"); selectFragment(x.dataset.frag); }));
}

function selectBefore(vid) {
  showTab("card");
  const f = DATA.layers.before.features.find((x) => x.properties.label_name === vid);
  highlight(f);
  const r = toRow(DATA.tables.before, vid) || {};
  $("#tab-card").innerHTML = `<div class="dc"><h2>Исходный выдел № ${esc(vid)}</h2><div class="kv">${KEY_FIELDS.map(([k, l]) =>
    `<div>${l}</div><div>${esc(typeof r[k] === "number" ? fmt(r[k], 4) : r[k] ?? "—")}</div>`).join("")}</div>
    <h4>Ярусы</h4><div class="small">${layerSummary(DATA.tables.layers_before, vid)}</div></div>`;
}

// ---------------------------------------------------------------- журнал и таблица
function renderJournal() {
  const flt = $("#jFilter").value;
  const ok = (d) => flt === "all" || (flt === "merge" && d.type === "merge") || (flt === "keep" && d.type === "keep") || (flt === "tech" && d.type === "tech")
    || (flt === "review" && ((d.flags || []).includes("needs_review") || d.type === "keep")) || (flt === "fallback" && (d.flags || []).includes("fallback"))
    || (flt === "user" && (d.flags || []).includes("user_override"));
  const items = DATA.decisions.filter(ok);
  $("#journal").innerHTML = items.length ? items.map((d) => `<div class="jitem" data-frag="${esc(d.fragment)}">
      <div class="t">${d.id} · ${d.type === "tech" ? "техосколок" : d.type === "keep" ? "оставлен" : "объединение"} · ${esc(d.fragment)} (${fmtA(d.fragment_area_ha)} га)${d.target_out_vid ? " → № " + esc(d.target_out_vid) : ""} ${badges(d.flags)}</div>
      <div class="s">${esc((d.explanation || "").slice(0, 170))}…</div></div>`).join("") : `<p class="muted pad">Нет решений по фильтру.</p>`;
  $$("#journal .jitem").forEach((x) => x.addEventListener("click", () => {
    setView("fragments");
    selectFragment(x.dataset.frag);
    const f = fragFeature(x.dataset.frag);
    if (f) fitTo({ features: [f] }, { padding: 120, maxZoom: 16 });
  }));
}

function renderTable() {
  const rows = DATA.tables.after;
  const changed = new Set(DATA.layers.after.features.filter((f) => f.properties.changed).map((f) => f.properties.label_name));
  const cols = KEY_FIELDS.slice(0, 6);
  $("#toTable").innerHTML = `<p class="muted small">Лист «Общие» итогового ТО (полный формат — в XLSX). Подсвечены изменённые выделы.</p>
    <table><thead><tr><th>№</th>${cols.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => {
      const v = String(r["Лесоустроительный номер выдела"]);
      return `<tr data-vid="${esc(v)}" style="cursor:pointer"><td class="${changed.has(v) ? "changed" : ""}">${esc(v)}</td>${cols.map(([k]) =>
        `<td class="${typeof r[k] === "number" ? "num" : ""}">${esc(typeof r[k] === "number" ? fmt(r[k], k === "Площадь выдела" ? 2 : 0) : r[k] ?? "")}</td>`).join("")}</tr>`; }).join("")}</tbody></table>`;
  $$("#toTable tr[data-vid]").forEach((tr) => tr.addEventListener("click", () => {
    setView("after"); selectAfter(tr.dataset.vid);
    const f = afterFeature(tr.dataset.vid);
    if (f) fitTo({ features: [f] }, { padding: 80, maxZoom: 16 });
  }));
}

function showTab(t) {
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach((x) => (x.hidden = x.id !== "tab-" + t));
}

// ---------------------------------------------------------------- параметры
function effectiveNorms(data) {
  const base = JSON.parse(JSON.stringify(CFG.norms));
  const merge = (a, b) => { for (const [k, v] of Object.entries(b || {})) { if (v && typeof v === "object" && !Array.isArray(v) && a[k]) merge(a[k], v); else a[k] = v; } return a; };
  return merge(base, data?.project?.params || {});
}

// Поля минимальных площадей всегда показывают нормы того разряда, под которым будут отправлены
function renderMinAreas(n, rank) {
  const ma = n.min_area_ha[rank] || n.min_area_ha[String(rank)] || {};
  const RU = { natural: "естеств. насаждения", contrast: "спелые среди молодняков", unstocked: "культуры / непокрытые", agri: "с/х угодья", nonforest: "нелесные, линейные" };
  $("#minAreas").innerHTML = Object.entries(ma).map(([k, v]) => `<label>${RU[k] || k}<input type="number" step="0.1" min="0" data-ma="${k}" value="${v}"></label>`).join("");
  $("#minAreas").dataset.rank = rank;
  $("#minAreas").closest("fieldset").querySelector("legend").textContent = `Мин. площадь выдела, га (п.196) — ${rank}-й разряд`;
}

function fillParams(data) {
  const n = effectiveNorms(data);
  const F = $("#params");
  F.rank.value = n.rank ?? "";
  F.method.innerHTML = Object.keys(n.tolerances.table).map((k) => `<option>${esc(k)}</option>`).join("");
  F.method.value = n.tolerances.method;
  renderMinAreas(n, data?.rank || n.rank || 2);
  F.tech_area.value = n.tech_slivers.max_area_ha;
  F.tech_width.value = n.tech_slivers.min_width_m;
  for (const k of ["similarity", "border", "violation", "same_parent_bonus", "largest_neighbor"]) F["w_" + k].value = n.cost_weights[k];
  F.scoring.value = n.scoring;
  F.bonitet.value = n.recalc.bonitet;
  F.numbering.value = n.recalc.numbering;
  F.default_form.value = n.cut.default_form;
  F.clearcut_kind.value = n.cut.clearcut_land_kind;
  F.allow_violation.checked = n.rules.allow_violation_if_no_alternative;
  F.keep_preexisting.checked = n.rules.keep_preexisting_small;
  F.use_models.checked = !!data?.project?.use_models;
}

function collectParams() {
  const F = $("#params");
  const rank = F.rank.value ? Number(F.rank.value) : null;
  const maRank = rank || Number($("#minAreas").dataset.rank);
  const ma = {};
  $$("#minAreas input").forEach((i) => (ma[i.dataset.ma] = Number(i.value)));
  // переопределение отправляем, только если пользователь изменил нормы ЛИ для этого разряда
  const def = CFG.norms.min_area_ha[maRank] || CFG.norms.min_area_ha[String(maRank)] || {};
  const changed = Object.keys(ma).some((k) => Number(def[k]) !== ma[k]);
  const w = {};
  for (const k of ["similarity", "border", "violation", "same_parent_bonus", "largest_neighbor"]) w[k] = Number(F["w_" + k].value);
  return {
    rank,
    ...(changed ? { min_area_ha: { [maRank]: ma } } : {}),
    tolerances: { method: F.method.value },
    tech_slivers: { max_area_ha: Number(F.tech_area.value), min_width_m: Number(F.tech_width.value) },
    cost_weights: w,
    scoring: F.scoring.value,
    recalc: { bonitet: F.bonitet.value, numbering: F.numbering.value },
    cut: { default_form: F.default_form.value, clearcut_land_kind: F.clearcut_kind.value },
    rules: { allow_violation_if_no_alternative: F.allow_violation.checked, keep_preexisting_small: F.keep_preexisting.checked },
  };
}

// ---------------------------------------------------------------- события
$("#params").addEventListener("submit", (e) => { e.preventDefault(); runProject(); });
$("#params").rank.addEventListener("change", (e) => {
  // «авто» — разряд, который решатель выбрал в последнем расчёте (или 2)
  renderMinAreas(effectiveNorms(DATA), Number(e.target.value) || DATA?.rank || 2);
});
$("#btnResetOv").addEventListener("click", () => runProject({ reset_overrides: true }));
$$("#layerCtl .seg button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$("#tglLs").addEventListener("change", () => setView(VIEW));
$("#tglLabels").addEventListener("change", drawLabels);
$("#tglSat").addEventListener("change", (e) => { vis("sat", e.target.checked); vis("osm", !e.target.checked); });
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("#jFilter").addEventListener("change", renderJournal);
$("#projectSelect").addEventListener("change", (e) => e.target.value && openProject(e.target.value));
$$("[data-export]").forEach((b) => b.addEventListener("click", () => PID && (window.location = `/api/projects/${PID}/export?fmt=${b.dataset.export}`)));
$("#btnSample").addEventListener("click", async () => {
  const { id } = await post("/api/projects/sample");
  await loadProjects(id);
  openProject(id);
});
$("#btnUpload").addEventListener("click", () => $("#dlgUpload").showModal());
$("#btnUploadGo").addEventListener("click", async (e) => {
  e.preventDefault();
  const form = $("#uploadForm");
  if (!form.reportValidity()) return;
  const fd = new FormData(form);
  for (const [k, v] of [...fd.entries()]) if (v instanceof File && !v.name) fd.delete(k);
  setBusy(true);
  try {
    const r = await fetch("/api/projects", { method: "POST", body: fd, credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (!r.ok) throw new Error((await r.json()).detail);
    const { id } = await r.json();
    $("#dlgUpload").close();
    await loadProjects(id);
    await openProject(id);
  } catch (err) { alert("Ошибка загрузки: " + err.message); }
  finally { setBusy(false); }
});

$("#btnLogout").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); } finally { location.replace("/login"); }
});

(async function init() {
  const me = await api("/api/me");
  if (me.auth) {
    $("#whoami").textContent = me.user + (me.role === "admin" ? " (админ)" : "");
    $("#btnLogout").hidden = false;
  }
  CFG = await api("/api/config");
  fillParams(null);
  const list = await loadProjects();
  if (list.length) openProject(list[0].id);
})();
