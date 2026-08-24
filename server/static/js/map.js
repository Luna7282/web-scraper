// Page 2: the districts view. An n8n-style pannable/zoomable world canvas
// -- nodes and edges live in "world" coordinates under one .world element,
// panned/zoomed by a single CSS transform, never by touching individual
// node positions. Floating panels (controls, stats, activity, node
// detail) sit over the canvas in screen space, draggable and collapsible,
// with position remembered per browser. Descending into a node promotes
// that SAME element in place (it never moves) and grows its children out
// from it; the level being left dims but stays on screen, capped at one
// level of visible history, so a drill-down reads as continuous movement
// instead of a page replace.

const app = document.getElementById("app");

const LEVEL_GAP = 170;      // world px, root-bottom to children-row-top
const NODE_GAP_X = 56;      // world px, gap between sibling nodes
const MIN_ZOOM = 0.15, MAX_ZOOM = 2.5;
const PAN_THRESHOLD = 5;    // px of pointer movement before a background press counts as a pan, not a tap
const WIDE_REGION_THRESHOLD = 60;

const state = {
  runId: null,
  run: null,
  mode: "path-tree", // 'path-tree' | 'discovery'
  sameSiteOnly: false,
  seedHost: null,
  pathTreeRoot: null,
  crossHostNodes: [],
  allDiscoveryNodes: [],
  breadcrumbs: [],
  selected: null,
  selectedEl: null,
  discoveryCache: new Map(),
  liveStatus: null,
  pollTimer: null,
  activityLog: [],
  seenLogKeys: new Set(),
  camera: { panX: 0, panY: 0, zoom: 1 },
  levels: [],            // stack of at most 2 built levels: [dimmed-parent?, current]
  panelPositions: {},
  panelCollapsed: {},
  els: {},                // long-lived DOM refs: world, canvasViewport, breadcrumbBar, livePill, gatesDock, panels
  lastAscendAt: 0,
};

function qs(name) {
  return new URLSearchParams(location.search).get(name);
}

function h(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children || []) {
    if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  return node;
}

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ------------------------------------------------------------- bootstrap

async function boot() {
  document.body.classList.add("map-page");
  startSpriteAnimationLoop();
  loadPanelPrefs();

  const runId = qs("run");
  if (!runId) {
    app.innerHTML = "";
    app.appendChild(h("div", { class: "empty-state", text: "No run selected. " }, [
      h("a", { href: "/", text: "Pick a run" }),
    ]));
    return;
  }
  state.runId = runId;

  app.innerHTML = "";
  app.appendChild(h("p", { class: "hint", text: "Loading run + map…" }));

  let detail = null;
  for (let attempt = 0; attempt < 10; attempt++) {
    try {
      detail = await Api.runDetail(runId);
      break;
    } catch (err) {
      if (err.status !== 404 || attempt === 9) {
        app.innerHTML = "";
        app.appendChild(errorBox(err));
        return;
      }
      app.innerHTML = "";
      app.appendChild(h("p", { class: "hint", text: "Starting…" }));
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  state.run = detail.run;

  await loadPathTree();

  try {
    state.liveStatus = await Api.liveStatus(state.runId);
    ingestLiveStatusIntoLog(state.liveStatus);
  } catch (e) {
    // frontier.db genuinely not there yet or unreadable
  }

  buildCanvasShell();
  buildPanels();
  await rebuildRootLevel(false);
  renderGatesDock();
  window.addEventListener("resize", onWindowResize);

  if (state.run.job_state === "running") startPolling();
}

function errorBox(err) {
  return h("div", { class: "error-box" }, [
    err.message || String(err),
    err.detail ? h("div", { class: "hint", text: err.detail }) : null,
  ]);
}

function showCanvasError(err) {
  const world = state.els.world;
  world.innerHTML = "";
  world.appendChild(errorBox(err));
}

// ------------------------------------------------------------- data layer
// (unchanged from the previous stage: path_tree/discovery_graph fetch,
// region normalization, gate attribution -- a rendering rework doesn't
// touch any of this.)

async function refreshPathTreeData() {
  const data = await Api.graph(state.runId, { type: "path-tree" });
  state.seedHost = data.seed_host;
  state.crossHostNodes = data.cross_host_nodes || [];
  state.pathTreeRoot = data.tree;
  try {
    const disc = await Api.graph(state.runId, { type: "discovery" });
    state.allDiscoveryNodes = disc.nodes || [];
  } catch (e) {
    state.allDiscoveryNodes = [];
  }
}

function findNodeByPath(root, path) {
  if (root.path === path) return root;
  for (const child of Object.values(root.children)) {
    const found = findNodeByPath(child, path);
    if (found) return found;
  }
  return null;
}

async function loadPathTree() {
  await refreshPathTreeData();
  const effective = findEffectiveRoot(state.pathTreeRoot);
  state.breadcrumbs = [{ label: "(root)", path: effective.path, pathTreeNode: effective }];
}

function findEffectiveRoot(node) {
  let cur = node;
  while (!cur.url_node && Object.keys(cur.children).length === 1) {
    cur = Object.values(cur.children)[0];
  }
  return cur;
}

function regionFromPathTreeNode(segment, node) {
  const u = node.url_node;
  return {
    id: node.path,
    label: segment,
    url: u ? u.url : null,
    status: u ? u.status : null,
    value: u ? u.subtree_size : Object.keys(node.children).length || 1,
    childCount: u ? u.child_count : Object.keys(node.children).length,
    hasChildren: Object.keys(node.children).length > 0,
    path: node.path,
    pathTreeNode: node,
  };
}

function regionFromDiscoveryNode(n) {
  return {
    id: n.url,
    label: shortLabel(n.url),
    url: n.url,
    status: n.status,
    value: Math.max(n.subtree_size, 1),
    childCount: n.child_count,
    hasChildren: n.child_count > 0,
    path: null,
  };
}

function shortLabel(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : u.hostname;
  } catch (e) {
    return url;
  }
}

function regionForCrumb(top) {
  let r;
  if (state.mode === "path-tree") {
    r = regionFromPathTreeNode(top.label, top.pathTreeNode);
    r.cancelledFraction = cancelledFraction(top.pathTreeNode);
  } else {
    const url = top.discoveryRootUrl;
    const found = state.allDiscoveryNodes.find((n) => n.url === url);
    r = found ? regionFromDiscoveryNode(found)
      : { id: url, label: top.label, url, status: null, value: 1, childCount: 0, hasChildren: true, path: null };
  }
  r.isCurrentRoot = true;
  return r;
}

function cancelledFraction(node) {
  let total = 0, cancelled = 0;
  (function walk(n) {
    if (n.url_node) {
      total++;
      if (n.url_node.status === "cancelled") cancelled++;
    }
    for (const c of Object.values(n.children)) walk(c);
  })(node);
  return total === 0 ? 0 : cancelled / total;
}

async function currentLevelRegions() {
  const top = state.breadcrumbs[state.breadcrumbs.length - 1];
  if (state.mode === "path-tree") {
    const node = top.pathTreeNode;
    return Object.entries(node.children).map(([seg, child]) => {
      const r = regionFromPathTreeNode(seg, child);
      r.cancelledFraction = cancelledFraction(child);
      return r;
    });
  }
  const rootUrl = top.discoveryRootUrl;
  if (!rootUrl) return [];
  if (!state.discoveryCache.has(rootUrl)) {
    const data = await Api.graph(state.runId, { type: "discovery", root: rootUrl, maxDepth: 1 });
    state.discoveryCache.set(rootUrl, data.nodes.filter((n) => n.url !== rootUrl));
  }
  return state.discoveryCache.get(rootUrl).map(regionFromDiscoveryNode);
}

function currentUrlPrefix() {
  const top = state.breadcrumbs[state.breadcrumbs.length - 1];
  if (top.path !== undefined && top.path !== null) return `https://${state.seedHost}${top.path}`;
  return top.discoveryRootUrl || null;
}

function gatesForCurrentLevel() {
  const prefix = currentUrlPrefix();
  if (!prefix) return [];
  const byHost = new Map();
  for (const n of state.allDiscoveryNodes) {
    if (!n.cross_host) continue;
    if (!n.parent_url || !n.parent_url.startsWith(prefix)) continue;
    byHost.set(n.host, (byHost.get(n.host) || 0) + 1);
  }
  return Array.from(byHost.entries()).map(([host, count]) => ({ host, count }));
}

function badge(status) {
  const s = status || "none";
  return h("span", { class: `badge badge-${s}`, text: s });
}

function statusColor(status, cancelledFrac) {
  if (cancelledFrac >= 0.999) return "var(--ash)";
  if (status === "cancelled") return "var(--ash)";
  if (status === "failed") return "var(--red)";
  if (status === "done") return "var(--green)";
  if (status === "mapped") return "var(--blue)";
  if (status === "queued" || status === "pending_score" || status === "in_progress") return "var(--amber)";
  return "var(--moss)";
}

// ------------------------------------------------------------ canvas shell

function buildCanvasShell() {
  app.innerHTML = "";
  const viewport = h("div", { class: "canvas-viewport", id: "canvas-viewport" });
  const world = h("div", { class: "world", id: "world" });
  viewport.appendChild(world);
  app.appendChild(viewport);

  const breadcrumbBar = h("div", { class: "breadcrumb-bar" });
  const livePill = h("span", { class: "live-pill static", text: "static view" });
  const toolbar = renderCanvasToolbar();
  const gatesDock = h("div", { class: "gates-dock" });

  app.appendChild(breadcrumbBar);
  app.appendChild(livePill);
  app.appendChild(toolbar);
  app.appendChild(gatesDock);

  state.els = { viewport, world, breadcrumbBar, livePill, gatesDock };
  initCanvasInteraction(viewport);
  renderBreadcrumbBar();
}

function renderCanvasToolbar() {
  const bar = h("div", { class: "canvas-toolbar" });
  bar.appendChild(h("button", { title: "Zoom in", text: "+", onclick: () => { const vr = viewportRect(); zoomAt(vr.width / 2, vr.height / 2, 1.3, true); } }));
  bar.appendChild(h("button", { title: "Zoom out", text: "−", onclick: () => { const vr = viewportRect(); zoomAt(vr.width / 2, vr.height / 2, 1 / 1.3, true); } }));
  bar.appendChild(h("button", { title: "Fit current level to view", text: "⤢", onclick: () => fitCurrentLevel(true) }));
  bar.appendChild(h("button", { title: "Reset zoom to 100%, centered on root", text: "1:1", onclick: resetView }));
  bar.appendChild(h("button", { class: "secondary", title: "Reset panel positions", text: "☷", onclick: resetPanelLayout }));
  return bar;
}

// -------------------------------------------------------- camera (pan/zoom)

function applyCamera() {
  const { world, viewport } = state.els;
  const c = state.camera;
  if (world) world.style.transform = `translate(${c.panX}px, ${c.panY}px) scale(${c.zoom})`;
  if (viewport) viewport.style.backgroundPosition = `${c.panX}px ${c.panY}px`;
}

// .canvas-viewport is .world's positioned ancestor -- camera.panX/panY
// and every screen-space coordinate fed into the camera math are defined
// relative to ITS top-left, not the browser window's. The two coincide
// on X (the canvas spans the full window width) but NOT on Y (the
// canvas sits below the header bar), so anything that used
// window.innerHeight/e.clientY directly was systematically off by the
// header's height on the vertical axis -- confirmed by measuring a
// sprite's actual screen position against its expected world-to-screen
// projection at three different zoom levels: X matched within rounding
// at all three (so the zoom-scaling math itself, and the earlier
// CSS-transform/SVG-attribute sprite fix, both hold under zoom); Y was
// off by the exact same ~58px at every zoom level, which is the
// signature of a fixed coordinate-space offset, not a scaling bug.
function viewportRect() {
  return state.els.viewport.getBoundingClientRect();
}

function screenToWorld(sx, sy) {
  return { x: (sx - state.camera.panX) / state.camera.zoom, y: (sy - state.camera.panY) / state.camera.zoom };
}

// Returns true if the zoom actually changed (false when already clamped
// at MIN_ZOOM/MAX_ZOOM) -- the wheel handler uses this to detect "still
// trying to zoom out past the floor" and treats that as "go up a level."
// sx, sy are viewport-local (see viewportRect()) -- callers convert.
function zoomAt(sx, sy, factor, animate) {
  const newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, state.camera.zoom * factor));
  const changed = Math.abs(newZoom - state.camera.zoom) > 1e-6;
  const w = screenToWorld(sx, sy);
  const panX = sx - w.x * newZoom;
  const panY = sy - w.y * newZoom;
  if (animate) animateCameraTo({ x: panX, y: panY }, newZoom);
  else { state.camera.panX = panX; state.camera.panY = panY; state.camera.zoom = newZoom; applyCamera(); }
  return changed;
}

function resetView() {
  const cur = state.levels[state.levels.length - 1];
  if (!cur) return;
  const vr = viewportRect();
  animateCameraTo({ x: vr.width / 2 - cur.rootItem.x, y: vr.height / 2 - cur.rootItem.y }, 1);
}

let cameraAnimHandle = null;
function animateCameraTo(targetPan, targetZoom, duration = 420) {
  if (cameraAnimHandle) cancelAnimationFrame(cameraAnimHandle);
  if (prefersReducedMotion()) {
    state.camera = { panX: targetPan.x, panY: targetPan.y, zoom: targetZoom };
    applyCamera();
    return;
  }
  const start = { ...state.camera };
  const t0 = performance.now();
  function step(now) {
    const t = Math.min(1, (now - t0) / duration);
    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    state.camera.panX = start.panX + (targetPan.x - start.panX) * ease;
    state.camera.panY = start.panY + (targetPan.y - start.panY) * ease;
    state.camera.zoom = start.zoom + (targetZoom - start.zoom) * ease;
    applyCamera();
    if (t < 1) cameraAnimHandle = requestAnimationFrame(step);
    else cameraAnimHandle = null;
  }
  cameraAnimHandle = requestAnimationFrame(step);
}

function boundingBoxOfLevel(level) {
  let minX = level.rootItem.x - level.rootItem.w / 2, maxX = level.rootItem.x + level.rootItem.w / 2;
  let minY = level.rootItem.y, maxY = level.rootItem.y + level.rootItem.h;
  for (const it of level.childItems) {
    minX = Math.min(minX, it.x - it.w / 2);
    maxX = Math.max(maxX, it.x + it.w / 2);
    minY = Math.min(minY, it.y);
    maxY = Math.max(maxY, it.y + it.h);
  }
  return { minX, minY, maxX, maxY };
}

function fitToBox(box, { animate = true, padding = 90 } = {}) {
  const vr = viewportRect();
  const vw = vr.width, vh = vr.height;
  const boxW = Math.max(box.maxX - box.minX, 1), boxH = Math.max(box.maxY - box.minY, 1);
  let zoom = Math.min((vw - padding * 2) / boxW, (vh - padding * 2) / boxH);
  zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
  const cx = (box.minX + box.maxX) / 2, cy = (box.minY + box.maxY) / 2;
  const panX = vw / 2 - cx * zoom;
  const panY = vh / 2 - cy * zoom - 20; // small upward bias so the breadcrumb bar doesn't sit on the root
  if (animate) animateCameraTo({ x: panX, y: panY }, zoom);
  else { state.camera = { panX, panY, zoom }; applyCamera(); }
}

function fitCurrentLevel(animate) {
  const cur = state.levels[state.levels.length - 1];
  if (!cur) return;
  if (cur.wide || cur.empty) {
    const r = cur.rootItem;
    fitToBox({ minX: r.x - r.w / 2, minY: r.y, maxX: r.x + r.w / 2, maxY: r.y + r.h }, { animate, padding: 220 });
    return;
  }
  fitToBox(boundingBoxOfLevel(cur), { animate });
}

// ---------------------------------------------------- canvas interaction

function initCanvasInteraction(viewport) {
  let pan = null;

  viewport.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".world-node") || e.target.closest(".float-panel") ||
        e.target.closest(".gate-tab") || e.target.closest(".canvas-toolbar")) return;
    pan = { startX: e.clientX, startY: e.clientY, startPanX: state.camera.panX, startPanY: state.camera.panY, moved: false, pointerId: e.pointerId };
    viewport.setPointerCapture(e.pointerId);
  });

  viewport.addEventListener("pointermove", (e) => {
    if (!pan || e.pointerId !== pan.pointerId) return;
    const dx = e.clientX - pan.startX, dy = e.clientY - pan.startY;
    // Threshold before a press counts as a drag -- without this, a click
    // with a couple of pixels of unintended movement pans the camera by
    // that same couple of pixels instead of registering as a tap, which
    // for a background click (which triggers "go up a level") silently
    // eats the gesture. Below the threshold we do nothing at all, so a
    // clean click always reaches pointerup with pan.moved still false.
    if (!pan.moved && Math.hypot(dx, dy) > PAN_THRESHOLD) {
      pan.moved = true;
      viewport.classList.add("panning");
    }
    if (pan.moved) {
      state.camera.panX = pan.startPanX + dx;
      state.camera.panY = pan.startPanY + dy;
      applyCamera();
    }
  });

  viewport.addEventListener("pointerup", (e) => {
    if (!pan || e.pointerId !== pan.pointerId) return;
    const wasDrag = pan.moved;
    viewport.classList.remove("panning");
    pan = null;
    if (!wasDrag) ascendOneLevel();
  });
  viewport.addEventListener("pointercancel", () => { viewport.classList.remove("panning"); pan = null; });

  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const vr = viewportRect();
    const factor = Math.pow(1.0018, -e.deltaY);
    const changed = zoomAt(e.clientX - vr.left, e.clientY - vr.top, factor, false);
    if (!changed && e.deltaY > 0) {
      const now = performance.now();
      if (now - state.lastAscendAt > 500) {
        state.lastAscendAt = now;
        ascendOneLevel();
      }
    }
  }, { passive: false });
}

function onWindowResize() {
  for (const id of Object.keys(state.panelPositions)) {
    const el = state.els[`panel_${id}`];
    if (el) clampPanelElement(el, id);
  }
}

// ------------------------------------------------------------ node layout

function buildNodeEl(r) {
  const frac = r.cancelledFraction || (r.status === "cancelled" ? 1 : 0);
  const fullyDead = frac >= 0.999;
  const classes = ["world-node"];
  if (r.isCurrentRoot) classes.push("world-node-root");
  if (fullyDead) classes.push("cancelled-full");
  const el = h("div", { class: classes.join(" "), title: r.label });
  el.appendChild(h("div", { class: "world-node-label", text: r.label }));
  el.appendChild(h("div", { class: "world-node-count", text: `${r.value} page${r.value === 1 ? "" : "s"}` }));
  if (fullyDead) {
    const mark = renderDeadMarkIcon();
    mark.classList.add("node-dead-mark");
    el.appendChild(mark);
  } else {
    el.appendChild(h("span", { class: "world-node-dot", style: `background:${statusColor(r.status, frac)}` }));
  }
  el.addEventListener("click", (e) => {
    e.stopPropagation();
    selectRegion(r, el);
  });
  return el;
}

function updateNodeVisuals(el, region) {
  const frac = region.cancelledFraction || (region.status === "cancelled" ? 1 : 0);
  const fullyDead = frac >= 0.999;
  el.classList.toggle("cancelled-full", fullyDead);
  const countEl = el.querySelector(".world-node-count");
  if (countEl) countEl.textContent = `${region.value} page${region.value === 1 ? "" : "s"}`;
  const dot = el.querySelector(".world-node-dot");
  const mark = el.querySelector(".node-dead-mark");
  if (fullyDead) {
    if (dot) dot.remove();
    if (!mark) { const m = renderDeadMarkIcon(); m.classList.add("node-dead-mark"); el.appendChild(m); }
  } else {
    if (mark) mark.remove();
    if (dot) dot.style.background = statusColor(region.status, frac);
    else el.appendChild(h("span", { class: "world-node-dot", style: `background:${statusColor(region.status, frac)}` }));
  }
}

function renderDeadMarkIcon() {
  const svg = svgEl("svg", { class: "dead-mark", viewBox: "-6 -6 12 12", width: 22, height: 22 });
  const s = 3;
  svg.appendChild(svgEl("rect", { x: -s * 2, y: -s * 2, width: s * 4, height: s * 3, fill: "var(--parchment)" }));
  svg.appendChild(svgEl("rect", { x: -s * 1.3, y: -s * 0.8, width: s * 0.8, height: s * 0.8, fill: "var(--ash-dark)" }));
  svg.appendChild(svgEl("rect", { x: s * 0.5, y: -s * 0.8, width: s * 0.8, height: s * 0.8, fill: "var(--ash-dark)" }));
  svg.appendChild(svgEl("rect", { x: -s * 0.5, y: s * 0.4, width: s, height: s * 0.6, fill: "var(--ash-dark)" }));
  return svg;
}

// Places a fresh root box at world (0,0) -- only ever used for the very
// first level a run boots into, or after a mode switch / multi-level
// breadcrumb jump that has no node to promote in place from.
function placeRoot(region) {
  const el = buildNodeEl(region);
  el.style.left = "0px"; el.style.top = "-99999px";
  state.els.world.appendChild(el);
  const w = el.offsetWidth, h2 = el.offsetHeight;
  el.style.left = `${-w / 2}px`;
  el.style.top = "0px";
  return { region, el, w, h: h2, x: 0, y: 0 };
}

// One un-wrapped row of children, content-sized and measured before
// placement so "sized to content, no wrap" falls out of ordinary layout
// instead of hand-rolled text-fitting. Centered under anchorX; unlike the
// previous stage there is no viewport width to worry about at all --
// world space has no edges, so a wide level just occupies more world
// pixels and the camera pans/zooms to see it, never scrolls or wraps.
function placeChildrenRow(regions, anchorX, anchorY, animateEntry) {
  const world = state.els.world;
  const items = regions.map((r) => ({ region: r, el: buildNodeEl(r) }));
  for (const it of items) {
    it.el.style.left = "0px"; it.el.style.top = "-99999px";
    world.appendChild(it.el);
  }
  for (const it of items) { it.w = it.el.offsetWidth; it.h = it.el.offsetHeight; }
  const totalW = items.reduce((s, it) => s + it.w, 0) + NODE_GAP_X * Math.max(items.length - 1, 0);
  let leftX = anchorX - totalW / 2;
  for (const it of items) {
    it.el.style.left = `${leftX}px`;
    it.el.style.top = `${anchorY}px`;
    it.x = leftX + it.w / 2;
    it.y = anchorY;
    leftX += it.w + NODE_GAP_X;
    if (animateEntry && !prefersReducedMotion()) it.el.classList.add("world-enter");
  }
  return items;
}

// -------------------------------------------------------------- edges

function drawEdgeCasing(svg, start, end) {
  const dy = Math.max((end.y - start.y) / 2, 12);
  const d = `M ${start.x} ${start.y} C ${start.x} ${start.y + dy}, ${end.x} ${end.y - dy}, ${end.x} ${end.y}`;
  svg.appendChild(svgEl("path", { class: "edge-casing", d }));
  const inner = svgEl("path", { class: "edge-inner", d });
  svg.appendChild(inner);
  return inner;
}

const SPRITE_FRAME = 34;
const SPRITE_FRAMES = 5;
const SPRITE_SIZE = 20;
const SPRITE_TRANSIT_SECONDS = 6;
let spriteClipCounter = 0;
let activeSpriteImages = [];
let spriteFrameIndex = 0;

function renderSprite(x, y) {
  // Position and rotation are two SEPARATE nested <g>s -- a CSS
  // `transform` property (the wiggle keyframes) completely replaces an
  // SVG `transform` ATTRIBUTE on the same element rather than composing
  // with it, so one <g> carrying both silently collapsed every sprite to
  // the SVG's origin the instant the animation was active (see
  // LESSONS_LEARNED.md #64). The world canvas now adds ITS OWN transform
  // (pan/zoom) on an ancestor, which is a different element again and
  // composes fine -- but the outer/inner split here still has to hold on
  // its own regardless of that ancestor, so it's kept exactly as fixed.
  const outer = svgEl("g", { transform: `translate(${x - SPRITE_SIZE / 2}, ${y - SPRITE_SIZE / 2})` });
  const inner = svgEl("g", { class: prefersReducedMotion() ? "" : "sprite-wiggle" });
  outer.appendChild(inner);
  const clipId = `sprite-clip-${spriteClipCounter++}`;
  const defs = svgEl("defs", {});
  const clip = svgEl("clipPath", { id: clipId });
  clip.appendChild(svgEl("rect", { x: 0, y: 0, width: SPRITE_SIZE, height: SPRITE_SIZE }));
  defs.appendChild(clip);
  outer.appendChild(defs);
  const img = svgEl("image", {
    class: "sprite-frame",
    href: "/static/assets/sprites/beetle.png",
    x: -spriteFrameIndex * SPRITE_SIZE, y: 0,
    width: SPRITE_SIZE * SPRITE_FRAMES, height: SPRITE_SIZE,
    "clip-path": `url(#${clipId})`,
    preserveAspectRatio: "none",
  });
  inner.appendChild(img);
  activeSpriteImages.push(img);
  return outer;
}

function startSpriteAnimationLoop() {
  if (prefersReducedMotion()) return;
  setInterval(() => {
    spriteFrameIndex = (spriteFrameIndex + 1) % SPRITE_FRAMES;
    for (const img of activeSpriteImages) img.setAttribute("x", String(-spriteFrameIndex * SPRITE_SIZE));
  }, 160);
}

function inFlightForRegion(r, inFlightByUrl) {
  const matches = [];
  if (r.url) {
    if (inFlightByUrl.has(r.url)) matches.push(inFlightByUrl.get(r.url));
  } else if (r.path) {
    const prefix = `https://${state.seedHost}${r.path}`;
    for (const [url, f] of inFlightByUrl) if (url.startsWith(prefix)) matches.push(f);
  }
  return matches;
}

function currentInFlightMap() {
  const m = new Map();
  if (state.liveStatus) for (const f of state.liveStatus.in_flight || []) m.set(f.url, f);
  return m;
}

// Clears and redraws just the in-flight fill/percentage/beetle overlay
// for a level -- called on every poll tick, independent of navigation, so
// live progress updates without rebuilding node/edge geometry.
function applyInFlightForLevel(level, inFlightByUrl) {
  if (!level.svg) return;
  level.svg.querySelectorAll(".edge-fill, .edge-pct").forEach((el) => el.remove());
  level.svg.querySelectorAll("g").forEach((g) => {
    if (g.querySelector("image")) g.remove(); // sprite groups
  });
  activeSpriteImages = activeSpriteImages.filter((img) => img.isConnected);
  for (const it of level.edgePaths) {
    const matches = inFlightForRegion(it.region, inFlightByUrl);
    if (!matches.length) continue;
    const d = it.path.getAttribute("d");
    const totalLen = it.path.getTotalLength();
    matches.forEach((f, i) => {
      const t = Math.max(0, Math.min(1, (f.running_seconds || 0) / SPRITE_TRANSIT_SECONDS));
      const filled = t * totalLen;
      level.svg.appendChild(svgEl("path", { class: "edge-fill", d, "stroke-dasharray": `${filled} ${totalLen}` }));
      const pt = it.path.getPointAtLength(filled);
      const pct = svgEl("text", { class: "edge-pct", x: pt.x + 8, y: pt.y - 6 - i * 12 });
      pct.textContent = `${Math.round(t * 100)}%`;
      level.svg.appendChild(pct);
      level.svg.appendChild(renderSprite(pt.x, pt.y));
    });
  }
}

// -------------------------------------------------------------- levels

// Builds the children row + edges for a level whose root is `rootItem`
// (already placed/positioned -- either a fresh root box at (0,0), or an
// existing node being promoted in place). Returns a level record.
async function buildLevelForRoot(rootItem, animateEntry) {
  rootItem.el.classList.add("world-node-root");
  rootItem.el.classList.remove("selected");
  rootItem.region.isCurrentRoot = true;

  let regions;
  try {
    regions = await currentLevelRegions();
  } catch (err) {
    return { rootItem, childItems: [], edgePaths: [], svg: null, error: err };
  }

  const level = { rootItem, childItems: [], edgePaths: [], svg: null, wide: false, empty: false };

  if (regions.length === 0) {
    level.empty = true;
    const isLeafPage = state.mode === "path-tree" ? !!state.breadcrumbs[state.breadcrumbs.length - 1].pathTreeNode.url_node : true;
    level.emptyIsLeaf = isLeafPage;
    return level;
  }
  if (regions.length > WIDE_REGION_THRESHOLD) {
    level.wide = true;
    level.wideRegions = regions;
    return level;
  }

  const anchorX = rootItem.x, anchorY = rootItem.y + rootItem.h + LEVEL_GAP;
  const items = placeChildrenRow(regions, anchorX, anchorY, animateEntry);

  const svg = svgEl("svg", { class: "world-edge-layer" });
  state.els.world.appendChild(svg);
  const rootAnchor = { x: rootItem.x, y: rootItem.y + rootItem.h };
  const edgePaths = items.map((it) => ({
    region: it.region,
    path: drawEdgeCasing(svg, rootAnchor, { x: it.x, y: it.y }),
  }));

  level.childItems = items;
  level.edgePaths = edgePaths;
  level.svg = svg;
  return level;
}

function removeLevelDOM(level, keepEl) {
  if (!level) return;
  if (level.svg) level.svg.remove();
  for (const it of level.childItems) if (it.el !== keepEl) it.el.remove();
  if (level.rootItem && level.rootItem.el !== keepEl) level.rootItem.el.remove();
  if (level.widePanelEl) level.widePanelEl.remove();
}

function dimLevelExcept(level, exceptEl) {
  if (level.rootItem.el !== exceptEl) level.rootItem.el.classList.add("world-dimmed");
  for (const it of level.childItems) if (it.el !== exceptEl) it.el.classList.add("world-dimmed");
  if (level.svg) level.svg.querySelectorAll(".edge-casing, .edge-inner, .edge-fill, .edge-pct").forEach((el) => el.classList.add("world-dimmed"));
  if (level.widePanelEl) { level.widePanelEl.remove(); level.widePanelEl = null; }
}

function undimLevel(level) {
  level.rootItem.el.classList.remove("world-dimmed");
  for (const it of level.childItems) it.el.classList.remove("world-dimmed");
  if (level.svg) level.svg.querySelectorAll(".world-dimmed").forEach((el) => el.classList.remove("world-dimmed"));
}

// Tears down every level and builds a fresh one anchored at world (0,0)
// for whatever state.breadcrumbs currently points at. Used at boot, on a
// projection-mode switch, and as the fallback for a breadcrumb jump of
// more than one level (no single node to grow from in that case).
async function rebuildRootLevel(animate) {
  for (const lvl of state.levels) removeLevelDOM(lvl);
  state.levels = [];
  state.selected = null;
  state.selectedEl = null;
  hideDetailPanel();
  const top = state.breadcrumbs[state.breadcrumbs.length - 1];
  const rootRegion = regionForCrumb(top);
  const rootItem = placeRoot(rootRegion);
  const level = await buildLevelForRoot(rootItem, false);
  if (level.error) { showCanvasError(level.error); return; }
  applyInFlightForLevel(level, currentInFlightMap());
  state.levels.push(level);
  attachWideListIfNeeded(level);
  renderBreadcrumbBar();
  renderGatesDock();
  renderStatsPanel();
  fitCurrentLevel(animate);
}

async function descendInto(region, nodeEl) {
  if (!region || !region.hasChildren || region.isCurrentRoot) return;
  const cur = state.levels[state.levels.length - 1];
  if (!cur) return;

  if (state.levels.length > 1) {
    const doomed = state.levels.shift();
    removeLevelDOM(doomed, cur.rootItem.el);
  }
  dimLevelExcept(cur, nodeEl);

  if (state.mode === "path-tree") {
    state.breadcrumbs.push({ label: region.label, path: region.path, pathTreeNode: region.pathTreeNode });
  } else {
    state.breadcrumbs.push({ label: region.label, discoveryRootUrl: region.url });
  }
  state.selected = null;
  state.selectedEl = null;
  hideDetailPanel();
  renderBreadcrumbBar();

  const w = nodeEl.offsetWidth, hh = nodeEl.offsetHeight;
  const x = parseFloat(nodeEl.style.left) + w / 2;
  const y = parseFloat(nodeEl.style.top);
  const rootItem = { region, el: nodeEl, w, h: hh, x, y };

  const level = await buildLevelForRoot(rootItem, true);
  if (level.error) { showCanvasError(level.error); return; }
  applyInFlightForLevel(level, currentInFlightMap());
  state.levels.push(level);
  attachWideListIfNeeded(level);
  renderGatesDock();
  fitCurrentLevel(true);
}

function ascendOneLevel() {
  if (state.levels.length < 2) return;
  const cur = state.levels.pop();
  const prev = state.levels[state.levels.length - 1];
  cur.rootItem.el.classList.remove("world-node-root");
  removeLevelDOM(cur, cur.rootItem.el);
  undimLevel(prev);
  attachWideListIfNeeded(prev);
  state.breadcrumbs.pop();
  state.selected = null;
  state.selectedEl = null;
  hideDetailPanel();
  renderBreadcrumbBar();
  renderGatesDock();
  fitCurrentLevel(true);
}

async function goToBreadcrumbIndex(i) {
  if (i === state.breadcrumbs.length - 1) return;
  if (i === state.breadcrumbs.length - 2) { ascendOneLevel(); return; }
  state.breadcrumbs = state.breadcrumbs.slice(0, i + 1);
  await rebuildRootLevel(true);
}

function renderBreadcrumbBar() {
  const bar = state.els.breadcrumbBar;
  bar.innerHTML = "";
  state.breadcrumbs.forEach((crumb, i) => {
    if (i > 0) bar.appendChild(h("span", { class: "sep", text: "›" }));
    bar.appendChild(h("span", { class: "crumb", text: crumb.label, onclick: () => goToBreadcrumbIndex(i) }));
  });
}

function ensureDiscoveryRootForBreadcrumbs() {
  const effective = findEffectiveRoot(state.pathTreeRoot);
  const rootUrl = `https://${state.seedHost}${effective.path}`;
  state.breadcrumbs = [{ label: "(root)", path: effective.path, discoveryRootUrl: rootUrl }];
}

// -------------------------------------------------------- gates (edge dock)

function renderGatesDock() {
  const dock = state.els.gatesDock;
  dock.innerHTML = "";
  for (const gate of gatesForCurrentLevel()) {
    const shut = state.sameSiteOnly;
    const tab = h("div", { class: "gate-tab" + (shut ? " shut" : "") });
    tab.appendChild(h("span", { class: "gate-host", text: gate.host }));
    tab.appendChild(h("span", { class: "gate-status" }, [
      shut ? "SHUT" : `${gate.count} link${gate.count === 1 ? "" : "s"}`,
      h("span", { class: "gate-arrow", text: shut ? "✕" : "→" }),
    ]));
    if (!shut) tab.addEventListener("click", () => selectGate(gate));
    dock.appendChild(tab);
  }
}

// --------------------------------------------------------- wide fallback

function attachWideListIfNeeded(level) {
  if (!level.wide) return;
  const { panel, body } = makeFloatingPanel("wide-list", `${level.wideRegions.length} pages`, { x: window.innerWidth / 2 - 320, y: 66 });
  panel.classList.add("wide-region-panel");
  renderWideListInto(body, level.wideRegions);
  document.body.appendChild(panel);
  level.widePanelEl = panel;
}

function renderWideListInto(body, regions) {
  body.innerHTML = "";
  body.appendChild(h("p", { class: "hint", text: `${regions.length} items — showing as a list, not spread across the canvas, above ${WIDE_REGION_THRESHOLD}.` }));
  const sorted = regions.slice().sort((a, b) => a.label.localeCompare(b.label));
  const table = h("table", {});
  const tbody = h("tbody", {});
  for (const r of sorted) {
    tbody.appendChild(h("tr", { class: "clickable", onclick: () => selectRegion(r, null) }, [
      h("td", { text: r.label }),
      h("td", {}, [r.status ? badge(r.status) : h("span", { class: "hint", text: `${r.value} pages` })]),
    ]));
  }
  table.appendChild(tbody);
  body.appendChild(table);
}

// ------------------------------------------------------------ selection

function selectRegion(region, el) {
  document.querySelectorAll(".world-node.selected").forEach((n) => n.classList.remove("selected"));
  if (el) el.classList.add("selected");
  state.selected = region;
  state.selectedEl = el;
  state.lastActionMessage = null;
  showDetailPanel();
}

function selectGate(gate) {
  state.selected = { gate };
  state.selectedEl = null;
  state.lastActionMessage = null;
  showDetailPanel();
}

async function runAction(apiFn, target, out) {
  out.textContent = "Working…";
  try {
    const result = await apiFn(state.runId, target);
    await refreshCurrentLevelVisuals();
    state.lastActionMessage = result.output || "Done.";
    renderDetailPanel();
  } catch (err) {
    out.textContent = (err.message || err) + (err.detail ? " " + err.detail : "");
  }
}

async function refreshCurrentLevelVisuals() {
  await refreshPathTreeData();
  if (state.mode === "path-tree") {
    state.breadcrumbs = state.breadcrumbs.map((b) => {
      if (b.path === undefined) return b;
      const fresh = findNodeByPath(state.pathTreeRoot, b.path);
      return fresh ? { ...b, pathTreeNode: fresh } : b;
    });
  }
  state.discoveryCache.clear();
  const cur = state.levels[state.levels.length - 1];
  if (!cur) return;
  let regions;
  try { regions = await currentLevelRegions(); } catch (e) { return; }

  const top = state.breadcrumbs[state.breadcrumbs.length - 1];
  const freshRoot = regionForCrumb(top);
  updateNodeVisuals(cur.rootItem.el, freshRoot);
  cur.rootItem.region = freshRoot;

  if (cur.wide && cur.widePanelEl) {
    renderWideListInto(cur.widePanelEl.querySelector(".float-panel-body"), regions);
  } else {
    for (const it of cur.childItems) {
      const fresh = regions.find((r) => r.id === it.region.id);
      if (fresh) { it.region = fresh; updateNodeVisuals(it.el, fresh); }
    }
  }

  if (state.selected && state.selected.id !== undefined) {
    const freshSel = regions.find((r) => r.id === state.selected.id) || (freshRoot.id === state.selected.id ? freshRoot : null);
    if (freshSel) state.selected = freshSel;
  }
  renderStatsPanel();
}

// ------------------------------------------------------- floating panels

function loadPanelPrefs() {
  try {
    const raw = localStorage.getItem("map-panel-prefs");
    const parsed = raw ? JSON.parse(raw) : {};
    state.panelPositions = parsed.positions || {};
    state.panelCollapsed = parsed.collapsed || {};
  } catch (e) {
    state.panelPositions = {};
    state.panelCollapsed = {};
  }
}

function savePanelPrefs() {
  try {
    localStorage.setItem("map-panel-prefs", JSON.stringify({ positions: state.panelPositions, collapsed: state.panelCollapsed }));
  } catch (e) { /* private browsing / quota -- position just won't persist */ }
}

// Restored (or default) positions can land off-screen -- a panel dragged
// near the right edge on a wide monitor is outside the viewport entirely
// on a narrower one next time, with no way to drag it back since you
// can't grab what you can't see. Every panel is clamped into the CURRENT
// viewport on creation and again on window resize.
function clampPanelElement(panel, id) {
  const w = panel.offsetWidth || 300, h2 = panel.offsetHeight || 100;
  const maxX = Math.max(4, window.innerWidth - w - 4);
  const maxY = Math.max(4, window.innerHeight - h2 - 4);
  const x = Math.min(Math.max(4, parseFloat(panel.style.left) || 0), maxX);
  const y = Math.min(Math.max(4, parseFloat(panel.style.top) || 0), maxY);
  panel.style.left = `${x}px`;
  panel.style.top = `${y}px`;
  if (id) state.panelPositions[id] = { x, y };
}

function makeFloatingPanel(id, title, defaultPos) {
  const pos = state.panelPositions[id] || defaultPos;
  const collapsed = !!state.panelCollapsed[id];
  const panel = h("div", { class: "float-panel" + (collapsed ? " collapsed" : ""), style: `left:${pos.x}px; top:${pos.y}px;`, "data-panel": id });
  const header = h("div", { class: "float-panel-header" });
  header.appendChild(h("span", { class: "float-panel-title", text: title }));
  const toggle = h("button", { class: "float-panel-toggle secondary", text: collapsed ? "+" : "–", title: collapsed ? "Expand" : "Collapse" });
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    const nowCollapsed = !state.panelCollapsed[id];
    state.panelCollapsed[id] = nowCollapsed;
    savePanelPrefs();
    panel.classList.toggle("collapsed", nowCollapsed);
    toggle.textContent = nowCollapsed ? "+" : "–";
    toggle.title = nowCollapsed ? "Expand" : "Collapse";
  });
  // .float-panel-toggle is a DIRECT child of header (not wrapped) --
  // showDetailPanel() inserts its close button before it via
  // header.insertBefore(), which requires the reference node to be a
  // real child of header, not merely a descendant.
  header.appendChild(toggle);
  panel.appendChild(header);
  const body = h("div", { class: "float-panel-body" });
  panel.appendChild(body);
  makePanelDraggable(panel, header, id);
  state.els[`panel_${id}`] = panel;
  requestAnimationFrame(() => clampPanelElement(panel, id));
  return { panel, body, header, toggle };
}

function makePanelDraggable(panel, handle, id) {
  let dragging = false, startX, startY, startLeft, startTop;
  handle.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".float-panel-toggle") || e.target.closest(".float-panel-close")) return;
    dragging = true;
    startX = e.clientX; startY = e.clientY;
    const r = panel.getBoundingClientRect();
    startLeft = r.left; startTop = r.top;
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const nx = startLeft + (e.clientX - startX);
    const ny = startTop + (e.clientY - startY);
    const maxX = window.innerWidth - panel.offsetWidth - 4;
    const maxY = window.innerHeight - panel.offsetHeight - 4;
    panel.style.left = `${Math.max(4, Math.min(nx, maxX))}px`;
    panel.style.top = `${Math.max(4, Math.min(ny, maxY))}px`;
  });
  handle.addEventListener("pointerup", () => {
    if (!dragging) return;
    dragging = false;
    state.panelPositions[id] = { x: parseFloat(panel.style.left), y: parseFloat(panel.style.top) };
    savePanelPrefs();
  });
}

function resetPanelLayout() {
  try { localStorage.removeItem("map-panel-prefs"); } catch (e) {}
  state.panelPositions = {};
  state.panelCollapsed = {};
  buildPanels();
  if (state.selected) showDetailPanel(); else hideDetailPanel();
}

function defaultPanelPositions() {
  const vw = window.innerWidth;
  return {
    controls: { x: 16, y: 110 },
    stats: { x: 16, y: 320 },
    activity: { x: 16, y: 470 },
    detail: { x: Math.max(16, vw - 336), y: 110 },
  };
}

function buildPanels() {
  ["panel_controls", "panel_stats", "panel_activity"].forEach((k) => { if (state.els[k]) state.els[k].remove(); });
  const defaults = defaultPanelPositions();

  const controls = makeFloatingPanel("controls", "Controls", defaults.controls);
  document.body.appendChild(controls.panel);
  state.panelBodies = state.panelBodies || {};
  state.panelBodies.controls = controls.body;
  renderControlsPanel();

  const stats = makeFloatingPanel("stats", "Stats", defaults.stats);
  document.body.appendChild(stats.panel);
  state.panelBodies.stats = stats.body;
  renderStatsPanel();

  const activity = makeFloatingPanel("activity", "Activity", defaults.activity);
  document.body.appendChild(activity.panel);
  state.panelBodies.activity = activity.body;
  renderActivityPanel();
}

function renderControlsPanel() {
  const body = state.panelBodies.controls;
  body.innerHTML = "";

  body.appendChild(h("div", { class: "field-block" }, [
    h("span", { class: "field-name", text: "Projection" }),
    radioGroup("projection-mode", ["path-tree", "discovery"], state.mode, (v) => {
      state.mode = v;
      if (v === "discovery") ensureDiscoveryRootForBreadcrumbs();
      rebuildRootLevel(true);
    }),
  ]));

  body.appendChild(h("label", { class: "checkbox-row" }, [
    h("input", {
      type: "checkbox", checked: state.sameSiteOnly ? "checked" : null,
      onchange: (e) => { state.sameSiteOnly = e.target.checked; renderGatesDock(); },
    }),
    "Same-site only (shuts cross-host gates)",
  ]));

  if (state.run.job_state) {
    body.appendChild(h("div", { class: "btn-row" }, controlButtons()));
  }
}

function radioGroup(name, options, current, onChange) {
  const wrap = h("span", { class: "radio-group" });
  for (const opt of options) {
    const id = `${name}-${opt}`;
    const pair = h("span", { class: "radio-pair" });
    pair.appendChild(h("input", {
      type: "radio", name, id, value: opt,
      checked: opt === current ? "checked" : null,
      onchange: () => onChange(opt),
    }));
    pair.appendChild(h("label", { for: id, text: opt }));
    wrap.appendChild(pair);
  }
  return wrap;
}

function controlButtons() {
  const st = state.run.job_state;
  const buttons = [];
  const wrap = async (fn) => {
    try {
      await fn();
      const job = await Api.runJob(state.runId);
      if (job.job) state.run.job_state = job.job.state;
      renderControlsPanel();
    } catch (err) {
      alert((err.message || err) + (err.detail ? "\n" + err.detail : ""));
    }
  };
  if (st === "running") {
    buttons.push(h("button", { class: "secondary", text: "Pause", onclick: () => wrap(() => Api.pauseRun(state.runId)) }));
    buttons.push(h("button", { class: "secondary", text: "Unpause", onclick: () => wrap(() => Api.unpauseRun(state.runId)) }));
    buttons.push(h("button", { class: "danger", text: "Stop", onclick: () => wrap(() => Api.stopRun(state.runId)) }));
  } else {
    buttons.push(h("button", { text: "Resume (continue as-is)", onclick: () => wrap(() => Api.resumeRun(state.runId)) }));
  }
  return buttons;
}

function renderStatsPanel() {
  const body = state.panelBodies.stats;
  body.innerHTML = "";
  const counts = (state.liveStatus && state.liveStatus.counts_by_status) || state.run.counts_by_status || {};
  const keys = Object.keys(counts);
  const chips = h("div", { class: "map-stats" });
  if (keys.length === 0) {
    chips.appendChild(h("span", { class: "hint", text: "No pages yet." }));
  } else {
    for (const k of keys.sort()) chips.appendChild(h("span", { class: "status-chip" }, [k, h("b", { text: String(counts[k]) })]));
  }
  body.appendChild(chips);
  const activeN = state.liveStatus ? (state.liveStatus.in_flight || []).length : 0;
  body.appendChild(h("div", { class: "hint", text: `Active fetches: ${activeN}`, style: "margin-top:0.4rem" }));
}

function renderActivityPanel() {
  const body = state.panelBodies.activity;
  body.innerHTML = "";
  const log = h("div", { class: "terminal-log" });
  body.appendChild(log);
  if (state.activityLog.length === 0) {
    log.appendChild(h("div", { class: "log-empty", text: "> waiting for activity…" }));
  } else {
    for (const line of state.activityLog) log.appendChild(h("div", { class: `log-line status-${line.cls}`, text: line.text }));
    log.scrollTop = log.scrollHeight;
  }
}

const LOG_VERBS = {
  done: "extracted", mapped: "mapped", failed: "FAILED", cancelled: "cancelled",
  skipped_extract: "skipped (relevance)", skipped_follow: "skipped (follow)",
};

function shortPathFromUrl(url) {
  try { return new URL(url).pathname || url; } catch (e) { return url; }
}

function ingestLiveStatusIntoLog(live) {
  if (!live || !live.recently_completed) return;
  const rows = live.recently_completed.slice().reverse();
  for (const row of rows) {
    const key = `${row.id}|${row.status}|${row.updated_at}`;
    if (state.seenLogKeys.has(key)) continue;
    state.seenLogKeys.add(key);
    const verb = LOG_VERBS[row.status] || row.status;
    state.activityLog.push({ text: `> ${verb}  ${shortPathFromUrl(row.id)}`, cls: row.status });
  }
  if (state.activityLog.length > 200) state.activityLog = state.activityLog.slice(-200);
}

// ------------------------------------------------------------ node detail

function hideDetailPanel() {
  if (state.els.panel_detail) { state.els.panel_detail.remove(); state.els.panel_detail = null; }
}

function showDetailPanel() {
  hideDetailPanel();
  const defaults = defaultPanelPositions();
  const { panel, body } = makeFloatingPanel("detail", "Selection", defaults.detail);
  const header = panel.querySelector(".float-panel-header");
  const closeBtn = h("button", { class: "float-panel-close", text: "✕", title: "Dismiss" });
  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    document.querySelectorAll(".world-node.selected").forEach((n) => n.classList.remove("selected"));
    state.selected = null;
    state.selectedEl = null;
    hideDetailPanel();
  });
  header.insertBefore(closeBtn, header.querySelector(".float-panel-toggle"));
  document.body.appendChild(panel);
  renderDetailPanel();
}

function renderDetailPanel() {
  if (!state.selected) { hideDetailPanel(); return; }
  const panel = state.els.panel_detail;
  if (!panel) return;
  const body = panel.querySelector(".float-panel-body");
  body.innerHTML = "";
  const sel = state.selected;

  if (sel.gate) {
    body.appendChild(h("h3", { text: sel.gate.host }));
    body.appendChild(h("p", { text: `${sel.gate.count} link(s) leave the crawled site to this host.` }));
    return;
  }

  if (state.lastActionMessage) body.appendChild(h("p", { class: "hint", text: `✓ ${state.lastActionMessage}` }));
  body.appendChild(h("h3", { text: sel.label }));
  const dl = h("dl", {});
  if (sel.url) {
    dl.appendChild(h("dt", { text: "URL" }));
    dl.appendChild(h("dd", { text: sel.url }));
    dl.appendChild(h("dt", { text: "Status" }));
    dl.appendChild(h("dd", {}, [badge(sel.status)]));
  } else {
    dl.appendChild(h("dt", { text: "Path prefix" }));
    dl.appendChild(h("dd", { text: sel.path || "(top level)" }));
  }
  dl.appendChild(h("dt", { text: "Pages under this" }));
  dl.appendChild(h("dd", { text: String(sel.value) }));
  body.appendChild(dl);

  const actionsOut = h("div", { class: "hint" });
  const buttons = [];
  if (sel.hasChildren && !sel.isCurrentRoot) {
    buttons.push(h("button", {
      text: "Zoom in ▸",
      onclick: () => descendInto(sel, state.selectedEl),
    }));
  }
  const target = sel.url ? { url: sel.url } : { prefix: `https://${state.seedHost}${sel.path}` };
  buttons.push(h("button", { class: "secondary", text: "Select for extraction", onclick: () => runAction(Api.select, target, actionsOut) }));
  buttons.push(h("button", { class: "danger", text: "Cancel", onclick: () => runAction(Api.cancel, target, actionsOut) }));
  body.appendChild(h("div", { class: "btn-row" }, buttons));
  body.appendChild(actionsOut);
}

// ------------------------------------------------------------- polling

function startPolling() {
  stopPolling();
  state.activityLog.push({ text: "> live feed connected", cls: "system" });
  renderActivityPanel();
  updateLivePill();
  state.pollTimer = setInterval(async () => {
    try {
      const [job, live] = await Promise.all([Api.runJob(state.runId), Api.liveStatus(state.runId)]);
      state.liveStatus = live;
      ingestLiveStatusIntoLog(live);
      if (job.job && job.job.state !== "running") {
        stopPolling();
        state.run.job_state = job.job.state;
        state.activityLog.push({ text: `> run finished: ${job.job.state}`, cls: "system" });
      }
      refreshLiveOverlay();
    } catch (e) {
      // transient poll failure -- try again next tick
    }
  }, 3000);
  updateLivePill();
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  updateLivePill();
}

function updateLivePill() {
  const live = state.run.job_state === "running";
  const pill = state.els.livePill;
  if (!pill) return;
  pill.textContent = live ? "● LIVE" : "static view";
  pill.classList.toggle("static", !live);
}

// Refreshes everything that can change without touching camera or level
// structure: stats, activity log, in-flight fills/beetles on whichever
// levels are currently built (current + the one dimmed level, if any).
function refreshLiveOverlay() {
  const inFlight = currentInFlightMap();
  for (const lvl of state.levels) applyInFlightForLevel(lvl, inFlight);
  renderStatsPanel();
  renderActivityPanel();
  updateLivePill();
}

boot();
