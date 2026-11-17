/* TNxFlix Web — single-page app (vanilla JS).
   Data: the TNxFlix backend (same origin by default, or window.TNXFLIX_API).
   Playback: MP4 via /proxy, DASH via /dash + dash.js, subs via /sub. */
"use strict";

const API = (window.TNXFLIX_API || "").replace(/\/+$/, "");
const apiUrl = (p) => API + p;
const enc = encodeURIComponent;
const app = document.getElementById("app");

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
async function jget(path) {
  const r = await fetch(apiUrl(path));
  if (!r.ok) throw new Error("Request failed (HTTP " + r.status + ")");
  return r.json();
}
function toast(msg, ms) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), ms || 3200);
}
function loadingView(text) {
  app.innerHTML = '<div class="center-msg"><div class="spinner"></div><p>' +
    esc(text || "Loading...") + "</p></div>";
}
function errorView(title, msg, retryHash) {
  app.innerHTML = '<div class="error-box"><h2>' + esc(title) + "</h2><p>" + esc(msg) +
    '</p><button class="btn" onclick="location.reload()">Reload</button>' +
    (retryHash ? ' <a class="btn secondary" href="' + retryHash + '">Back</a>' : "") + "</div>";
}
function fmtTime(sec) {
  if (!isFinite(sec) || sec == null) return "0:00";
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

/* ---------------- subject normalisation ---------------- */
function normSubject(s) {
  if (!s || typeof s !== "object") return null;
  const id = s.subjectId != null ? s.subjectId : s.subject_id;
  const title = s.title;
  if (id == null || !title) return null;
  let cover = null;
  if (s.cover && typeof s.cover === "object") cover = s.cover.url;
  else if (typeof s.cover === "string") cover = s.cover;
  return {
    id: String(id),
    title: title,
    cover: cover,
    rating: s.imdbRatingValue != null ? s.imdbRatingValue : (s.rating != null ? s.rating : null),
    year: String(s.releaseDate || s.year || "").slice(0, 4) || null,
    type: s.subjectType === 1 ? "Movie" : (s.subjectType === 2 ? "Series" : (s.type || null)),
  };
}
function subjectsFrom(node) {
  if (!node || typeof node !== "object") return [];
  const keys = ["subjects", "subjectList", "items", "list"];
  for (const k of keys) {
    if (Array.isArray(node[k])) {
      const out = node[k].map(normSubject).filter(Boolean);
      if (out.length) return out;
    }
  }
  return [];
}
function cardHTML(item, extra) {
  const sub = [];
  if (item.rating) sub.push('<span class="rating">★ ' + esc(item.rating) + "</span>");
  if (item.year) sub.push("<span>" + esc(item.year) + "</span>");
  if (item.type) sub.push("<span>" + esc(item.type) + "</span>");
  let progress = "";
  if (extra && extra.progress != null && extra.duration) {
    const pct = Math.min(100, Math.round((extra.progress / extra.duration) * 100));
    progress = '<div class="progress"><i style="width:' + pct + '%"></i></div>';
  }
  const href = extra && extra.href ? extra.href : "#/title/" + enc(item.id);
  return '<a class="card" href="' + href + '">' +
    (item.cover ? '<img loading="lazy" src="' + esc(item.cover) + '" alt="">' : '<img alt="">') +
    '<div class="card-title">' + esc(item.title) + '</div>' +
    '<div class="card-sub">' + sub.join("") + "</div>" + progress + "</a>";
}

/* ---------------- Continue Watching (localStorage) ---------------- */
const CW_KEY = "tnxflix_cw";
function cwLoad() {
  try { return JSON.parse(localStorage.getItem(CW_KEY) || "[]"); } catch (e) { return []; }
}
function cwSave(list) {
  try { localStorage.setItem(CW_KEY, JSON.stringify(list.slice(0, 12))); } catch (e) {}
}
function cwUpsert(entry) {
  const list = cwLoad().filter((e) => !(e.id === entry.id && e.se === entry.se && e.ep === entry.ep));
  list.unshift(entry);
  cwSave(list);
}
function cwRemove(id, se, ep) {
  cwSave(cwLoad().filter((e) => !(e.id === id && e.se === se && e.ep === ep)));
}
function progressKey(id, se, ep) { return "tnxflix_pos_" + id + "_" + se + "_" + ep; }

/* ---------------- Home ---------------- */
async function viewHome() {
  setNav("home");
  loadingView("Loading TNxFlix...");
  let sections = [];
  let heroPool = [];
  try {
    const home = await jget("/home");
    const data = home.data || home;
    const ops = Array.isArray(data.operatingList) ? data.operatingList : [];
    for (const op of ops) {
      const subs = subjectsFrom(op);
      if (subs.length) {
        heroPool = heroPool.concat(subs);
        if (subs.length >= 3) {
          sections.push({ title: op.title || op.name || "More to watch", subjects: subs });
        }
      }
    }
    if (Array.isArray(data.platformList)) {
      for (const p of data.platformList) heroPool = heroPool.concat(subjectsFrom(p), [normSubject(p)].filter(Boolean));
    }
  } catch (e) { /* fall through to the fallback rows below */ }

  if (!sections.length) {
    try {
      const [mv, tv] = await Promise.all([jget("/movies?page=1"), jget("/tv-series?page=1")]);
      const mItems = (mv.items || []).map(normSubject).filter(Boolean);
      const tItems = (tv.items || []).map(normSubject).filter(Boolean);
      if (mItems.length) { sections.push({ title: "Popular Movies", subjects: mItems }); heroPool = heroPool.concat(mItems); }
      if (tItems.length) sections.push({ title: "Popular TV Series", subjects: tItems });
    } catch (e) { /* handled by the empty check below */ }
  }
  if (!sections.length) {
    errorView("Could not load the catalogue", "The backend did not return any titles. Check that the TNxFlix API is running, then reload.");
    return;
  }

  const seen = new Set();
  heroPool = heroPool.filter((s) => s.cover && !seen.has(s.id) && seen.add(s.id)).slice(0, 5);
  const hero = heroPool[0];

  let html = "";
  if (hero) {
    html += '<section class="hero" style="background-image:url(\'' + esc(hero.cover) + '\')">' +
      '<div class="hero-content"><h1>' + esc(hero.title) + "</h1>" +
      '<div class="hero-meta">' +
      (hero.rating ? '<span class="rating">★ ' + esc(hero.rating) + "</span>" : "") +
      (hero.year ? "<span>" + esc(hero.year) + "</span>" : "") +
      (hero.type ? "<span>" + esc(hero.type) + "</span>" : "") + "</div>" +
      '<div class="hero-actions"><a class="btn" href="#/title/' + enc(hero.id) + '">▶ Play</a>' +
      '<a class="btn secondary" href="#/title/' + enc(hero.id) + '">More Info</a></div></div></section>';
  }

  const cw = cwLoad();
  if (cw.length) {
    html += '<section class="row"><h2>Continue Watching</h2><div class="row-posters">' +
      cw.map((e) => cardHTML(
        { id: e.id, title: e.title + (e.ep ? " · S" + e.se + " E" + e.ep : ""), cover: e.cover, rating: null, year: null, type: null },
        { progress: e.pos, duration: e.dur, href: "#/watch/" + enc(e.id) + "?se=" + e.se + "&ep=" + e.ep + (e.playId && e.playId !== e.id ? "&play=" + enc(e.playId) : "") }
      )).join("") + "</div></section>";
  }
  for (const sec of sections) {
    html += '<section class="row"><h2>' + esc(sec.title) + '</h2><div class="row-posters">' +
      sec.subjects.slice(0, 20).map((s) => cardHTML(s)).join("") + "</div></section>";
  }
  app.innerHTML = html;
}

/* ---------------- Movies / TV grids ---------------- */
const gridState = { kind: null, page: 0, loading: false };
async function viewGrid(kind) {
  setNav(kind === "movies" ? "movies" : "tv");
  gridState.kind = kind; gridState.page = 0; gridState.loading = false;
  app.innerHTML = '<div class="page"><h1>' + (kind === "movies" ? "Movies" : "TV Series") +
    '</h1><div class="grid" id="grid"></div>' +
    '<div class="load-more"><button class="btn secondary" id="moreBtn">Load more</button></div></div>';
  document.getElementById("moreBtn").addEventListener("click", () => loadGridPage());
  await loadGridPage();
}
async function loadGridPage() {
  if (gridState.loading) return;
  gridState.loading = true;
  const btn = document.getElementById("moreBtn");
  if (btn) btn.textContent = "Loading...";
  gridState.page += 1;
  try {
    const data = await jget("/" + (gridState.kind === "movies" ? "movies" : "tv-series") + "?page=" + gridState.page);
    const items = (data.items || []).map(normSubject).filter(Boolean);
    const grid = document.getElementById("grid");
    if (!grid) return;
    if (!items.length) {
      if (gridState.page === 1) grid.innerHTML = '<p style="color:#b3b3b3">No titles returned.</p>';
      if (btn) btn.classList.add("hidden");
      return;
    }
    grid.insertAdjacentHTML("beforeend", items.map((s) => cardHTML(s)).join(""));
    if (btn) btn.textContent = "Load more";
  } catch (e) {
    if (btn) btn.textContent = "Failed — tap to retry";
    if (gridState.page === 1) errorView("Could not load titles", e.message);
  } finally {
    gridState.loading = false;
  }
}

/* ---------------- Search ---------------- */
async function viewSearch(q) {
  setNav("");
  loadingView("Searching...");
  try {
    const data = await jget("/search?q=" + enc(q) + "&page=1");
    const items = (data.results || []).map((r) => ({
      id: r.subject_id, title: r.title, cover: r.cover,
      rating: r.imdb_rating, year: String(r.release_date || "").slice(0, 4) || null, type: r.type,
    })).filter((r) => r.id && r.title);
    app.innerHTML = '<div class="page"><h1>Results for "' + esc(q) + '"</h1>' +
      (items.length
        ? '<div class="grid">' + items.map((s) => cardHTML(s)).join("") + "</div>"
        : '<p style="color:#b3b3b3">No results found. Try another title.</p>') + "</div>";
  } catch (e) {
    errorView("Search failed", e.message);
  }
}

/* ---------------- Detail ---------------- */
let detailState = null;
async function viewDetail(id) {
  setNav("");
  loadingView("Loading title...");
  let d;
  try {
    d = await jget("/detail/" + enc(id));
  } catch (e) {
    errorView("Could not load this title", e.message, "#/");
    return;
  }
  detailState = { detail: d, playId: String(d.subject_id || id), seasons: [], season: 1 };
  const isSeries = d.subject_type !== 1;
  const meta = [];
  if (d.imdb_rating) meta.push('<span class="rating">★ ' + esc(d.imdb_rating) + "</span>");
  if (d.release_date) meta.push("<span>" + esc(String(d.release_date).slice(0, 4)) + "</span>");
  if (d.duration) meta.push("<span>" + esc(d.duration) + "</span>");
  if (d.type) meta.push("<span>" + esc(d.type) + "</span>");
  if (d.genre) meta.push("<span>" + esc(d.genre) + "</span>");
  if (d.country) meta.push("<span>" + esc(d.country) + "</span>");
  if (d.language) meta.push("<span>" + esc(d.language) + "</span>");

  let html = "";
  if (d.cover) {
    html += '<section class="detail-backdrop" style="background-image:url(\'' + esc(d.cover) + '\')">' +
      '<div class="detail-head"><h1>' + esc(d.title) + '</h1><div class="meta-line">' + meta.join("") + "</div></div></section>";
  }
  html += '<div class="detail-body">';
  if (!d.cover) html += "<h1>" + esc(d.title) + '</h1><div class="meta-line">' + meta.join("") + "</div>";
  if (d.description) html += '<p class="detail-desc">' + esc(d.description) + "</p>";

  const dubs = Array.isArray(d.dubs) ? d.dubs : [];
  if (dubs.length > 1) {
    html += '<div class="section-label">Audio / Language</div><div class="chip-row" id="dubRow">' +
      dubs.map((db) => '<button class="chip' + (String(db.subject_id) === detailState.playId ? " active" : "") +
        '" data-dub="' + esc(db.subject_id) + '">' + esc(db.lang_name || db.language || "Audio") + "</button>").join("") + "</div>";
  }

  if (isSeries) {
    html += '<div id="epArea"><div class="section-label">Episodes</div><p style="color:#b3b3b3;margin-top:10px">Loading episodes...</p></div>';
  } else {
    html += '<div style="margin-top:26px"><button class="btn" id="watchBtn">▶ Watch Now</button></div>';
  }
  html += "</div>";
  app.innerHTML = html;

  if (dubs.length > 1) {
    document.getElementById("dubRow").addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-dub]");
      if (!btn) return;
      detailState.playId = btn.dataset.dub;
      document.querySelectorAll("#dubRow .chip").forEach((c) => c.classList.toggle("active", c.dataset.dub === detailState.playId));
      if (isSeries) await loadEpisodes();
    });
  }
  if (isSeries) {
    await loadEpisodes();
  } else {
    document.getElementById("watchBtn").addEventListener("click", () => {
      location.hash = "#/watch/" + enc(id) + "?se=0&ep=0" +
        (detailState.playId !== String(id) ? "&play=" + enc(detailState.playId) : "");
    });
  }
}
async function loadEpisodes() {
  const area = document.getElementById("epArea");
  if (!area || !detailState) return;
  area.innerHTML = '<div class="section-label">Episodes</div><p style="color:#b3b3b3;margin-top:10px">Loading episodes...</p>';
  try {
    const data = await jget("/episodes/" + enc(detailState.playId));
    const seasons = data.seasons || [];
    detailState.seasons = seasons;
    if (!seasons.length) {
      area.innerHTML = '<div class="section-label">Episodes</div><p style="color:#b3b3b3;margin-top:10px">No episode information available for this audio.</p>';
      return;
    }
    detailState.season = seasons[0].season;
    renderEpisodes();
  } catch (e) {
    area.innerHTML = '<div class="section-label">Episodes</div><p style="color:#b3b3b3;margin-top:10px">Could not load episodes: ' + esc(e.message) + "</p>";
  }
}
function renderEpisodes() {
  const area = document.getElementById("epArea");
  const seasons = detailState.seasons;
  const cur = seasons.find((s) => s.season === detailState.season) || seasons[0];
  detailState.season = cur.season;
  let html = '<div class="section-label">Season</div>';
  if (seasons.length > 1) {
    html += '<select class="season-select" id="seasonSel">' +
      seasons.map((s) => '<option value="' + s.season + '"' + (s.season === cur.season ? " selected" : "") +
        ">Season " + s.season + "</option>").join("") + "</select>";
  } else {
    html += '<p style="margin-top:8px">Season ' + cur.season + "</p>";
  }
  html += '<div class="section-label">Episodes</div><div class="ep-grid">';
  for (let ep = 1; ep <= (cur.episode_count || 0); ep++) {
    html += '<button class="ep-btn" data-ep="' + ep + '">' + ep + "</button>";
  }
  html += "</div>";
  area.innerHTML = html;
  const sel = document.getElementById("seasonSel");
  if (sel) sel.addEventListener("change", () => { detailState.season = parseInt(sel.value, 10); renderEpisodes(); });
  area.querySelector(".ep-grid").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-ep]");
    if (!btn) return;
    const id = detailState.detail.subject_id;
    location.hash = "#/watch/" + enc(id) + "?se=" + detailState.season + "&ep=" + btn.dataset.ep +
      (detailState.playId !== String(id) ? "&play=" + enc(detailState.playId) : "");
  });
}

/* ---------------- Watch ---------------- */
let dashPlayer = null;
let watchCleanup = null;
function teardownWatch() {
  if (dashPlayer) { try { dashPlayer.destroy(); } catch (e) {} dashPlayer = null; }
  if (watchCleanup) { try { watchCleanup(); } catch (e) {} watchCleanup = null; }
}
async function viewWatch(id, q) {
  setNav("");
  teardownWatch();
  const se = parseInt(q.get("se") || "0", 10) || 0;
  const ep = parseInt(q.get("ep") || "0", 10) || 0;
  const playId = q.get("play") || id;
  loadingView("Finding the best source...");
  let streamData, capData, detail;
  try {
    [streamData, capData, detail] = await Promise.all([
      jget("/api/stream/" + enc(playId) + "?se=" + se + "&ep=" + ep),
      jget("/api/stream/" + enc(playId) + "/captions?se=" + se + "&ep=" + ep).catch(() => ({ captions: [] })),
      jget("/detail/" + enc(id)).catch(() => null),
    ]);
  } catch (e) {
    errorView("Could not load streams", e.message, "#/title/" + enc(id));
    return;
  }
  const streams = streamData.streams || [];
  const title = (detail && detail.title) || "TNxFlix";
  const cover = detail && detail.cover;
  if (!streams.length) {
    app.innerHTML = '<div class="error-box"><h2>No playable source</h2>' +
      "<p>The server returned no streams for this title right now. Please try again later.</p>" +
      '<a class="btn" href="#/title/' + enc(id) + '">Back to title</a></div>';
    return;
  }

  app.innerHTML =
    '<div class="watch-wrap">' +
    '<div class="player-box">' +
    '<video id="player" controls playsinline crossorigin="anonymous" poster="' + esc(cover || "") + '"></video>' +
    '<div class="player-overlay" id="playerOverlay"><div class="spinner"></div><p id="overlayText">Loading video...</p></div>' +
    "</div>" +
    '<h1 class="watch-title">' + esc(title) + "</h1>" +
    '<div class="watch-sub">' + (ep ? "Season " + se + " · Episode " + ep : "Movie") + "</div>" +
    '<div class="source-bar"><span class="lbl">Quality / Source:</span><span id="sourceChips"></span></div>' +
    '<div class="source-bar"><span class="lbl">Subtitles:</span><span id="subChips"></span></div>' +
    '<p><a class="btn secondary small" href="#/title/' + enc(id) + '">← Back to details</a></p>' +
    "</div>";

  const video = document.getElementById("player");
  const overlay = document.getElementById("playerOverlay");
  const overlayText = document.getElementById("overlayText");
  const chipsBox = document.getElementById("sourceChips");
  const subBox = document.getElementById("subChips");
  const posKey = progressKey(id, se, ep);
  let currentIdx = -1;
  let started = false;
  let stallTimer = null;

  function showOverlay(html) { overlay.innerHTML = html; overlay.classList.remove("hidden"); }
  function hideOverlay() { overlay.classList.add("hidden"); }
  function sourceLabel(s) {
    return s.quality + " · " + s.format + (s.size_mb ? " · " + s.size_mb + " MB" : "");
  }
  function renderChips() {
    chipsBox.innerHTML = streams.map((s, i) =>
      '<button class="chip' + (i === currentIdx ? " active" : "") + '" data-src="' + i + '">' +
      esc(sourceLabel(s)) + "</button>").join(" ");
  }
  function failCurrent(reason) {
    const next = streams[currentIdx + 1];
    showOverlay("<h3>Playback problem</h3><p>" + esc(reason || "This source could not be played.") + "</p>" +
      (next
        ? '<button class="btn" id="nextSrcBtn">Try next source (' + esc(sourceLabel(next)) + ")</button>"
        : '<a class="btn" href="#/title/' + enc(id) + '">Back to title</a>'));
    const b = document.getElementById("nextSrcBtn");
    if (b) b.addEventListener("click", () => loadSource(currentIdx + 1, video.currentTime || 0));
  }
  function armStallWatch() {
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      if (!started && video.readyState < 2) {
        showOverlay("<h3>Still loading...</h3><p>The video is taking unusually long. " +
          "The source may be slow or temporarily blocked.</p>" +
          '<button class="btn" id="retryBtn">Retry this source</button> ' +
          (streams[currentIdx + 1] ? '<button class="btn secondary" id="nextSrcBtn2">Try next source</button>' : ""));
        const r = document.getElementById("retryBtn");
        if (r) r.addEventListener("click", () => loadSource(currentIdx, video.currentTime || 0));
        const n = document.getElementById("nextSrcBtn2");
        if (n) n.addEventListener("click", () => loadSource(currentIdx + 1, 0));
      }
    }, 30000);
  }
  function loadSource(idx, resumeAt) {
    if (idx < 0 || idx >= streams.length) return;
    currentIdx = idx;
    started = false;
    renderChips();
    const s = streams[idx];
    showOverlay('<div class="spinner"></div><p>Loading ' + esc(sourceLabel(s)) + "...</p>");
    if (dashPlayer) { try { dashPlayer.destroy(); } catch (e) {} dashPlayer = null; }
    video.removeAttribute("src");
    video.querySelectorAll("track").forEach((t) => t.remove());
    // subtitles (native tracks; also picked up by dash.js when present)
    (capData.captions || []).forEach((c, i) => {
      const tr = document.createElement("track");
      tr.kind = "subtitles";
      tr.label = c.language || ("Subtitle " + (i + 1));
      tr.srclang = (c.code || "en").slice(0, 2);
      tr.src = apiUrl("/sub?url=" + enc(c.url));
      if (i === 0) tr.default = true;
      video.appendChild(tr);
    });
    const onMeta = () => {
      const target = resumeAt || parseFloat(localStorage.getItem(posKey) || "0");
      if (target > 5 && isFinite(video.duration) && target < video.duration - 10) {
        try { video.currentTime = target; } catch (e) {}
        toast("Resumed from " + fmtTime(target));
      }
    };
    video.addEventListener("loadedmetadata", onMeta, { once: true });
    if (s.format === "DASH" || /\.mpd($|\?)/i.test(s.url)) {
      if (window.dashjs) {
        dashPlayer = dashjs.MediaPlayer().create();
        dashPlayer.initialize(video, apiUrl("/dash?url=" + enc(s.url) + (s.cookie ? "&cookie=" + enc(s.cookie) : "")), true);
        dashPlayer.on(dashjs.MediaPlayer.events.ERROR, (e) => {
          if (!started) failCurrent("DASH playback error" + (e && e.error && e.error.message ? ": " + e.error.message : "."));
        });
      } else {
        failCurrent("DASH player library failed to load (CDN unreachable). MP4 sources, if any, will work.");
        return;
      }
    } else {
      video.src = apiUrl("/proxy?url=" + enc(s.url));
      video.load();
      video.play().catch(() => {});
    }
    armStallWatch();
  }

  chipsBox.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-src]");
    if (btn) loadSource(parseInt(btn.dataset.src, 10), video.currentTime || 0);
  });
  const captions = capData.captions || [];
  subBox.innerHTML = captions.length
    ? captions.map((c) => '<span class="chip">' + esc(c.language || "Subtitle") + "</span>").join(" ") +
      '<span style="color:#b3b3b3;font-size:13px"> (toggle CC in the player controls)</span>'
    : '<span style="color:#b3b3b3;font-size:13.5px">No subtitles available</span>';

  video.addEventListener("playing", () => {
    started = true;
    hideOverlay();
    clearTimeout(stallTimer);
    cwUpsert({ id: String(id), playId: String(playId), title: title, cover: cover,
               se: se, ep: ep, pos: video.currentTime || 0, dur: video.duration || 0, ts: Date.now() });
  });
  video.addEventListener("waiting", () => { if (!started) return; });
  video.addEventListener("error", () => {
    if (!started) failCurrent("The player reported an error for this source.");
  });
  let saveTick = 0;
  video.addEventListener("timeupdate", () => {
    if (!video.duration) return;
    try { localStorage.setItem(posKey, String(video.currentTime)); } catch (e) {}
    if (++saveTick % 4 === 0) {
      cwUpsert({ id: String(id), playId: String(playId), title: title, cover: cover,
                 se: se, ep: ep, pos: video.currentTime, dur: video.duration, ts: Date.now() });
    }
  });
  video.addEventListener("ended", () => {
    try { localStorage.removeItem(posKey); } catch (e) {}
    cwRemove(String(id), se, ep);
  });
  watchCleanup = () => {
    clearTimeout(stallTimer);
    try { localStorage.setItem(posKey, String(video.currentTime || 0)); } catch (e) {}
  };

  // Default source: prefer an MP4 at <=720p for a fast start, else the
  // first MP4, else the first stream.
  let def = streams.findIndex((s) => s.format !== "DASH" && (s.quality_value || 0) > 0 && s.quality_value <= 720);
  if (def < 0) def = streams.findIndex((s) => s.format !== "DASH");
  if (def < 0) def = 0;
  loadSource(def, 0);
}

/* ---------------- Router / chrome ---------------- */
function setNav(name) {
  document.querySelectorAll(".main-nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === name));
}
function route() {
  teardownWatch();
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const seg = pathPart.split("/").filter(Boolean);
  const q = new URLSearchParams(queryPart || "");
  window.scrollTo(0, 0);
  if (!seg.length) return viewHome();
  if (seg[0] === "movies") return viewGrid("movies");
  if (seg[0] === "tv") return viewGrid("tv");
  if (seg[0] === "search") return viewSearch(q.get("q") || "");
  if (seg[0] === "title" && seg[1]) return viewDetail(seg[1]);
  if (seg[0] === "watch" && seg[1]) return viewWatch(seg[1], q);
  return viewHome();
}
window.addEventListener("hashchange", route);

/* search box + suggestions */
const searchInput = document.getElementById("searchInput");
const suggestBox = document.getElementById("suggestBox");
let suggestTimer = null;
searchInput.addEventListener("input", () => {
  const val = searchInput.value.trim();
  clearTimeout(suggestTimer);
  if (val.length < 2) { suggestBox.classList.add("hidden"); return; }
  suggestTimer = setTimeout(async () => {
    try {
      const data = await jget("/search/suggest?q=" + enc(val));
      const items = (data.suggestions || []).filter((s) => s.title);
      if (!items.length) { suggestBox.classList.add("hidden"); return; }
      suggestBox.innerHTML = items.slice(0, 8).map((s) =>
        '<button data-id="' + esc(s.subject_id || "") + '" data-title="' + esc(s.title) + '">' +
        esc(s.title) + "</button>").join("");
      suggestBox.classList.remove("hidden");
    } catch (e) { suggestBox.classList.add("hidden"); }
  }, 300);
});
suggestBox.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  suggestBox.classList.add("hidden");
  if (btn.dataset.id && btn.dataset.id !== "None") location.hash = "#/title/" + enc(btn.dataset.id);
  else location.hash = "#/search?q=" + enc(btn.dataset.title);
});
searchInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    const val = searchInput.value.trim();
    if (val) { suggestBox.classList.add("hidden"); location.hash = "#/search?q=" + enc(val); }
  }
});
document.addEventListener("click", (ev) => {
  if (!ev.target.closest(".search-box")) suggestBox.classList.add("hidden");
});

route();
