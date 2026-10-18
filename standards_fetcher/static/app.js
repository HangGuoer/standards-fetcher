(() => {
  "use strict";

  const token = document.querySelector('meta[name="app-token"]').content;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const state = {
    config: null,
    records: [],
    selection: new Set(),
    query: "",
    discoveryMeta: null,
    typeFilter: "all",
    jobId: null,
    jobTimer: null,
    lastJob: null,
  };

  const els = {
    snapshot: $("#snapshot"),
    search: $("#searchInput"),
    searchGo: $("#searchGoBtn"),
    folder: $("#folderInput"),
    choose: $("#chooseBtn"),
    selectAll: $("#selectAllBtn"),
    clear: $("#clearBtn"),
    selectionCount: $("#selectionCount"),
    download: $("#downloadBtn"),
    resultCount: $("#resultCount"),
    resultSub: $("#resultSub"),
    typeBar: $("#typeBar"),
    typeChips: $("#typeChips"),
    sortHint: $("#sortHint"),
    tableBody: $("#tableBody"),
    empty: $("#emptyState"),
    checkAll: $("#checkAll"),
    refresh: $("#refreshBtn"),
    quit: $("#quitBtn"),
    jobPanel: $("#jobPanel"),
    jobTitle: $("#jobTitle"),
    jobStatus: $("#jobStatus"),
    jobProgress: $("#jobProgress"),
    jobMeta: $("#jobMeta"),
    jobItems: $("#jobItems"),
    jobClose: $("#jobClose"),
    jobCancel: $("#jobCancel"),
    reveal: $("#revealBtn"),
    toastWrap: $("#toastWrap"),
    footerMeta: $("#footerMeta"),
  };

  const ICONS = {
    pdf: '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M4 2h5l3 3v9H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M9 2v3h3M6 9h4M6 11.5h3" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
    lock: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><rect x="3.2" y="7" width="9.6" height="7" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5.4 7V5.3a2.6 2.6 0 0 1 5.2 0V7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ "X-App-Token": token }, options.headers || {});
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, Object.assign({}, options, { headers }));
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { /* ignore */ }
    if (!response.ok || (payload && payload.ok === false)) {
      throw new Error((payload && payload.error) || `请求失败（${response.status}）`);
    }
    return payload;
  }

  function toast(message, kind = "") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`.trim();
    node.textContent = message;
    els.toastWrap.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity .25s";
      setTimeout(() => node.remove(), 260);
    }, 3600);
  }

  const formatCount = (value) => Number(value || 0).toLocaleString("zh-CN");
  const formatDate = (value) => (value ? value.replaceAll("-", ".") : "—");

  function isDownloadable(record) {
    return Boolean(record.pdf_available || (record.amendment && record.amendment.pdf_available));
  }

  function statusBadges(record) {
    const badges = [];
    if (record.status === "upcoming") badges.push('<span class="badge upcoming">即将实施</span>');
    else badges.push('<span class="badge current">现行</span>');
    if (record.has_amendment) badges.push('<span class="badge amended">含局部修订</span>');
    return badges.join(" ");
  }

  function fileState(record) {
    if (record.pdf_available) return `<span class="file-state">${ICONS.pdf} PDF 可下载</span>`;
    if (record.amendment && record.amendment.pdf_available) {
      return `<span class="file-state" style="color:var(--amber)">${ICONS.pdf} 仅修订条文</span>`;
    }
    return `<span class="file-state no">${ICONS.lock} 未公开</span>`;
  }

  function secondaryLine(record) {
    const parts = [];
    if (record.has_amendment && record.amendment) {
      parts.push(`含 ${escapeHtml(record.amendment.label || "局部修订")}`);
    }
    if (record.supersedes && record.supersedes.length) {
      parts.push(`替代 ${escapeHtml(record.supersedes.join("、"))}`);
    }
    if (record.hidden_versions) {
      parts.push(`已合并 ${record.hidden_versions} 个旧版`);
    }
    if (record.source_count > 1 && record.source_labels && record.source_labels.length) {
      parts.push(`来源：${record.source_labels.join(" · ")}`);
    } else if (record.source !== "mohurd" && record.source_labels && record.source_labels.length) {
      parts.push(`来源：${record.source_labels[0]}`);
    }
    return parts.length ? `<div class="standard-sub">${parts.join(" · ")}</div>` : "";
  }

  function renderConfig() {
    const config = state.config;
    if (!config) return;
    els.snapshot.textContent = `数据快照 ${config.snapshot || "—"}`;
    els.footerMeta.textContent = `${config.app_name} v${config.version} · 数据截至 ${config.snapshot || "—"}`;
    if (!els.folder.value) els.folder.value = config.out_dir || "";
    if (!state.query && !state.records.length) {
      els.resultSub.textContent = `在 ${formatCount(config.index_count)} 条住建部标准公告中检索。`;
    }
  }

  const TYPE_ORDER = ["GB", "GB/T", "GB/Z", "JGJ", "JGJ/T", "CJJ", "CJJ/T", "JG/T", "DB", "DG", "团体", "其他"];

  function filteredRecords() {
    if (state.typeFilter === "all") return state.records;
    return state.records.filter((record) => (record.code_type || "其他") === state.typeFilter);
  }

  function renderTypeFilters() {
    if (!state.records.length) {
      els.typeBar.classList.add("hidden");
      els.typeChips.innerHTML = "";
      return;
    }
    els.typeBar.classList.remove("hidden");
    const counts = new Map();
    for (const record of state.records) {
      const type = record.code_type || "其他";
      counts.set(type, (counts.get(type) || 0) + 1);
    }
    if (state.typeFilter !== "all" && !counts.has(state.typeFilter)) {
      state.typeFilter = "all";
    }
    const types = Array.from(counts.keys()).sort((a, b) => {
      const ai = TYPE_ORDER.indexOf(a);
      const bi = TYPE_ORDER.indexOf(b);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
    const chips = [
      `<button class="type-chip${state.typeFilter === "all" ? " active" : ""}" data-type="all">全部 <span class="badge">${state.records.length}</span></button>`,
    ];
    for (const type of types) {
      const label = (state.records.find((record) => (record.code_type || "其他") === type) || {}).code_label || type;
      chips.push(`<button class="type-chip${state.typeFilter === type ? " active" : ""}" data-type="${escapeHtml(type)}" title="${escapeHtml(label)}">${escapeHtml(type)} <span class="badge">${counts.get(type)}</span></button>`);
    }
    els.typeChips.innerHTML = chips.join("");
    $$(".type-chip", els.typeChips).forEach((button) => {
      button.addEventListener("click", () => {
        state.typeFilter = button.dataset.type || "all";
        state.selection.clear();
        renderTable();
      });
    });
  }

  function updateSelectionUI() {
    const count = state.selection.size;
    els.selectionCount.textContent = `已选 ${count} 项`;
    els.download.disabled = count === 0;
    els.download.innerHTML = count
      ? `下载选中文件（${count}）`
      : "下载选中文件";
    const downloadable = filteredRecords().filter(isDownloadable);
    const allSelected = downloadable.length > 0 && downloadable.every((record) => state.selection.has(record.id));
    els.checkAll.checked = allSelected;
    els.checkAll.indeterminate = count > 0 && !allSelected;
  }

  function renderTable() {
    const records = filteredRecords();
    renderTypeFilters();
    els.resultCount.textContent = records.length
      ? formatCount(records.length)
      : (state.records.length ? "0" : "—");

    if (state.discoveryMeta) {
      const meta = state.discoveryMeta;
      if (els.sortHint) els.sortHint.textContent = `可下载 ${formatCount(records.filter(isDownloadable).length)} 项优先`;
      const counts = meta.source_counts || {};
      const parts = [];
      if (counts.mohurd) parts.push(`住建部 ${formatCount(counts.mohurd)}`);
      if (counts.samr) parts.push(`国标平台 ${formatCount(counts.samr)}`);
      if (counts.ccsn) parts.push(`工程标准网 ${formatCount(counts.ccsn)}`);
      const sourceText = parts.length ? `${parts.join(" · ")} → ` : "";
      const filterText = state.typeFilter === "all" ? "" : ` · 筛选 ${state.typeFilter}`;
      const shownText = meta.shown && meta.shown < meta.groups ? `，展示前 ${formatCount(meta.shown)} 项` : "";
      els.resultSub.textContent = `“${meta.query || state.query}”：${sourceText}解析 ${formatCount(meta.resolved)} 条，合并 ${formatCount(meta.groups)} 项${shownText}${filterText}${meta.truncated ? "（来源较多，仅解析部分）" : ""}`;
    } else if (state.query) {
      els.resultSub.textContent = `正在检索“${state.query}”…`;
    } else {
      if (els.sortHint) els.sortHint.textContent = "可下载优先";
      els.resultSub.textContent = state.config
        ? ``
        : "输入关键词，检索住建部标准公告。";
    }

    els.empty.classList.toggle("hidden", records.length > 0);
    const emptyTitle = els.empty.querySelector("h4");
    const emptyText = els.empty.querySelector("p");
    if (emptyTitle && emptyText) {
      if (state.records.length && !records.length) {
        emptyTitle.textContent = "该类型下没有结果";
        emptyText.textContent = "切换到“全部”或选择其他标准类型。";
      } else if (state.query && state.discoveryMeta) {
        emptyTitle.textContent = "没有找到相关公告";
        emptyText.textContent = "试试更短的核心词，或换用规范名称中的关键词。";
      } else if (state.query) {
        emptyTitle.textContent = "正在检索…";
        emptyText.textContent = "正在解析公告正文并合并同名版本，请稍候。";
      } else {
        emptyTitle.textContent = "检索全部标准公告";
      }
    }

    els.tableBody.innerHTML = records.map((record) => {
      const checked = state.selection.has(record.id);
      const downloadable = isDownloadable(record);
      const disabled = downloadable ? "" : "disabled";
      const selectedClass = checked ? " selected" : "";
      const sourceUrl = record.announcement_url || record.official_url || "";
      const actions = [];
      if (downloadable) {
        actions.push(`<button class="btn link" data-download="${escapeHtml(record.id)}">下载</button>`);
      }
      if (sourceUrl) {
        actions.push(`<button class="btn link" data-open="${escapeHtml(sourceUrl)}">公告</button>`);
      }
      if (!downloadable && record.purchase_url) {
        actions.push(`<button class="btn link muted" data-open="${escapeHtml(record.purchase_url)}">购买/阅读</button>`);
      }
      return `<tr class="${selectedClass}" data-id="${escapeHtml(record.id)}">
        <td class="check-col"><input type="checkbox" ${checked ? "checked" : ""} ${disabled} aria-label="选择 ${escapeHtml(record.name)}" data-check="${escapeHtml(record.id)}"></td>
        <td><div class="standard-no">${escapeHtml(record.number)}</div></td>
        <td>
          <div class="standard-name"><button data-open="${escapeHtml(sourceUrl || detailUrl)}">${escapeHtml(record.name)}</button></div>
          ${secondaryLine(record)}
        </td>
        <td><span class="date-text">${formatDate(record.impl_date)}</span></td>
        <td>${statusBadges(record)}</td>
        <td>${fileState(record)}</td>
        <td><div class="row-actions">${actions.join("")}</div></td>
      </tr>`;
    }).join("");

    $$("[data-check]", els.tableBody).forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const id = checkbox.dataset.check;
        if (checkbox.checked) state.selection.add(id);
        else state.selection.delete(id);
        checkbox.closest("tr")?.classList.toggle("selected", checkbox.checked);
        updateSelectionUI();
      });
    });
    $$("[data-download]", els.tableBody).forEach((button) => {
      button.addEventListener("click", () => {
        state.selection.add(button.dataset.download);
        updateSelectionUI();
        startDownload();
      });
    });
    $$("[data-open]", els.tableBody).forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.open) openUrl(button.dataset.open);
      });
    });
    updateSelectionUI();
  }

  async function openUrl(url) {
    if (!url) return;
    try {
      await api("/api/open", { method: "POST", body: JSON.stringify({ url }) });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function runDiscovery() {
    const query = els.search.value.trim();
    if (!query) {
      toast("请输入检索关键词");
      els.search.focus();
      return;
    }
    state.query = query;
    state.selection.clear();
    state.discoveryMeta = null;
    state.typeFilter = "all";
    state.records = [];
    renderTable();
    try {
      const payload = await api("/api/discover", {
        method: "POST",
        body: JSON.stringify({ q: query, limit: 60 }),
      });
      showJob(payload.job_id, "discover");
    } catch (error) {
      toast(error.message, "error");
      els.resultSub.textContent = "检索失败，请重试";
    }
  }

  async function loadDiscovered() {
    try {
      const payload = await api("/api/discovered");
      state.records = payload.items || [];
      state.discoveryMeta = payload.meta || null;
      state.typeFilter = "all";
      state.selection.clear();
      renderTable();
      if (state.discoveryMeta) {
        toast(`三站命中 ${state.discoveryMeta.total_hits} 条记录，合并为 ${state.discoveryMeta.groups} 项标准`, "success");
      }
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function startDownload() {
    if (!state.selection.size) {
      toast("请先选择要下载的标准");
      return;
    }
    const outDir = els.folder.value.trim();
    if (!outDir) {
      toast("请先选择保存文件夹");
      return;
    }
    els.download.disabled = true;
    try {
      const payload = await api("/api/download", {
        method: "POST",
        body: JSON.stringify({ ids: Array.from(state.selection), out_dir: outDir }),
      });
      showJob(payload.job_id, "download");
    } catch (error) {
      toast(error.message, "error");
      updateSelectionUI();
    }
  }

  function jobIcon(status) {
    const map = { done: "✓", skipped: "✓", error: "!", unavailable: "–", running: "•", pending: "·", cancelled: "×" };
    return `<span class="job-icon ${status}">${map[status] || "·"}</span>`;
  }

  function renderJob(job) {
    state.lastJob = job;
    els.jobPanel.classList.remove("hidden");
    els.jobTitle.textContent = job.kind === "refresh"
      ? "更新公告库"
      : job.kind === "discover"
        ? "检索全部标准公告"
        : "下载标准 PDF";
    els.jobStatus.textContent = job.message || "";
    els.jobProgress.style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
    const total = (job.items || []).length;
    els.jobMeta.textContent = job.kind === "download"
      ? `${job.result && job.result.total ? job.result.total : total} 个文件 · ${Math.round(job.progress || 0)}%`
      : job.kind === "discover"
        ? `${Math.round(job.progress || 0)}% · ${job.message || "正在解析公告"}`
        : `${Math.round(job.progress || 0)}%`;
    els.jobItems.innerHTML = (job.items || []).map((item) => {
      const title = item.number ? `${item.number} ${item.name}` : item.name;
      return `<div class="job-item">${jobIcon(item.status)}<div class="job-text"><strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong><span>${escapeHtml(item.message || "")}</span></div></div>`;
    }).join("");
    const terminal = ["done", "error", "cancelled"].includes(job.status);
    els.jobCancel.classList.toggle("hidden", terminal || job.kind !== "download");
    els.reveal.disabled = !(job.status === "done" && job.kind === "download");
    if (terminal) {
      if (job.status === "done") {
        if (job.kind === "refresh") toast(job.message || "公告库已更新", "success");
        else if (job.kind === "discover") toast(job.message || "检索完成", "success");
        else toast(job.message || "下载完成", "success");
      } else if (job.status === "error") {
        toast(job.error || "任务失败", "error");
      }
    }
  }

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const payload = await api(`/api/jobs/${state.jobId}`);
      const job = payload.job;
      renderJob(job);
      if (["done", "error", "cancelled"].includes(job.status)) {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        if (job.kind === "discover" && job.status === "done") await loadDiscovered();
        if (job.kind === "refresh" && job.status === "done") await loadConfig();
        if (job.kind === "download") updateSelectionUI();
        return;
      }
    } catch (error) {
      clearInterval(state.jobTimer);
      state.jobTimer = null;
      toast(error.message, "error");
    }
  }

  function showJob(jobId, kind) {
    state.jobId = jobId;
    els.jobPanel.classList.remove("hidden");
    els.jobTitle.textContent = kind === "refresh" ? "更新公告库" : kind === "discover" ? "检索全部标准公告" : "下载标准 PDF";
    els.jobStatus.textContent = "正在启动…";
    els.jobProgress.style.width = "0%";
    els.jobMeta.textContent = "";
    els.jobItems.innerHTML = "";
    els.reveal.disabled = true;
    els.jobCancel.classList.toggle("hidden", kind !== "download");
    if (state.jobTimer) clearInterval(state.jobTimer);
    state.jobTimer = setInterval(pollJob, 650);
    pollJob();
  }

  async function loadConfig() {
    state.config = await api("/api/config");
    renderConfig();
  }

  async function chooseFolder() {
    els.choose.disabled = true;
    try {
      const payload = await api("/api/choose-folder", { method: "POST", body: "{}" });
      if (payload.path) {
        els.folder.value = payload.path;
        if (state.config) state.config.out_dir = payload.path;
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      els.choose.disabled = false;
    }
  }

  async function refreshIndex() {
    if (state.lastJob && !["done", "error", "cancelled"].includes(state.lastJob.status)) {
      toast("已有任务在进行中");
      return;
    }
    els.refresh.disabled = true;
    try {
      const payload = await api("/api/refresh", { method: "POST", body: JSON.stringify({ full: false }) });
      showJob(payload.job_id, "refresh");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setTimeout(() => { els.refresh.disabled = false; }, 1200);
    }
  }

  function bindEvents() {
    els.search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") runDiscovery();
    });
    els.searchGo.addEventListener("click", runDiscovery);
    els.choose.addEventListener("click", chooseFolder);
    els.folder.addEventListener("change", () => {
      if (state.config) state.config.out_dir = els.folder.value;
    });
    els.selectAll.addEventListener("click", () => {
      filteredRecords().filter(isDownloadable).forEach((record) => state.selection.add(record.id));
      renderTable();
    });
    els.clear.addEventListener("click", () => {
      state.selection.clear();
      renderTable();
    });
    els.checkAll.addEventListener("change", () => {
      if (els.checkAll.checked) filteredRecords().filter(isDownloadable).forEach((record) => state.selection.add(record.id));
      else filteredRecords().filter(isDownloadable).forEach((record) => state.selection.delete(record.id));
      renderTable();
    });
    els.download.addEventListener("click", startDownload);
    els.refresh.addEventListener("click", refreshIndex);
    els.quit.addEventListener("click", async () => {
      if (!confirm("退出后本地服务会关闭，已下载的文件不受影响。确定退出吗？")) return;
      try { await api("/api/quit", { method: "POST", body: "{}" }); } catch { /* ignore */ }
      document.body.innerHTML = '<div style="min-height:100vh;display:grid;place-items:center;font-family:-apple-system,sans-serif;color:#556;background:#f5f7fb"><div style="text-align:center"><h2 style="color:#172033">标准规范下载器已退出</h2><p>可以直接关闭此页面。</p></div></div>';
    });
    els.jobClose.addEventListener("click", () => {
      const running = state.lastJob && !["done", "error", "cancelled"].includes(state.lastJob.status);
      if (running) {
        toast("任务进行中，请稍候");
        return;
      }
      els.jobPanel.classList.add("hidden");
    });
    els.jobCancel.addEventListener("click", async () => {
      if (!state.jobId) return;
      try {
        await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST", body: "{}" });
        toast("已请求取消");
      } catch (error) {
        toast(error.message, "error");
      }
    });
    els.reveal.addEventListener("click", async () => {
      try {
        const jobOut = state.lastJob && state.lastJob.result && state.lastJob.result.out_dir;
        const path = jobOut || els.folder.value.trim() || (state.config && state.config.out_dir);
        await api("/api/reveal", { method: "POST", body: JSON.stringify({ path }) });
      } catch (error) {
        toast(error.message, "error");
      }
    });
  }

  async function init() {
    bindEvents();
    try {
      await loadConfig();
      renderTable();
      els.search.focus();
    } catch (error) {
      toast(`启动失败：${error.message}`, "error");
    }
  }

  init();
})();
