"use strict";

const uiModel = globalThis.BranchRadarUIModel || Object.freeze({
  SIZE_THRESHOLDS: Object.freeze({ medium: 200, large: 2000 }),
  classifyChangeSize: () => ({ key: "small", label: "小", level: 1, total: 0, ratio: 0 }),
  buildSignalFieldModel: (events) => {
    const values = Array.isArray(events) ? events : [];
    const normalized = values.map((event, index) => ({
      event,
      id: event.id,
      position: 1,
      slot: 0,
      fallbackIndex: index,
    }));
    const nowMs = Date.now();
    return {
      startMs: nowMs - 86_400_000,
      endMs: nowMs,
      rangeHours: 24,
      hiddenCount: 0,
      lanes: normalized.map((item) => ({
        key: `fallback-${item.fallbackIndex}`,
        repository: item.event.repository,
        branch: item.event.branch,
        slots: 1,
        events: [item],
      })),
      events: normalized,
      scanBands: [],
    };
  },
  buildBranchCanvasModel: (events) => {
    const normalized = (Array.isArray(events) ? events : []).map((event, index) => ({
      event,
      id: Number(event?.id) || index + 1,
      repositoryKey: `repository:${event?.repository || ""}`,
      laneKey: `branch:${event?.branch || ""}`,
      repository: event?.repository || "",
      branch: event?.branch || "",
      timeMs: Date.parse(event?.detected_at || "") || null,
      x: 120 + (index * 120),
      y: 110 + (index * 92),
      metadata: { authors: event?.authors || [], categories: [], signals: [], stats: event?.stats || {}, size: { key: "small", total: 0 } },
      topology: {},
    }));
    return {
      startMs: Date.now() - 86_400_000,
      endMs: Date.now(),
      rangeHours: 24,
      hiddenCount: 0,
      repositories: [],
      lanes: normalized.map((item) => ({ ...item, key: item.laneKey, events: [item], authorSegments: [] })),
      events: normalized,
      edges: [],
      ambiguousEdgeCount: 0,
      latestX: Math.max(900, 120 + (normalized.length * 120)),
      bounds: { x: 0, y: 0, width: Math.max(1024, 280 + (normalized.length * 120)), height: Math.max(500, 220 + (normalized.length * 92)) },
    };
  },
  clampCanvasZoom: (value) => Math.max(0.08, Math.min(4, Number(value) || 1)),
  canvasLod: (value) => Number(value) < 0.62 ? "overview" : Number(value) < 1.28 ? "authors" : "details",
  zoomAroundPoint: (transform, zoom) => ({ x: transform?.x || 0, y: transform?.y || 0, zoom: Number(zoom) || 1 }),
  fitCanvasTransform: () => ({ x: 24, y: 24, zoom: 1 }),
  latestCanvasTransform: () => ({ x: 24, y: 24, zoom: 1 }),
  normalizeEventMetadata: () => ({ authors: [], categories: [], signals: [], stats: {}, size: { key: "small", label: "小", total: 0, ratio: 0 } }),
  normalizeCheckRunSummary: () => ({ id: null, isPending: false, counts: {}, publication: { truncated: false, backlog: { succeeded: 0, failed: 0, unknown: 0 } }, eventIds: [], events: [], summary: null }),
  shouldHandleShortcut: () => false,
  moveEventSelection: () => null,
  moveListboxActiveIndex: () => null,
  prioritizeBranchNames: (branches) => Array.isArray(branches) ? branches.slice(0, 200) : [],
  isEventNewForRun: (event) => Boolean(event?.is_new),
  RECENT_EVENT_WINDOW_MS: 86_400_000,
  shortSha: (value) => (value ? String(value).slice(0, 10) : "—"),
  watchBranchId: (watch) => {
    const value = Number(watch?.id ?? watch?.branch_id);
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  },
  branchStatus: () => ["正常", ""],
  groupWatchStatus: () => ["正常", ""],
  groupWatches: (watches) => (Array.isArray(watches) ? watches : []).map((watch) => ({
    id: watch.repository_id ?? null,
    name: watch.repository || "未命名仓库",
    alias: watch.repository_alias || null,
    tags: [],
    watches: [watch],
  })),
  latestValue: () => null,
  latestTimestamp: (...values) => values.filter(Boolean)[0] || null,
  parseRepositoryTags: (value) => String(value ?? "").split(/[,，、]/)
    .map((item) => item.trim()).filter(Boolean),
  recentEventWatchKeys: () => new Set(),
  repositoryDisplayName: (group) => group?.alias || group?.name,
  repositoryGroupKey: (group) => (group?.id === null || group?.id === undefined
    ? `name:${group?.name}` : `id:${group?.id}`),
  repositoryNeedsAttention: () => false,
  watchAttentionReason: () => null,
  watchKey: (repository, branch) => `${repository}\u0000${branch}`,
});
const i18n = globalThis.BranchRadarI18n || Object.freeze({
  intlLocale: () => "zh-CN",
  locale: () => "zh-CN",
  registerDOM: () => {},
  t: (value) => String(value ?? ""),
  translateDOM: () => {},
});

function t(value, variables) {
  return i18n.t(value, variables);
}

function captureLaunchSession() {
  const launchFragment = new URLSearchParams(location.hash.replace(/^#/, ""));
  const launchToken = launchFragment.get("session");
  if (!launchToken) return false;
  sessionStorage.setItem("branch-radar-session", launchToken);
  history.replaceState(null, "", `${location.pathname}${location.search}#overview`);
  return true;
}

captureLaunchSession();
const csrfToken = sessionStorage.getItem("branch-radar-session") || "";
const state = {
  dashboard: null,
  integrations: null,
  monitoring: null,
  monitoringLoading: false,
  monitoringSaving: false,
  monitoringDirty: false,
  monitoringLoadedAt: 0,
  monitoringError: null,
  monitoringMessage: null,
  monitoringSubmissionJobId: null,
  changelog: null,
  changelogLoading: false,
  candidates: null,
  discoveryError: null,
  discoveryErrorIsRaw: false,
  discoveryErrorCode: null,
  scanRootsSaving: false,
  scanRootPickerPending: false,
  scanRootPickerNeedsReconcile: false,
  scanRootsDraft: [],
  selectedCandidate: null,
  localProjectPage: 1,
  localProjectPageSize: 8,
  matchedRemoteCanonical: null,
  branchQuery: "",
  activityScope: null,
  activityEvents: [],
  activityHasMore: false,
  activityNextBeforeId: null,
  activityRequest: 0,
  activityLoading: false,
  activityError: null,
  activityErrorCode: null,
  activityRangeHours: 24,
  selectedActivityEventId: null,
  signalSweepRunId: null,
  eventRequest: 0,
  currentEvent: null,
  recentlyAddedBranches: [],
  recentlyAddedWatchIds: new Set(),
  selectedWatchIds: new Set(),
  watchWorkspaceTab: "watched",
  selectedRepositoryTag: null,
  repositoryExpansion: {},
  visibleWatchIds: [],
  repositoryProfileId: null,
  pendingUnwatch: null,
  checkSubmission: null,
  watchSubmission: null,
  watchProgressPhase: null,
  pendingRemoteUrl: null,
  clonePreview: null,
  cloneSubmission: null,
  cloneTerminalJob: null,
  cloneProgressPhase: null,
  cloneTaskExpanded: false,
  cloneCancelling: false,
  cloneValidationErrorCode: null,
  activeJob: null,
  pollingTimer: null,
  pollGeneration: 0,
  pollFailures: 0,
  pollAttempts: 0,
  autoDiscoveryStarted: false,
  larkCreateRequestId: null,
  larkCreatePending: null,
  checkRunExpanded: false,
  checkRunAcknowledging: false,
  visibleCheckRunId: null,
  announcedCheckRunId: null,
  canvasModel: null,
  canvasModelSignature: null,
  canvasTransform: { x: 48, y: 30, zoom: 1 },
  canvasLod: "authors",
  canvasFocused: false,
  canvasSelectedAuthor: null,
  canvasSelectedEventId: null,
};

const WATCH_BUTTON_DEFAULT = "关注所选分支";
const WATCH_PHASE_ORDER = ["inspect", "fetch", "resolve", "save"];
const CLONE_PHASE_ORDER = ["prepare", "connect", "download", "verify", "complete"];
const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "cancelling"]);
const MAX_SCAN_ROOTS = 16;
const SCAN_ROOT_PICKER_JOB_STORAGE_KEY = "branch-radar-scan-root-picker-job";
const CLONE_JOB_STORAGE_KEY = "branch-radar-clone-job";
const THEME_STORAGE_KEY = "branch-radar-theme";
const REPOSITORY_EXPANSION_STORAGE_KEY = "branch-radar-repository-expansion";
const RAIL_COLLAPSED_STORAGE_KEY = "branch-radar-sidebar-collapsed";
const CANVAS_ZOOM_STEP = 1.24;
// The server holds a job request until the job actually moves, so progress
// arrives when it happens instead of on the client's next tick. The short delay
// only paces the loop if a reply comes back unchanged.
const JOB_WAIT_SECONDS = 25;
const JOB_REPOLL_DELAY_MS = 50;
const TOPBAR_COMPACT_ENTER_Y = 64;
const TOPBAR_COMPACT_EXIT_Y = 16;
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
let followsSystemTheme = true;
const localeFormatters = new Map();
const eventSearchTextCache = new WeakMap();
const scheduledRenders = new Map();
let groupedWatchSource = null;
let groupedWatchResult = [];
const repositoryDisclosureAnimations = new WeakMap();
const eventDetailCache = new Map();
let canvasDetailRequest = 0;
let canvasPanSession = null;
let canvasTransformFrame = null;
let canvasResizeObserver = null;
let canvasFocusReturn = null;
let canvasViewportSize = null;
let canvasViewMode = "latest";
// Collapsed repositories keep their branch rows as an unbuilt closure. A watch
// list is dominated by rows nobody has opened, and rebuilding them on every
// dashboard refresh costs far more than it shows.
const pendingBranchRows = new WeakMap();
let stickyTopbarObserver = null;
let stickyTopbarCompact = false;
let stickyTopbarObservedHeight = null;

function loadRepositoryExpansion() {
  try {
    const value = JSON.parse(localStorage.getItem(REPOSITORY_EXPANSION_STORAGE_KEY) || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value).filter(([key, expanded]) => key.length <= 180 && typeof expanded === "boolean").slice(-200),
    );
  } catch (_error) {
    return {};
  }
}

state.repositoryExpansion = loadRepositoryExpansion();

function loadRailCollapsed() {
  try {
    return localStorage.getItem(RAIL_COLLAPSED_STORAGE_KEY) === "true";
  } catch (_error) {
    return false;
  }
}

document.body.classList.toggle("rail-collapsed", loadRailCollapsed());

function syncRailCollapseControl() {
  const button = document.getElementById("rail-collapse-button");
  if (!button) return;
  const collapsed = document.body.classList.contains("rail-collapsed");
  button.setAttribute("aria-expanded", collapsed ? "false" : "true");
  button.setAttribute("aria-label", t(collapsed ? "展开侧栏" : "收起侧栏"));
}

function setRailCollapsed(collapsed) {
  document.body.classList.toggle("rail-collapsed", Boolean(collapsed));
  try {
    localStorage.setItem(RAIL_COLLAPSED_STORAGE_KEY, collapsed ? "true" : "false");
  } catch (_error) {
    // The UI state remains useful even when storage is unavailable.
  }
  syncRailCollapseControl();
}

try {
  followsSystemTheme = !["light", "dark"].includes(localStorage.getItem(THEME_STORAGE_KEY));
} catch (_error) {
  followsSystemTheme = true;
}

function resolvedTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function syncThemeControls(theme = resolvedTheme()) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  const themeColor = document.getElementById("theme-color-meta");
  if (themeColor) themeColor.content = dark ? "#0f1722" : "#f3f6fa";
  document.querySelectorAll("[data-theme-toggle]").forEach((control) => {
    control.setAttribute("aria-pressed", dark ? "true" : "false");
    control.setAttribute("aria-label", t(dark ? "切换到浅色模式" : "切换到深色模式"));
  });
  document.querySelectorAll("[data-theme-label]").forEach((label) => {
    label.textContent = t(dark ? "深色" : "浅色");
  });
}

function applyTheme(theme, { persist = false } = {}) {
  syncThemeControls(theme);
  if (!persist) return;
  followsSystemTheme = false;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (_error) {
    // The visible theme still works when local storage is unavailable.
  }
}

function toggleTheme(event) {
  const next = resolvedTheme() === "dark" ? "light" : "dark";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!document.startViewTransition || reduced) {
    applyTheme(next, { persist: true });
    return;
  }
  const bounds = event.currentTarget.getBoundingClientRect();
  const x = bounds.left + bounds.width / 2;
  const y = bounds.top + bounds.height / 2;
  const radius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );
  const transition = document.startViewTransition(() => {
    applyTheme(next, { persist: true });
  });
  transition.ready.then(() => {
    document.documentElement.animate(
      { clipPath: [`circle(0 at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
      {
        duration: 520,
        easing: "cubic-bezier(0.2, 0.75, 0.25, 1)",
        pseudoElement: "::view-transition-new(root)",
      },
    );
  }).catch(() => {});
}

function initializeThemeControls() {
  syncThemeControls();
  document.querySelectorAll("[data-theme-toggle]").forEach((control) => {
    control.addEventListener("click", toggleTheme);
  });
  themeMedia.addEventListener?.("change", (event) => {
    if (followsSystemTheme) applyTheme(event.matches ? "dark" : "light");
  });
}

try {
  state.larkCreatePending = JSON.parse(sessionStorage.getItem("branch-radar-lark-create") || "null");
  state.larkCreateRequestId = state.larkCreatePending?.requestId || null;
} catch (_error) {
  sessionStorage.removeItem("branch-radar-lark-create");
}

const viewCopy = {
  overview: ["BRANCH RADAR / INBOX", "分支动态"],
  projects: ["BRANCH RADAR / WATCHES", "关注分支"],
  changelog: ["BRANCH RADAR / RELEASES", "更新日志"],
  settings: ["BRANCH RADAR / SETTINGS", "设置"],
};

const jobCopy = {
  discover: ["正在扫描本地仓库", "读取 Git、remote 与当前分支，不会修改仓库"],
  pick_scan_root: ["等待选择本机文件夹", "请在系统窗口中选择一个扫描目录"],
  watch: ["正在建立分支基线", "获取远程引用并保存静默 baseline"],
  check: ["正在检查分支变化", "获取远程、分析差异并生成摘要"],
  publish: ["正在与飞书对账", "已有事件标记不会重复写入"],
  integrations: ["正在检查本机连接", "验证 Codex CLI 与 lark-cli 登录状态"],
  monitoring: ["正在应用监控设置", "更新本机后台服务与变化通知"],
  lark_connect: ["正在验证飞书文档", "确认当前用户具备读写条件"],
  lark_create: ["正在创建飞书文档", "请勿关闭页面或重复提交"],
  clone: ["正在克隆仓库", "从远程下载并验证本地 Git checkout"],
};

const kindCopy = {
  fast_forward: "普通前进",
  force_push: "强制推送",
  rewind: "分支回退",
  deleted: "分支删除",
  recreated: "重新出现",
  history_gap: "历史缺口",
  baseline: "建立基线",
};

const categoryCopy = {
  api: "API",
  database: "数据库",
  config: "配置",
  dependency: "依赖",
  deployment: "部署",
  test: "测试",
  docs: "文档",
};

const ATTENTION_EVENT_KINDS = new Set(["force_push", "rewind", "deleted", "recreated", "history_gap"]);
const MONITORING_INTERVALS = new Set([300, 900, 1800, 3600]);
const MONITORING_DEFAULT_INTERVAL = 900;
const MONITORING_ENDPOINT = "/api/v1/monitoring";

// One entry per error code the local service can return. The code is the
// contract; the wording belongs to the console, which is what lets an English
// reader get the actual reason instead of a generic failure. A code with no
// entry here is a gap, and tests/test_web.py fails on one.
const ERROR_COPY = {
  authentication_failed: "认证失败。请先在终端确认当前 Git 凭证可以访问该仓库。",
  check_run_not_found: "这批检查记录已不存在，请重新检查。",
  clone_authentication_failed: "认证失败。请先在终端确认当前 Git 凭证可以访问该仓库。",
  clone_cancelled: "已取消克隆；未完成的目标目录不会被保留。",
  clone_cleanup_failed: "克隆失败，且未能完全清理临时目录。请检查所选扫描目录中的 .branch-radar-clone-*.partial，确认无用后可手动清理。",
  clone_destination_exists: "目标目录已存在。请选择其他扫描目录或修改目录名。",
  clone_failed: "克隆失败。请检查网络连接以及当前 Git 凭证是否有权访问该仓库。",
  clone_parent_required: "请选择一个已配置的扫描目录。",
  clone_root_changed: "保存目录在克隆期间发生变化。任务已安全停止，请重新选择扫描目录。",
  clone_timeout: "克隆操作超时。请检查网络后重试。",
  clone_verification_failed: "仓库已下载，但无法验证为可用的 Git checkout。请更换目录后重试。",
  document_required: "请输入飞书文档链接或 token。",
  event_not_found: "这条变化记录已不存在，请返回列表重新选择。",
  folder_picker_failed: "无法打开系统目录选择器。请确认桌面会话可用，或展开手动输入目录路径。",
  folder_picker_timeout: "目录选择已超时。请重新打开选择器并完成选择。",
  folder_picker_unavailable: "当前会话没有可用的图形界面。请展开手动输入目录路径。",
  idempotency_conflict: "同一个创建请求不能用于不同的文档标题。请重新发起创建。",
  idempotency_required: "创建请求缺少标识，请重新发起创建。",
  invalid_check_run_acknowledgement: "确认请求无效，请刷新页面后重试。",
  invalid_clone_directory: "克隆目标目录不可用。请重新选择已配置的扫描目录并检查目录名。",
  invalid_clone_name: "目录名无效。请使用普通文件夹名称，不要包含路径分隔符。",
  invalid_clone_parent: "所选扫描目录不可用。请重新选择已配置的扫描目录。",
  invalid_clone_request: "克隆请求无效。请重新粘贴仓库地址并确认目标目录。",
  invalid_folder_picker_request: "目录选择请求无效，请刷新页面后重试。",
  invalid_host: "请求来源无效。请从启动终端输出的安全链接重新打开控制台。",
  invalid_job_cancel_request: "取消请求无效，请刷新页面后重试。",
  invalid_limit: "发布数量超出允许范围，请调整后重试。",
  invalid_monitoring_interval: "检查频率无效，请选择 5、15、30 或 60 分钟。",
  invalid_monitoring_settings: "监控设置无效，请刷新后重新设置。",
  invalid_notification_locale: "通知语言无效，请切换语言后重试。",
  invalid_option: "选项无效，请刷新页面后重试。",
  invalid_origin: "请求来源无效。请从启动终端输出的安全链接重新打开控制台。",
  invalid_query: "查询条件无效，请刷新页面后重试。",
  invalid_remote: "仓库地址无效，请检查后重试。",
  invalid_repository_profile: "别名最多 80 个字符、标签最多 8 个且每个不超过 24 个字符，均不能包含逗号。",
  invalid_response: "服务返回了无法识别的内容",
  invalid_quiet_hours: "勿扰时段无效，请重新选择开始和结束时间。",
  invalid_retention: "保留天数无效，请填写 0 到 3650 之间的整数。",
  invalid_roots: "扫描目录无效，请重新选择目录。",
  invalid_scan_roots: "扫描目录无效。请选择具体的项目目录，不要选择整个文件系统。",
  invalid_selection: "所选仓库或分支无效，请重新选择。",
  invalid_title: "文档标题无效，请重新输入。",
  invalid_token: "页面会话已失效，请从启动终端输出的安全链接重新打开",
  invalid_watch: "关注请求无效，请重新选择仓库和分支。",
  invalid_watch_id: "这个关注分支已不存在，请刷新页面。",
  job_not_cancellable: "当前任务不支持取消，请等待它结束。",
  job_not_found: "任务记录已不存在，请重新操作。",
  json_required: "请求格式无效，请刷新页面后重试。",
  lark_create_failed: "创建飞书文档失败。请在普通 Terminal 中确认 lark-cli 登录状态后重试。",
  lark_create_unknown: "创建结果待确认。请先在飞书云空间检查是否已生成文档，不要直接重复创建。",
  lark_document_required: "尚未关联飞书文档。请先在设置中关联或创建文档。",
  lark_document_unavailable: "无法访问已关联的飞书文档。请确认文档仍存在且当前身份有权限。",
  method_not_allowed: "该操作不受支持。",
  monitoring_service_failed: "无法更新本机后台服务。请重试，或运行 branch-radar doctor 查看原因。",
  not_found: "没有找到该页面，请刷新后重试。",
  operation_cancelled: "已取消克隆；未完成的目标目录不会被保留。",
  operation_failed: "任务遇到内部错误，请重试或查看启动终端的日志。",
  operation_in_progress: "已有任务正在运行，请等待当前任务完成。",
  remote_required: "仓库地址无效，请检查后重试。",
  repository_not_found: "没有找到该仓库，请重新扫描本地项目。",
  selection_not_found: "所选仓库或分支已不存在，请刷新页面后重新选择。",
  selection_required: "请至少选择一个仓库。",
  selection_too_large: "一次选择的数量过多，请分批添加。",
  title_required: "请输入文档标题。",
  watch_not_created: "关注未能建立，请重试。",
  watch_not_found: "没有找到该关注分支，请刷新页面。",
};

class ApiError extends Error {
  constructor(message, code, payload, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.payload = payload;
    this.status = status;
  }
}

function localizedErrorMessage(value, fallback = "操作失败") {
  const code = value?.code || value?.error_code || "";
  if (ERROR_COPY[code]) return t(ERROR_COPY[code]);
  if (code.startsWith("clone_")) {
    return cloneErrorMessage(code, value?.message || value?.error || "");
  }
  const message = value?.message || value?.error;
  if (!message) return t(fallback);
  const localized = t(message);
  if (i18n.locale() === "en" && localized === message && /[\u3400-\u9fff]/u.test(message)) {
    return t(fallback);
  }
  return localized;
}

function createIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  return svg;
}

function element(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = String(text);
  return value;
}

function uiElement(tag, className, text, variables) {
  return element(tag, className, t(text, variables));
}

function jobIsActive(job) {
  return Boolean(job && ACTIVE_JOB_STATUSES.has(job.status));
}

function localeFormatter(key, create) {
  const cacheKey = `${i18n.intlLocale()}:${key}`;
  if (!localeFormatters.has(cacheKey)) localeFormatters.set(cacheKey, create(i18n.intlLocale()));
  return localeFormatters.get(cacheKey);
}

function scheduleRender(key, render) {
  if (scheduledRenders.has(key)) return;
  const handle = window.requestAnimationFrame(() => {
    scheduledRenders.delete(key);
    render();
  });
  scheduledRenders.set(key, handle);
}

function identityText(value, fallback) {
  if (typeof value !== "string") return fallback;
  const normalized = value.trim();
  return normalized || fallback;
}

function identityInitial(value, fallback) {
  const normalized = identityText(value, "");
  if (!normalized) return fallback;
  const words = normalized.split(/\s+/).filter(Boolean);
  const first = Array.from(words[0] || "")[0] || fallback;
  const last = words.length > 1 ? (Array.from(words[words.length - 1])[0] || "") : "";
  return `${first}${last}`.toLocaleUpperCase();
}

function setIntegrationIdentity(provider, details) {
  document.getElementById(`${provider}-identity-name`).textContent = details.name;
  document.getElementById(`${provider}-identity-meta`).textContent = details.meta;
  const status = document.getElementById(`${provider}-status`);
  status.className = `status-chip${details.statusClass ? ` ${details.statusClass}` : ""}`;
  status.textContent = details.status;
  if (provider === "lark") {
    document.getElementById("lark-identity-avatar").textContent = details.avatar;
  }
}

function button(label, className, handler) {
  const value = element("button", className, label);
  value.type = "button";
  value.addEventListener("click", handler);
  return value;
}

function scanRootPickerJobReference(value) {
  if (!value || typeof value !== "object") return null;
  if (value.kind !== "pick_scan_root" || typeof value.id !== "string") return null;
  const id = value.id.trim();
  if (!id || id.length > 200) return null;
  return { id, kind: "pick_scan_root" };
}

function parseScanRootPickerJobReference(rawValue) {
  if (typeof rawValue !== "string" || !rawValue) return null;
  try {
    return scanRootPickerJobReference(JSON.parse(rawValue));
  } catch (_error) {
    return null;
  }
}

function storedScanRootPickerJob() {
  try {
    const rawValue = sessionStorage.getItem(SCAN_ROOT_PICKER_JOB_STORAGE_KEY);
    const reference = parseScanRootPickerJobReference(rawValue);
    if (rawValue && !reference) sessionStorage.removeItem(SCAN_ROOT_PICKER_JOB_STORAGE_KEY);
    return reference;
  } catch (_error) {
    return null;
  }
}

function rememberScanRootPickerJob(job) {
  const reference = scanRootPickerJobReference(job);
  if (!reference) return false;
  try {
    sessionStorage.setItem(SCAN_ROOT_PICKER_JOB_STORAGE_KEY, JSON.stringify(reference));
    return true;
  } catch (_error) {
    return false;
  }
}

function clearStoredScanRootPickerJob() {
  try {
    sessionStorage.removeItem(SCAN_ROOT_PICKER_JOB_STORAGE_KEY);
  } catch (_error) {
    // Recovery remains safe when session storage is unavailable.
  }
}

function storedCloneTask() {
  try {
    const raw = sessionStorage.getItem(CLONE_JOB_STORAGE_KEY);
    if (typeof raw !== "string" || !raw.trim()) return null;
    if (!raw.trim().startsWith("{")) {
      return raw.length <= 200 ? { id: raw } : null;
    }
    const value = JSON.parse(raw);
    if (!value || typeof value.id !== "string" || !value.id || value.id.length > 200) return null;
    return {
      id: value.id,
      repository: typeof value.repository === "string" ? value.repository.slice(0, 300) : "",
      directoryName: typeof value.directoryName === "string" ? value.directoryName.slice(0, 200) : "",
      parent: typeof value.parent === "string" ? value.parent.slice(0, 4096) : "",
    };
  } catch (_error) {
    return null;
  }
}

function storedCloneJobId() {
  return storedCloneTask()?.id || null;
}

function rememberCloneJob(job) {
  if (job?.kind !== "clone" || typeof job.id !== "string" || !job.id || job.id.length > 200) return;
  try {
    const previous = storedCloneTask();
    const repository = identityText(
      state.cloneSubmission?.repository
        || state.clonePreview?.remote?.canonical
        || previous?.repository,
      "",
    );
    const directoryName = identityText(
      state.cloneSubmission?.directoryName || previous?.directoryName,
      "",
    );
    const roots = configuredScanRoots();
    sessionStorage.setItem(CLONE_JOB_STORAGE_KEY, JSON.stringify({
      id: job.id,
      repository: repository.slice(0, 300),
      directoryName: directoryName.slice(0, 200),
      parent: roots.includes(state.cloneSubmission?.parent) ? state.cloneSubmission.parent : previous?.parent || "",
    }));
  } catch (_error) {
    // The dashboard still restores an active clone when session storage is unavailable.
  }
}

function clearStoredCloneJob() {
  try {
    sessionStorage.removeItem(CLONE_JOB_STORAGE_KEY);
  } catch (_error) {
    // A stale recovery hint is harmless when storage is unavailable.
  }
}

function configuredScanRoots() {
  const roots = state.dashboard?.settings?.scan_roots;
  if (!Array.isArray(roots)) return [];
  const seen = new Set();
  return roots.reduce((values, root) => {
    if (typeof root !== "string") return values;
    if (!root.trim() || seen.has(root)) return values;
    seen.add(root);
    values.push(root);
    return values;
  }, []);
}

function hasConfiguredScanRoots() {
  return configuredScanRoots().length > 0;
}

function scanRootsSummary(roots = configuredScanRoots()) {
  if (!roots.length) return t("尚未设置扫描目录");
  const visible = roots.slice(0, 2).join(i18n.locale() === "en" ? ", " : "、");
  return roots.length > 2
    ? t("{count} 个目录 · {visible} 等", { count: roots.length, visible })
    : t("{count} 个目录 · {visible}", { count: roots.length, visible });
}

function syncScanRootsControls() {
  const roots = configuredScanRoots();
  const ready = Boolean(state.dashboard);
  const busy = Boolean(state.activeJob || state.scanRootsSaving || state.scanRootPickerPending);
  const scopeButton = document.getElementById("scan-roots-button");
  const refreshButton = document.getElementById("projects-refresh-button");
  if (scopeButton) {
    scopeButton.disabled = !ready || busy;
    scopeButton.setAttribute("aria-label", roots.length
      ? t("设置本地项目扫描范围，当前 {count} 个目录", { count: roots.length })
      : t("设置本地项目扫描范围，当前尚未设置目录"));
    scopeButton.title = roots.length ? scanRootsSummary(roots) : t("先选择要扫描的本地目录");
  }
  if (refreshButton) refreshButton.disabled = !ready || !roots.length || busy;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Branch-Radar-Token", csrfToken);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new ApiError(t("服务返回了无法识别的内容"), "invalid_response", null, response.status);
  }
  if (!response.ok) {
    const error = payload.error || {};
    throw new ApiError(error.message || t("操作失败"), error.code || "request_failed", payload, response.status);
  }
  return payload;
}

const monitoringApi = Object.freeze({
  read() {
    return api(MONITORING_ENDPOINT);
  },
  save(payload) {
    return submitJob(MONITORING_ENDPOINT, payload, {
      method: "PUT",
      resumeConflict: false,
      surfaceError: false,
    });
  },
});

function monitoringClockValue(value, fallback) {
  const source = String(value || "").trim();
  const match = /^(\d{2}):(\d{2})$/.exec(source);
  if (!match) return fallback;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  return hours <= 23 && minutes <= 59 ? source : fallback;
}

function normalizeMonitoringSettings(payload) {
  const root = payload && typeof payload === "object" ? payload : {};
  const source = root.monitoring && typeof root.monitoring === "object" ? root.monitoring : root;
  const quiet = source.quiet_hours && typeof source.quiet_hours === "object" ? source.quiet_hours : {};
  const interval = Number(source.interval_seconds);
  const platform = typeof root.platform === "string" ? root.platform.trim().slice(0, 40) : "";
  return {
    enabled: Boolean(source.enabled),
    interval_seconds: Number.isSafeInteger(interval) && interval > 0 ? interval : MONITORING_DEFAULT_INTERVAL,
    notifications_enabled: Boolean(source.notifications_enabled),
    quiet_hours: {
      enabled: Boolean(quiet.enabled),
      start: monitoringClockValue(quiet.start, "22:00"),
      end: monitoringClockValue(quiet.end, "08:00"),
    },
    locale: source.locale === "en" ? "en" : "zh-CN",
    supported: root.supported !== false,
    platform,
    installed: Boolean(root.installed),
    loaded: Boolean(root.loaded),
    notifications_supported: root.notifications_supported !== false,
    notification_provider: typeof root.notification_provider === "string" ? root.notification_provider.slice(0, 40) : "",
    last_attempt_at: root.last_attempt_at || null,
    last_success_at: root.last_success_at || null,
    next_run_at: root.next_run_at || null,
  };
}

function monitoringCustomIntervalCopy(intervalSeconds) {
  if (!Number.isSafeInteger(intervalSeconds) || intervalSeconds <= 0 || MONITORING_INTERVALS.has(intervalSeconds)) return "";
  if (intervalSeconds % 60 === 0) {
    return t("当前频率：自定义 {count} 分钟。请选择一个新频率后再保存。", { count: intervalSeconds / 60 });
  }
  return t("当前频率：自定义 {count} 秒。请选择一个新频率后再保存。", { count: intervalSeconds });
}

function monitoringPlatformLabel(value) {
  const platform = String(value || "").toLowerCase();
  if (platform.includes("darwin") || platform.includes("mac")) return "macOS";
  if (platform.includes("linux")) return "Linux";
  if (platform.includes("win")) return "Windows";
  return t("当前系统");
}

function monitoringServicePresentation(value) {
  if (!value?.supported) return { label: t("暂不支持"), modifier: "is-muted" };
  if (value.loaded) return { label: t("已启用"), modifier: "" };
  if (value.enabled && value.installed) return { label: t("等待启动"), modifier: "is-warning" };
  if (value.installed) return { label: t("已停止"), modifier: "is-muted" };
  return { label: t("未启用"), modifier: "is-muted" };
}

function setMonitoringMessage(source, { kind = "", variables = {}, raw = false, remember = true } = {}) {
  const status = document.getElementById("monitoring-save-status");
  if (!status) return;
  if (remember) state.monitoringMessage = { source, kind, variables, raw };
  status.textContent = raw ? String(source || "") : t(source, variables);
  status.classList.toggle("is-success", kind === "success");
  status.classList.toggle("is-error", kind === "error");
  status.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
}

function syncMonitoringControls() {
  const form = document.getElementById("monitoring-form");
  if (!form) return;
  const available = Boolean(state.monitoring?.supported)
    && !state.monitoringLoading
    && !state.monitoringSaving
    && !state.activeJob;
  const notifications = document.getElementById("monitoring-notifications");
  const quietEnabled = document.getElementById("monitoring-quiet-enabled");
  form.querySelectorAll("input").forEach((control) => { control.disabled = !available; });
  if (notifications) notifications.disabled = !available || !state.monitoring?.notifications_supported;
  if (quietEnabled) quietEnabled.disabled = !available || !state.monitoring?.notifications_supported || !notifications.checked;
  document.querySelectorAll("#monitoring-quiet-start, #monitoring-quiet-end").forEach((control) => {
    control.disabled = !available || !state.monitoring?.notifications_supported || !notifications.checked || !quietEnabled.checked;
  });
  const save = document.getElementById("monitoring-save-button");
  const reload = document.getElementById("monitoring-reload-button");
  save.disabled = !available || !state.monitoringDirty;
  save.setAttribute("aria-busy", state.monitoringSaving ? "true" : "false");
  save.querySelector("span").textContent = t(state.monitoringSaving ? "正在保存…" : "保存监控设置");
  reload.hidden = !state.monitoringError;
  reload.disabled = state.monitoringLoading || state.monitoringSaving;
  form.setAttribute("aria-busy", state.monitoringLoading || state.monitoringSaving ? "true" : "false");
}

function monitoringNextRunCopy(value) {
  if (!value?.enabled) return t("启用后计算");
  if (!value.loaded) return t("服务运行后计算");
  if (value.next_run_at) return formatTime(value.next_run_at, true);
  return t("等待首次运行");
}

function renderMonitoringSettings(value, { preserveDraft = false, message = null } = {}) {
  state.monitoring = value;
  if (!preserveDraft) state.monitoringError = null;
  const card = document.getElementById("monitoring-card");
  const presentation = monitoringServicePresentation(value);
  const status = document.getElementById("monitoring-service-status");
  status.textContent = presentation.label;
  status.className = `status-chip ${presentation.modifier}`.trim();
  card.classList.toggle("is-running", Boolean(value.supported && value.loaded));
  card.classList.toggle("is-unavailable", !value.supported);

  const platform = monitoringPlatformLabel(value.platform);
  document.getElementById("monitoring-service-detail").textContent = value.supported
    ? `${platform} · ${t(value.installed ? "已安装" : "未安装")}`
    : `${platform} · ${t("不受支持")}`;
  const lastSuccess = document.getElementById("monitoring-last-success");
  lastSuccess.textContent = value.last_success_at ? formatTime(value.last_success_at, true) : t("暂无成功记录");
  lastSuccess.title = value.last_success_at ? formatTime(value.last_success_at) : "";
  const nextRun = document.getElementById("monitoring-next-run");
  nextRun.textContent = monitoringNextRunCopy(value);
  nextRun.title = value.next_run_at ? formatTime(value.next_run_at) : "";
  const customInterval = document.getElementById("monitoring-custom-interval");
  const customIntervalCopy = monitoringCustomIntervalCopy(value.interval_seconds);
  customInterval.textContent = customIntervalCopy;
  customInterval.hidden = !customIntervalCopy;
  document.getElementById("monitoring-notifications-help").textContent = t(value.notifications_supported
    ? "仅在发现新变化时提醒，每轮合并为一条。"
    : "当前系统不支持系统通知；后台检查仍可使用。");

  if (!preserveDraft) {
    document.getElementById("monitoring-enabled").checked = value.enabled;
    document.getElementById("monitoring-notifications").checked = value.notifications_enabled;
    document.getElementById("monitoring-quiet-enabled").checked = value.quiet_hours.enabled;
    document.getElementById("monitoring-quiet-start").value = value.quiet_hours.start;
    document.getElementById("monitoring-quiet-end").value = value.quiet_hours.end;
    document.querySelectorAll('input[name="monitoring-interval"]').forEach((control) => {
      control.checked = Number(control.value) === value.interval_seconds;
    });
    state.monitoringDirty = false;
    document.getElementById("monitoring-quiet-error").textContent = "";
    document.getElementById("monitoring-frequency-error").textContent = "";
    document.querySelector(".monitoring-frequency-options").setAttribute("aria-invalid", "false");
    document.querySelectorAll('input[name="monitoring-interval"], #monitoring-quiet-start, #monitoring-quiet-end').forEach((control) => {
      control.setCustomValidity("");
      control.setAttribute("aria-invalid", "false");
    });
  }
  if (message) setMonitoringMessage(message.source, message);
  else if (!value.supported) setMonitoringMessage("当前系统暂不支持后台监控，手动检查仍可使用。");
  else if (!state.monitoringDirty) setMonitoringMessage("监控设置已与本机同步。");
  syncMonitoringControls();
}

function renderMonitoringLoadState() {
  const status = document.getElementById("monitoring-service-status");
  status.textContent = t(state.monitoring ? "刷新中" : "读取中");
  status.className = "status-chip is-loading";
  if (!state.monitoring) {
    document.getElementById("monitoring-service-detail").textContent = t("正在读取…");
    document.getElementById("monitoring-last-success").textContent = t("正在读取…");
    document.getElementById("monitoring-next-run").textContent = t("正在读取…");
  }
  setMonitoringMessage(state.monitoring ? "正在刷新本机监控状态…" : "正在读取本机监控设置…");
  syncMonitoringControls();
}

function renderMonitoringLoadError(error) {
  state.monitoringError = error;
  if (state.monitoring) {
    renderMonitoringSettings(state.monitoring, { preserveDraft: true });
    state.monitoringError = error;
  } else {
    const card = document.getElementById("monitoring-card");
    card.classList.remove("is-running");
    card.classList.add("is-unavailable");
    const status = document.getElementById("monitoring-service-status");
    status.textContent = t("读取失败");
    status.className = "status-chip is-error";
    document.getElementById("monitoring-service-detail").textContent = t("暂时不可用");
    document.getElementById("monitoring-last-success").textContent = "—";
    document.getElementById("monitoring-next-run").textContent = "—";
  }
  setMonitoringMessage(localizedErrorMessage(error, "无法读取监控设置，请重新读取。"), { kind: "error", raw: true });
  syncMonitoringControls();
}

async function loadMonitoringSettings({ force = false, preserveDraft = false } = {}) {
  if (state.monitoringLoading || state.monitoringSaving) return state.monitoring;
  if (!force && state.monitoring) return state.monitoring;
  state.monitoringLoading = true;
  state.monitoringError = null;
  renderMonitoringLoadState();
  syncContextActions();
  try {
    const value = normalizeMonitoringSettings(await monitoringApi.read());
    state.monitoringLoading = false;
    state.monitoringLoadedAt = Date.now();
    renderMonitoringSettings(value, { preserveDraft });
    syncContextActions();
    return value;
  } catch (error) {
    state.monitoringLoading = false;
    renderMonitoringLoadError(error);
    syncContextActions();
    return null;
  }
}

function monitoringDraftPayload() {
  const selectedInterval = Number(document.querySelector('input[name="monitoring-interval"]:checked')?.value);
  return {
    enabled: document.getElementById("monitoring-enabled").checked,
    interval_seconds: selectedInterval,
    notifications_enabled: document.getElementById("monitoring-notifications").checked,
    quiet_hours: {
      enabled: document.getElementById("monitoring-quiet-enabled").checked,
      start: document.getElementById("monitoring-quiet-start").value,
      end: document.getElementById("monitoring-quiet-end").value,
    },
    locale: i18n.locale() === "en" ? "en" : "zh-CN",
  };
}

function validateMonitoringInterval({ focus = false } = {}) {
  const controls = Array.from(document.querySelectorAll('input[name="monitoring-interval"]'));
  const selected = Number(controls.find((control) => control.checked)?.value);
  const source = MONITORING_INTERVALS.has(selected) ? "" : "请选择 5、15、30 或 60 分钟的检查频率。";
  const message = source ? t(source) : "";
  const group = document.querySelector(".monitoring-frequency-options");
  group.setAttribute("aria-invalid", source ? "true" : "false");
  controls.forEach((control) => control.setCustomValidity(message));
  document.getElementById("monitoring-frequency-error").textContent = message;
  if (source && focus) controls[0]?.focus();
  return !source;
}

function validateMonitoringQuietHours({ focus = false } = {}) {
  const notifications = state.monitoring?.notifications_supported
    && document.getElementById("monitoring-notifications").checked;
  const quietEnabled = document.getElementById("monitoring-quiet-enabled").checked;
  const start = document.getElementById("monitoring-quiet-start");
  const end = document.getElementById("monitoring-quiet-end");
  let source = "";
  if (notifications && quietEnabled && (!monitoringClockValue(start.value, "") || !monitoringClockValue(end.value, ""))) {
    source = "请选择完整的勿扰开始和结束时间。";
  } else if (notifications && quietEnabled && start.value === end.value) {
    source = "勿扰开始和结束时间不能相同。";
  }
  const message = source ? t(source) : "";
  document.getElementById("monitoring-quiet-error").textContent = message;
  [start, end].forEach((control) => {
    control.setAttribute("aria-invalid", source ? "true" : "false");
    control.setCustomValidity(message);
  });
  if (source && focus) start.focus();
  return !source;
}

function markMonitoringDirty() {
  if (!state.monitoring || state.monitoringLoading || state.monitoringSaving) return;
  state.monitoringDirty = true;
  setMonitoringMessage("设置尚未保存。");
  if (document.getElementById("monitoring-quiet-error").textContent) {
    validateMonitoringQuietHours();
  }
  if (document.getElementById("monitoring-frequency-error").textContent) {
    validateMonitoringInterval();
  }
  syncMonitoringControls();
}

async function saveMonitoringSettings(event) {
  event.preventDefault();
  if (!state.monitoring?.supported || state.monitoringLoading || state.monitoringSaving || state.activeJob) return;
  if (!validateMonitoringInterval({ focus: true }) || !validateMonitoringQuietHours({ focus: true })) return;
  const payload = monitoringDraftPayload();
  state.monitoringSaving = true;
  setMonitoringMessage("正在提交监控设置…");
  syncMonitoringControls();
  syncContextActions();
  try {
    const job = await monitoringApi.save(payload);
    state.monitoringSubmissionJobId = job.id;
    setMonitoringMessage("监控设置正在后台应用；进度显示在页面底部。");
    syncMonitoringControls();
    syncContextActions();
  } catch (error) {
    state.monitoringSaving = false;
    state.monitoringSubmissionJobId = null;
    setMonitoringMessage(localizedErrorMessage(error, "监控设置保存失败，请重试。"), { kind: "error", raw: true });
    syncMonitoringControls();
    syncContextActions();
    document.getElementById("monitoring-save-status").focus({ preventScroll: true });
  }
}

async function refreshSettingsStatus() {
  if (state.activeJob || state.monitoringSaving) return;
  await Promise.all([
    loadMonitoringSettings({ force: true }),
    refreshIntegrations(),
  ]);
}

function currentView() {
  const value = location.hash.replace(/^#/, "").split("/", 1)[0];
  if (value === "activity") return "overview";
  if (value === "connections") return "settings";
  return viewCopy[value] ? value : "overview";
}

function watchWorkspaceTabFromLocation() {
  return location.hash.replace(/^#/, "") === "projects/add" ? "add" : "watched";
}

function setWatchWorkspaceTab(name, { focus = false, syncUrl = true } = {}) {
  const selected = name === "add" ? "add" : "watched";
  state.watchWorkspaceTab = selected;
  document.querySelectorAll("[data-watch-tab]").forEach((tab) => {
    const active = tab.dataset.watchTab === selected;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  document.querySelectorAll("[data-watch-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.watchPanel !== selected;
  });
  document.getElementById("watch-list-controls").hidden = selected !== "watched";
  document.getElementById("watch-workspace-description").textContent = selected === "add"
    ? t("从本地 checkout 选择分支，或粘贴 GitHub 地址匹配已有仓库。")
    : t("查看状态、筛选分支并执行批量检查。");
  if (syncUrl && currentView() === "projects") {
    const nextHash = selected === "add" ? "#projects/add" : "#projects";
    if (location.hash !== nextHash) {
      history.replaceState(null, "", `${location.pathname}${location.search}${nextHash}`);
    }
  }
  if (currentView() === "projects") {
    if (selected === "add") {
      if (state.candidates) renderLocalProjects();
      ensureLocalProjects();
    } else if (state.dashboard) {
      renderProjectList();
    }
  }
  syncContextActions();
  syncBatchActionBar();
}

function bindWatchWorkspaceTabs() {
  const tabs = Array.from(document.querySelectorAll("[data-watch-tab]"));
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => setWatchWorkspaceTab(tab.dataset.watchTab));
    tab.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (["ArrowRight", "ArrowDown"].includes(event.key)) nextIndex = (index + 1) % tabs.length;
      if (["ArrowLeft", "ArrowUp"].includes(event.key)) nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      setWatchWorkspaceTab(tabs[nextIndex].dataset.watchTab, { focus: true });
    });
  });
}

function setView(name) {
  const selected = viewCopy[name] ? name : "overview";
  const previous = document.body.dataset.view;
  if (selected !== "overview" && state.canvasFocused) setCanvasFocus(false, { restoreFocus: false });
  document.body.dataset.view = selected;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    const active = panel.dataset.viewPanel === selected;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
  document.querySelectorAll("[data-view]").forEach((item) => {
    const active = item.dataset.view === selected;
    item.classList.toggle("is-active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.getElementById("view-eyebrow").textContent = t(viewCopy[selected][0]);
  document.getElementById("view-title").textContent = t(viewCopy[selected][1]);
  if (selected === "overview" && state.dashboard) {
    renderWatchPreview(state.dashboard.watches || []);
    renderActivityList();
  }
  if (selected === "projects") {
    setWatchWorkspaceTab(watchWorkspaceTabFromLocation(), { syncUrl: false });
  }
  if (selected === "changelog") ensureChangelog({ refresh: true });
  if (selected === "settings" && (!state.integrations || !state.integrations.checked || state.integrations.stale)) {
    refreshIntegrations();
  }
  if (
    selected === "settings"
    && !state.monitoringLoading
    && (
      !state.monitoring
      || (previous !== "settings" && Date.now() - state.monitoringLoadedAt > 30_000)
    )
  ) {
    loadMonitoringSettings({ force: Boolean(state.monitoring) });
  }
  syncContextActions();
  syncBatchActionBar();
}

function syncContextActions() {
  const view = currentView();
  const check = document.getElementById("check-button");
  const options = document.getElementById("check-options-button");
  const connections = document.getElementById("connections-refresh-button");
  const hasSelection = state.selectedWatchIds.size > 0;
  const watchedWorkspace = view === "projects" && state.watchWorkspaceTab === "watched";

  connections.hidden = view !== "settings";
  options.hidden = view !== "overview";
  check.hidden = !(view === "overview" || (watchedWorkspace && !hasSelection));
  const hasWatches = Boolean(state.dashboard?.watches?.length);
  check.disabled = !hasWatches || checkOperationBusy();
  options.disabled = !hasWatches || checkOperationBusy();
  connections.disabled = Boolean(state.activeJob || state.monitoringLoading || state.monitoringSaving);
  if (!check.hidden) {
    const label = check.querySelector("span");
    if (label) label.textContent = t("检查全部");
    check.setAttribute("aria-label", t("检查全部关注分支"));
  }
  syncScanRootsControls();
}

function formatReleaseDate(value) {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return t("日期待定");
  return localeFormatter("release-date", (locale) => new Intl.DateTimeFormat(locale, {
    year: "numeric", month: "long", day: "numeric", timeZone: "UTC",
  })).format(date);
}

function changelogTypeModifier(type) {
  if (type === "新增") return "is-new";
  if (type === "修复") return "is-fix";
  if (type === "安全") return "is-security";
  return "is-improvement";
}

function localizedChangelogValue(value, key) {
  if (!value || typeof value !== "object") return "";
  const localizedKey = i18n.locale() === "en" ? `${key}_en` : key;
  const candidate = value[localizedKey] ?? value[key];
  return typeof candidate === "string" ? candidate.trim() : "";
}

function normalizedChangelogChanges(changes) {
  if (!Array.isArray(changes)) return [];
  return changes.flatMap((change) => {
    if (!change || typeof change !== "object" || !Array.isArray(change.items)) return [];
    const localizedItems = i18n.locale() === "en" && Array.isArray(change.items_en)
      ? change.items_en
      : change.items;
    const items = localizedItems
      .filter((item) => typeof item === "string" && item.trim())
      .map((item) => item.trim());
    if (!items.length) return [];
    const sourceType = typeof change.type === "string" && change.type.trim() ? change.type.trim() : "变化";
    const type = localizedChangelogValue(change, "type") || t(sourceType);
    return [{ type, sourceType, items }];
  });
}

function changelogChangeGroup(change, { highlight = false } = {}) {
  const section = element(
    "section",
    `changelog-change-group${highlight ? " changelog-highlight is-highlight" : ""}`,
  );
  const heading = element("h3", changelogTypeModifier(change.sourceType || change.type), change.type);
  if (highlight) {
    section.append(heading, element("p", "", change.items[0]));
    return section;
  }
  const list = element("ul", "changelog-change-list");
  change.items.forEach((item) => list.append(element("li", "", item)));
  section.append(heading, list);
  return section;
}

function changelogDetails(changes, totalItems) {
  const details = element("details", "changelog-details");
  const summary = element("summary", "changelog-details-summary");
  const collapsedCopy = `查看全部 ${totalItems} 项变化`;
  const expandedCopy = "收起详细变化";
  const collapsedLabel = uiElement("span", "changelog-details-label is-collapsed", collapsedCopy);
  const expandedLabel = uiElement("span", "changelog-details-label is-expanded", expandedCopy);
  expandedLabel.hidden = true;
  summary.setAttribute("aria-label", t(collapsedCopy));
  summary.append(collapsedLabel, expandedLabel, createIcon("chevron"));

  const changeGrid = element("div", "changelog-change-grid");
  changes.forEach((change) => changeGrid.append(changelogChangeGroup(change)));
  details.append(summary, changeGrid);
  details.addEventListener("toggle", () => {
    collapsedLabel.hidden = details.open;
    expandedLabel.hidden = !details.open;
    summary.setAttribute("aria-label", t(details.open ? expandedCopy : collapsedCopy));
  });
  return details;
}

function renderChangelog(payload) {
  const releases = Array.isArray(payload?.releases) ? payload.releases : [];
  const container = document.getElementById("changelog-list");
  const current = releases.find((release) => release?.current) || releases[0];
  document.getElementById("changelog-current-version").textContent = current?.version || "—";
  document.getElementById("changelog-current-date").textContent = current?.date
    ? formatReleaseDate(current.date)
    : t("暂无记录");

  if (!releases.length) {
    container.replaceChildren(emptyState("还没有更新记录", "后续版本会在这里留下简短、可回看的说明。"));
    return;
  }

  const fragment = document.createDocumentFragment();
  releases.forEach((release, releaseIndex) => {
    if (!release || typeof release !== "object") return;
    const isUpdate = release.entry_type === "update";
    const article = element(
      "article",
      `changelog-release${release.current ? " is-current" : ""}${isUpdate ? " is-update" : ""}`,
    );
    const rail = element("div", "changelog-release-rail");
    rail.append(element("span", "changelog-release-node"));
    const content = element("div", "changelog-release-content");
    const header = element("header", "changelog-release-header");
    const meta = element("div", "changelog-release-meta");
    const time = element("time", "", formatReleaseDate(release.date));
    if (release.date) time.dateTime = release.date;
    meta.append(element("span", "changelog-version", `v${release.version || current?.version || "—"}`), time);
    if (isUpdate) meta.append(uiElement("span", "changelog-update-badge", "补充更新"));
    if (release.current) meta.append(uiElement("span", "changelog-current-badge", "当前版本"));
    const headline = localizedChangelogValue(release, "headline");
    const summary = localizedChangelogValue(release, "summary");
    const title = element("h2", "", headline || (isUpdate
      ? t("补充更新")
      : t("版本 {version}", { version: release.version || t("更新") })));
    title.id = `changelog-release-${releaseIndex}`;
    header.append(meta, title, element("p", "", summary));
    article.setAttribute("aria-labelledby", title.id);
    content.append(header);

    const changes = normalizedChangelogChanges(release.changes);
    const totalItems = changes.reduce((total, change) => total + change.items.length, 0);
    if (changes.length) {
      const categories = element("ul", "changelog-category-chips");
      categories.setAttribute("aria-label", t("变化分类统计"));
      changes.forEach((change) => {
        categories.append(element(
          "li",
          `changelog-category-chip changelog-change-count ${changelogTypeModifier(change.sourceType || change.type)}`,
          `${change.type} ${change.items.length}`,
        ));
      });
      const highlights = element("div", "changelog-highlights");
      changes.slice(0, 4).forEach((change) => highlights.append(changelogChangeGroup(change, { highlight: true })));
      content.append(categories, highlights, changelogDetails(changes, totalItems));
    }
    article.append(rail, content);
    fragment.append(article);
  });
  container.replaceChildren(fragment);
}

async function ensureChangelog({ refresh = false } = {}) {
  if (state.changelogLoading || (state.changelog && !refresh)) return;
  const previous = state.changelog;
  state.changelogLoading = true;
  const container = document.getElementById("changelog-list");
  container.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/assets/changelog.json", {
      cache: "no-cache",
      headers: { "Accept": "application/json" },
    });
    if (!response.ok) throw new Error(t("更新日志读取失败"));
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.releases)) throw new Error(t("更新日志格式无效"));
    const changed = !previous || JSON.stringify(previous) !== JSON.stringify(payload);
    state.changelog = payload;
    if (changed) renderChangelog(payload);
  } catch (_error) {
    if (previous) {
      state.changelog = previous;
      showToast(t("更新日志同步失败，已保留上次内容。"), true);
    } else {
      container.replaceChildren(emptyState(
        "暂时无法读取更新日志",
        "刷新页面或稍后重新读取。",
        "重新读取",
        () => ensureChangelog({ refresh: true }),
      ));
    }
  } finally {
    state.changelogLoading = false;
    container.setAttribute("aria-busy", "false");
  }
}

function formatTime(value, relative = false) {
  if (!value) return t("尚未检查");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("时间未知");
  if (!relative) {
    return localeFormatter("absolute-time", (locale) => new Intl.DateTimeFormat(locale, {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    })).format(date);
  }
  const formatter = localeFormatter(
    "relative-time",
    (locale) => new Intl.RelativeTimeFormat(locale, { numeric: "auto" }),
  );
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}

const shortSha = uiModel.shortSha;

function branchStatus(watch) {
  if (watch.error) return ["检查失败", "is-error"];
  if (watch.baseline_pending) return ["等待基线", "is-pending"];
  if (!watch.present) return ["分支不存在", "is-warning"];
  return ["正常", ""];
}

function statusChip(label, modifier = "") {
  return uiElement("span", `status-chip ${modifier}`.trim(), label);
}

function checkRunStatusCopy(status) {
  return t({
    succeeded: "检查完成",
    completed_with_errors: "检查完成，部分需处理",
    reconcile_required: "检查完成，发布待对账",
    failed: "检查失败",
    interrupted: "检查已中断",
  }[status] || "最近检查");
}

function checkRunDuration(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return t("耗时未知");
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return t(`${seconds} 秒`);
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return t(remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`);
}

function checkRunBacklogParts(backlog) {
  return [
    backlog?.succeeded ? t("{count} 成功", { count: backlog.succeeded }) : "",
    backlog?.failed ? t("{count} 失败", { count: backlog.failed }) : "",
    backlog?.unknown ? t("{count} 待对账", { count: backlog.unknown }) : "",
  ].filter(Boolean);
}

function appendRunSummaryGroup(container, title, values) {
  if (!values?.length) return;
  const group = element("div", "check-run-summary-group");
  group.append(uiElement("strong", "", title));
  const list = element("ul");
  values.forEach((value) => list.append(element("li", "", value)));
  group.append(list);
  container.append(group);
}

function checkRunEventReferences(run) {
  if (run.events?.length) return run.events;
  const loadedEvents = new Map((state.activityEvents || []).map((event) => [String(event.id), event]));
  return (run.eventIds || []).map((eventId) => {
    const event = loadedEvents.get(String(eventId));
    return {
      id: eventId,
      repository: event?.repository || "",
      branch: event?.branch || "",
      kind: event?.kind || "",
      title: event?.summary?.title || "",
      categories: Array.isArray(event?.categories) ? event.categories : [],
    };
  });
}

function renderCheckRunDetails(run) {
  const container = document.getElementById("check-run-details");
  container.replaceChildren();

  const summary = element("section", "check-run-summary");
  const source = run.summary?.source === "codex" ? "Codex 跨事件综述" : "确定性批次摘要";
  summary.append(uiElement("span", "check-run-summary-source", source));
  if (run.summary) {
    if (run.summary.overview) summary.append(element("p", "", run.summary.overview));
    const groups = element("div", "check-run-summary-groups");
    appendRunSummaryGroup(groups, "主题", run.summary.themes);
    appendRunSummaryGroup(groups, "跨仓库关联", run.summary.connections);
    appendRunSummaryGroup(groups, "建议先看", run.summary.priorities);
    if (groups.childElementCount) summary.append(groups);
  } else {
    summary.append(
      uiElement("h3", "", "本轮结果"),
      uiElement("p", "", run.counts.events > 1
        ? "已保留本轮跨分支结果；当前没有可用的跨事件综述。"
        : "本轮结果由本地事实确定，单个事件可直接查看详情。"),
    );
  }
  [run.summary?.completenessNote, run.summaryWarning, run.error].filter(Boolean).forEach((message) => {
    summary.append(element("div", "check-run-note", message));
  });
  const backlogParts = checkRunBacklogParts(run.publication.backlog);
  if (backlogParts.length) {
    summary.append(element("div", "check-run-note", t("历史投递：{parts}。", { parts: backlogParts.join(" · ") })));
  }

  const eventSection = element("section", "check-run-events");
  eventSection.append(element("strong", "", t("本轮事件 · {count}", { count: run.counts.events })));
  const references = checkRunEventReferences(run);
  const list = element("div", "check-run-event-list");
  references.slice(0, 12).forEach((reference) => {
    const row = element("button", "check-run-event");
    row.type = "button";
    const copy = element("span");
    const fallbackTitle = reference.repository && reference.branch
      ? `${reference.repository} / ${reference.branch}`
      : t("事件 #{id}", { id: reference.id });
    copy.append(
      element("strong", "", reference.title || fallbackTitle),
      element("small", "", [reference.repository, reference.branch, t(kindCopy[reference.kind] || "")].filter(Boolean).join(" · ") || t("事件 #{id}", { id: reference.id })),
    );
    row.append(copy, uiElement("span", "", "查看"));
    row.addEventListener("click", () => openEvent(reference.id));
    list.append(row);
  });
  if (references.length) eventSection.append(list);
  else eventSection.append(uiElement("p", "check-run-event-overflow", "本轮没有产生可查看的新事件。"));
  const hiddenCount = Math.max(0, run.counts.events - Math.min(references.length, 12));
  if (run.eventsTruncated || hiddenCount) {
    eventSection.append(element("p", "check-run-event-overflow", t(
      "另有 {count} 条事件，请在最近变化中查看。",
      { count: hiddenCount || t("更多") },
    )));
  }
  container.append(summary, eventSection);
  if (run.blocked?.length) {
    const blockedSection = element("section", "check-run-blocked");
    const attentionBranchCount = Math.max(run.blocked.length, run.counts.blocked);
    blockedSection.append(element(
      "strong",
      "",
      run.blockedTruncated ? t("需要关注的分支") : t("需要关注的分支 · {count}", { count: attentionBranchCount }),
    ));
    const blockedList = element("div", "check-run-blocked-list");
    run.blocked.slice(0, 8).forEach((item) => {
      const row = element("div", "check-run-blocked-row");
      row.append(
        element("strong", "", [item.repository, item.branch].filter(Boolean).join(" / ") || t("分支 #{id}", { id: item.branchId || "—" })),
        element("span", "", item.error || item.status || t("本轮未能完成观察")),
      );
      blockedList.append(row);
    });
    blockedSection.append(blockedList);
    const hiddenBlocked = Math.max(0, attentionBranchCount - Math.min(run.blocked.length, 8));
    if (run.blockedTruncated) {
      blockedSection.append(uiElement("p", "check-run-event-overflow", "另有更多分支需要关注。"));
    } else if (hiddenBlocked) {
      blockedSection.append(element("p", "check-run-event-overflow", t("另有 {count} 个分支需要关注。", { count: hiddenBlocked })));
    }
    container.append(blockedSection);
  }
  container.hidden = !state.checkRunExpanded;
}

function renderCheckRunBanner(payload) {
  const banner = document.getElementById("check-run-banner");
  const rawRun = payload?.pending_check_run || payload?.latest_check_run;
  const run = uiModel.normalizeCheckRunSummary(rawRun);
  if (!run.id) {
    banner.hidden = true;
    state.visibleCheckRunId = null;
    return;
  }

  if (String(state.visibleCheckRunId) !== String(run.id)) {
    state.visibleCheckRunId = run.id;
    state.checkRunExpanded = false;
  }
  banner.hidden = false;
  banner.dataset.runId = String(run.id);
  const actionablePending = typeof rawRun?.pending === "boolean"
    ? run.pending
    : Boolean(payload?.pending_check_run) && String(payload.pending_check_run.id) === String(run.id);
  const needsAttention = run.counts.blocked > 0 || run.counts.errors > 0
    || run.publication.failed > 0 || run.publication.unknown > 0
    || run.publication.backlog.failed > 0 || run.publication.backlog.unknown > 0
    || ["completed_with_errors", "reconcile_required", "failed", "interrupted"].includes(run.status);
  banner.classList.toggle("is-pending", actionablePending);
  banner.classList.toggle("has-errors", needsAttention);
  document.getElementById("check-run-kicker").textContent = actionablePending
    ? `NEW CHECK RUN · ${formatTime(run.finishedAt || run.startedAt, true)}`
    : `LAST CHECK · ${formatTime(run.finishedAt || run.startedAt, true)}`;

  const deterministicTitle = run.status === "failed" ? t("检查未完成")
    : run.status === "interrupted" ? t("检查已中断")
      : run.counts.changed ? t("发现 {count} 个变化", { count: run.counts.changed })
        : t("没有发现新的变化");
  document.getElementById("check-run-title").textContent = run.summary?.title || deterministicTitle;
  const fallbackOverview = run.error || t("{status}：检查 {checked} 个分支，产生 {events} 条事件。", {
    status: checkRunStatusCopy(run.status), checked: run.counts.checked, events: run.counts.events,
  });
  document.getElementById("check-run-overview").textContent = fallbackOverview;

  const stats = document.getElementById("check-run-stats");
  stats.replaceChildren();
  const runStats = [
    [run.counts.checked, run.counts.requested ? t("已检查 / {count} 个请求", { count: run.counts.requested }) : "已检查", false],
    [run.counts.changed, "有变化", false],
    [run.counts.events, "新事件", false],
  ];
  if (run.counts.baselines) runStats.push([run.counts.baselines, "新基线", false]);
  if (run.counts.blocked) runStats.push([run.counts.blocked, "未能观察", true]);
  if (run.counts.errors) runStats.push([run.counts.errors, "异常", true]);
  runStats.push([checkRunDuration(run.durationMs), "耗时", false]);
  runStats.forEach(([value, label, alert]) => {
    const item = element("span", `check-run-stat${alert ? " is-alert" : ""}`);
    item.append(element("strong", "", value), document.createTextNode(t(label)));
    stats.append(item);
  });
  if (run.publication.requested || run.publication.failed || run.publication.unknown) {
    const publication = element("span", `check-run-stat${run.publication.failed || run.publication.unknown ? " is-alert" : ""}`);
    const issues = [
      run.publication.failed ? t("{count} 失败", { count: run.publication.failed }) : "",
      run.publication.unknown ? t("{count} 待确认", { count: run.publication.unknown }) : "",
    ].filter(Boolean).join(" · ");
    publication.append(
      element("strong", "", `${run.publication.succeeded}/${run.publication.requested}`),
      document.createTextNode(t(issues ? "发布成功 · {issues}" : "发布成功", { issues })),
    );
    stats.append(publication);
  }
  const backlogParts = checkRunBacklogParts(run.publication.backlog);
  if (backlogParts.length) {
    const backlog = run.publication.backlog;
    const total = backlog.succeeded + backlog.failed + backlog.unknown;
    const item = element("span", `check-run-stat${backlog.failed || backlog.unknown ? " is-alert" : ""}`);
    item.append(
      element("strong", "", total),
      document.createTextNode(t("历史投递 · {parts}", { parts: backlogParts.join(" · ") })),
    );
    stats.append(item);
  }

  const pendingCount = Math.max(0, Number(payload.pending_check_run_count) || 0);
  const queue = document.getElementById("check-run-queue");
  const remaining = Math.max(0, pendingCount - (actionablePending ? 1 : 0));
  queue.hidden = !remaining;
  queue.textContent = remaining ? t("另有 {count} 轮待确认", { count: remaining }) : "";
  const acknowledge = document.getElementById("check-run-acknowledge");
  acknowledge.hidden = !actionablePending;
  acknowledge.disabled = state.checkRunAcknowledging;
  acknowledge.textContent = t(state.checkRunAcknowledging ? "正在确认…" : "标为已查看");
  const toggle = document.getElementById("check-run-toggle");
  toggle.setAttribute("aria-expanded", state.checkRunExpanded ? "true" : "false");
  toggle.querySelector("span").textContent = t(state.checkRunExpanded ? "收起结果" : "查看结果");
  renderCheckRunDetails(run);

  if (actionablePending && state.announcedCheckRunId !== run.id) {
    state.announcedCheckRunId = run.id;
    document.getElementById("check-run-announcement").textContent = t(
      "新一轮检查完成，{changed} 个分支有变化，{events} 条新事件。",
      { changed: run.counts.changed, events: run.counts.events },
    );
  }
}

function toggleCheckRunDetails() {
  if (!state.visibleCheckRunId) return;
  state.checkRunExpanded = !state.checkRunExpanded;
  renderCheckRunBanner(state.dashboard);
}

async function acknowledgeCheckRun() {
  if (state.checkRunAcknowledging || !state.visibleCheckRunId) return;
  const runId = String(state.visibleCheckRunId);
  if (!/^[0-9a-f]{32}$/.test(runId)) {
    showToast(t("这轮检查缺少可确认的本地编号，请刷新后重试。"), true);
    return;
  }
  state.checkRunAcknowledging = true;
  renderCheckRunBanner(state.dashboard);
  try {
    await api(`/api/v1/check-runs/${encodeURIComponent(runId)}/acknowledge`, { method: "POST", body: {} });
    showToast(t("已确认本轮检查结果。"), false);
    await loadDashboard();
    if (state.activityScope) await loadActivityFeed({ reset: true });
  } catch (error) {
    showToast(localizedErrorMessage(error, "无法确认本轮检查结果"), true);
  } finally {
    state.checkRunAcknowledging = false;
    if (state.dashboard) renderCheckRunBanner(state.dashboard);
  }
}

function renderDashboard(payload) {
  state.dashboard = payload;
  const availableWatchIds = new Set((payload.watches || []).map(watchBranchId).filter((value) => value !== null));
  state.selectedWatchIds.forEach((branchId) => {
    if (!availableWatchIds.has(branchId)) state.selectedWatchIds.delete(branchId);
  });
  const counts = payload.counts || {};
  document.getElementById("app-version").textContent = `Branch Radar ${payload.app?.version || ""}`;
  document.getElementById("metric-branches").textContent = counts.branches ?? 0;
  document.getElementById("metric-repositories").textContent = t("{count} 个仓库", { count: counts.repositories ?? 0 });
  document.getElementById("metric-errors").textContent = counts.branch_errors ?? 0;
  document.getElementById("metric-deliveries").textContent = counts.pending_deliveries ?? 0;
  document.getElementById("watch-tab-count").textContent = counts.branches ?? 0;

  const errors = Number(counts.branch_errors || 0);
  const branches = Number(counts.branches || 0);
  const pendingDeliveries = Number(counts.pending_deliveries || 0);
  const daemonHealth = payload.daemon_health || {};
  const daemonFailures = Number(daemonHealth.consecutive_failures || 0);
  const overviewStatus = document.getElementById("overview-status");
  const indicator = document.getElementById("live-indicator");
  indicator.classList.toggle("has-alert", errors > 0 || daemonFailures > 0);
  overviewStatus.classList.toggle("has-alert", errors > 0 || daemonFailures > 0 || pendingDeliveries > 0);
  overviewStatus.classList.toggle("is-empty", !branches);
  if (!branches) {
    document.getElementById("overview-kicker").textContent = t("还没有关注分支");
    document.getElementById("overview-headline").textContent = t("添加第一个关注分支");
    document.getElementById("overview-description").textContent = t("从本机项目中选择需要持续观察的分支。");
  } else if (errors) {
    document.getElementById("overview-kicker").textContent = daemonFailures
      ? t("{errors} 个分支异常 · 后台连续失败 {failures} 次", { errors, failures: daemonFailures })
      : t("{errors} 个分支需要处理", { errors });
    document.getElementById("overview-headline").textContent = t(daemonFailures
      ? "分支与后台检查均需处理"
      : "部分分支检查异常");
    document.getElementById("overview-description").textContent = t(daemonFailures
      ? "成功结果已保留；请先查看异常分支，再运行 doctor 检查后台服务。"
      : "成功结果已保留；右侧列出需要处理的分支。");
  } else if (daemonFailures) {
    document.getElementById("overview-kicker").textContent = t("后台检查连续失败 {count} 次", { count: daemonFailures });
    document.getElementById("overview-headline").textContent = t("后台检查需要关注");
    const failedAt = daemonHealth.last_failure_at
      ? t("最近一次失败于{time}。", { time: formatTime(daemonHealth.last_failure_at, true) })
      : "";
    document.getElementById("overview-description").textContent = t(
      "{failedAt}上次成功结果已保留；运行 doctor 可查看本机诊断信息。",
      { failedAt },
    );
  } else if (pendingDeliveries) {
    document.getElementById("overview-kicker").textContent = t("{count} 条变化等待发布", { count: pendingDeliveries });
    document.getElementById("overview-headline").textContent = t("分支监控正常");
    document.getElementById("overview-description").textContent = t("可以前往设置，将待处理事件发布到已关联的飞书文档。");
  } else {
    document.getElementById("overview-kicker").textContent = t("所有关注分支状态正常");
    document.getElementById("overview-headline").textContent = t("监控正常");
    document.getElementById("overview-description").textContent = t(
      "{repositories} 个仓库、{branches} 个分支均已就绪。",
      { repositories: counts.repositories ?? 0, branches },
    );
  }

  const checkedAt = latestTimestamp(
    daemonHealth.last_success_at,
    latestWatchSuccessTime(payload.watches || []),
  );
  document.getElementById("overview-last-check").textContent = checkedAt
    ? formatTime(checkedAt, true)
    : t("尚未检查");
  if (!state.activityScope && !state.activityLoading) {
    state.activityEvents = payload.events || [];
    state.activityHasMore = Boolean(payload.events_has_more);
    state.activityNextBeforeId = payload.events_next_before_id ?? null;
  }
  renderCheckRunBanner(payload);
  const selectedView = currentView();
  if (selectedView === "overview") {
    renderWatchPreview(payload.watches || []);
    renderActivityList();
  } else if (selectedView === "projects") {
    if (state.watchWorkspaceTab === "watched") renderProjectList();
    else if (!hasConfiguredScanRoots() || state.candidates) renderLocalProjects();
  }
  syncContextActions();

  const publishToggle = document.getElementById("check-publish");
  publishToggle.disabled = !payload.settings?.lark_document_configured;
  if (publishToggle.disabled) publishToggle.checked = false;

  const remoteJob = payload.active_job;
  if (remoteJob && jobIsActive(remoteJob)) {
    if (state.activeJob?.id !== remoteJob.id) resumeJob(remoteJob);
  } else if (state.activeJob) {
    // Dashboard is the durable reconciliation source after polling transport
    // failures. If the server has no active job, release stale local locks.
    const releasedJobId = state.activeJob.id;
    state.pollGeneration += 1;
    if (state.pollingTimer) window.clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
    state.activeJob = null;
    state.pollFailures = 0;
    if (state.checkSubmission?.jobId === releasedJobId) state.checkSubmission = null;
    hideTask();
    if (state.watchSubmission) {
      setWatchButtonState("error", state.watchSubmission.branches.length);
    }
  }
}

function emptyState(title, description, actionLabel, action, { rawDescription = false } = {}) {
  const wrapper = element("div", "empty-state");
  wrapper.append(
    element("div", "empty-illustration"),
    uiElement("strong", "", title),
    rawDescription ? element("p", "", description) : uiElement("p", "", description),
  );
  if (actionLabel && action) wrapper.append(button(t(actionLabel), "button button-secondary", action));
  return wrapper;
}

function watchIdentity(watch) {
  return `${watch.repository_id ?? watch.repository}:${watch.id ?? watch.branch}`;
}

const watchBranchId = uiModel.watchBranchId;

function isRecentlyAdded(watch) {
  return state.recentlyAddedWatchIds.has(watchIdentity(watch));
}

// Grouping is pure; the cache is not, so it stays with the render loop that
// depends on the identity of the array it was handed.
function groupWatches(watches) {
  if (groupedWatchSource === watches) return groupedWatchResult;
  groupedWatchSource = watches;
  groupedWatchResult = uiModel.groupWatches(watches);
  return groupedWatchResult;
}

function invalidateWatchGroups() {
  groupedWatchSource = null;
  groupedWatchResult = [];
}

const repositoryDisplayName = uiModel.repositoryDisplayName;
const repositoryGroupKey = uiModel.repositoryGroupKey;

function checkOperationBusy() {
  return Boolean(state.activeJob || state.checkSubmission?.pending);
}

function rememberRepositoryExpansion(key, expanded) {
  state.repositoryExpansion[key] = Boolean(expanded);
  try {
    const entries = Object.entries(state.repositoryExpansion).slice(-200);
    state.repositoryExpansion = Object.fromEntries(entries);
    localStorage.setItem(REPOSITORY_EXPANSION_STORAGE_KEY, JSON.stringify(state.repositoryExpansion));
  } catch (_error) {
    // Disclosure remains usable when local storage is unavailable.
  }
}

function repositoryNeedsAttention(group) {
  return uiModel.repositoryNeedsAttention(group, isRecentlyAdded);
}

function repositoryIsExpanded(group, query) {
  if (query) return true;
  const remembered = state.repositoryExpansion[repositoryGroupKey(group)];
  if (typeof remembered === "boolean") return remembered;
  return repositoryNeedsAttention(group);
}

function syncRepositoryExpansionControl() {
  const control = document.getElementById("toggle-all-repositories");
  if (!control) return;
  const toggles = Array.from(document.querySelectorAll("#project-list .repository-group-toggle"));
  control.hidden = toggles.length < 2;
  if (control.hidden) return;
  const allExpanded = toggles.every((toggle) => toggle.getAttribute("aria-expanded") === "true");
  control.classList.toggle("is-collapse-action", allExpanded);
  control.querySelector("span").textContent = t(allExpanded ? "全部收起" : "全部展开");
  control.setAttribute("aria-label", t(
    allExpanded ? "收起当前 {count} 个仓库" : "展开当前 {count} 个仓库",
    { count: toggles.length },
  ));
}

function ensureBranchRows(branchList) {
  const build = pendingBranchRows.get(branchList);
  if (!build) return;
  pendingBranchRows.delete(branchList);
  branchList.append(build());
}

function setRepositoryDisclosure(section, branchList, toggle, expanded, { persist = true, animate = true } = {}) {
  const nextExpanded = Boolean(expanded);
  if (nextExpanded) ensureBranchRows(branchList);
  const previousAnimation = repositoryDisclosureAnimations.get(branchList);
  previousAnimation?.cancel();
  repositoryDisclosureAnimations.delete(branchList);

  if (persist) rememberRepositoryExpansion(section.dataset.repositoryKey, nextExpanded);
  toggle.setAttribute("aria-expanded", String(nextExpanded));
  toggle.setAttribute("aria-label", t(
    nextExpanded ? "收起仓库 {repository}" : "展开仓库 {repository}",
    { repository: toggle.dataset.repositoryLabel },
  ));
  section.classList.toggle("is-collapsed", !nextExpanded);
  branchList.setAttribute("aria-hidden", String(!nextExpanded));
  branchList.toggleAttribute("inert", !nextExpanded);
  closeBranchActionMenus();

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!animate || reducedMotion || typeof branchList.animate !== "function") {
    branchList.hidden = !nextExpanded;
    syncRepositoryExpansionControl();
    return;
  }

  branchList.hidden = false;
  const fullHeight = branchList.scrollHeight;
  const animation = branchList.animate(
    nextExpanded
      ? [{ height: "0px", opacity: 0 }, { height: `${fullHeight}px`, opacity: 1 }]
      : [{ height: `${fullHeight}px`, opacity: 1 }, { height: "0px", opacity: 0 }],
    { duration: nextExpanded ? 220 : 180, easing: "cubic-bezier(0.2, 0.75, 0.25, 1)" },
  );
  repositoryDisclosureAnimations.set(branchList, animation);
  animation.finished.then(() => {
    if (repositoryDisclosureAnimations.get(branchList) !== animation) return;
    repositoryDisclosureAnimations.delete(branchList);
    branchList.hidden = !nextExpanded;
  }).catch(() => {});
  syncRepositoryExpansionControl();
}

function toggleAllRepositories() {
  const sections = Array.from(document.querySelectorAll("#project-list .repository-group"));
  const shouldExpand = sections.some((section) => {
    return section.querySelector(".repository-group-toggle")?.getAttribute("aria-expanded") !== "true";
  });
  const animate = sections.length <= 6;
  sections.forEach((section) => {
    const toggle = section.querySelector(".repository-group-toggle");
    const branchList = section.querySelector(".repository-branch-list");
    if (!toggle || !branchList) return;
    setRepositoryDisclosure(section, branchList, toggle, shouldExpand, {
      persist: section.dataset.temporaryDisclosure !== "true",
      animate,
    });
  });
}

function renderRepositoryTagNavigation(groups) {
  const navigation = document.getElementById("repository-tag-navigation");
  const switcher = document.getElementById("repository-tag-switcher");
  const tagCounts = new Map();
  groups.forEach((group) => {
    group.tags.forEach((tag) => {
      const key = tag.toLocaleLowerCase("zh-CN");
      const current = tagCounts.get(key) || { label: tag, count: 0 };
      current.count += 1;
      tagCounts.set(key, current);
    });
  });
  const entries = Array.from(tagCounts.values()).sort((left, right) => {
    return right.count - left.count || left.label.localeCompare(right.label, "zh-CN");
  });
  if (!entries.length) {
    state.selectedRepositoryTag = null;
    navigation.hidden = true;
    switcher.replaceChildren();
    return;
  }

  const hasUntagged = groups.some((group) => !group.tags.length);
  const values = [null, ...entries.map((entry) => entry.label), ...(hasUntagged ? ["__untagged__"] : [])];
  if (!values.includes(state.selectedRepositoryTag)) state.selectedRepositoryTag = null;
  const primaryLimit = window.matchMedia("(max-width: 560px)").matches
    ? 2
    : window.matchMedia("(max-width: 1180px)").matches ? 3 : 5;
  const primaryValues = entries.slice(0, primaryLimit).map((entry) => entry.label);
  if (state.selectedRepositoryTag && state.selectedRepositoryTag !== "__untagged__" && !primaryValues.includes(state.selectedRepositoryTag)) {
    if (primaryValues.length === primaryLimit) primaryValues[primaryLimit - 1] = state.selectedRepositoryTag;
    else primaryValues.push(state.selectedRepositoryTag);
  }
  const overflowValues = entries.map((entry) => entry.label).filter((tag) => !primaryValues.includes(tag));
  if (hasUntagged) overflowValues.push("__untagged__");
  const fragment = document.createDocumentFragment();

  const makeFilter = (value, className = "repository-tag-filter") => {
    const label = value === null ? t("全部") : value === "__untagged__" ? t("未分类") : value;
    const control = button(label, className, () => {
      state.selectedRepositoryTag = value;
      renderProjectList();
    });
    control.setAttribute("aria-pressed", state.selectedRepositoryTag === value ? "true" : "false");
    control.dataset.tagSearch = label.toLocaleLowerCase("zh-CN");
    return control;
  };

  fragment.append(makeFilter(null));
  primaryValues.forEach((value) => fragment.append(makeFilter(value)));
  if (overflowValues.length) {
    const more = element("details", "repository-tag-more");
    const summary = element("summary", "repository-tag-more-trigger");
    const selectedInMore = overflowValues.includes(state.selectedRepositoryTag);
    summary.classList.toggle("is-active", selectedInMore);
    summary.append(createIcon("more"), element("span", "", selectedInMore ? state.selectedRepositoryTag === "__untagged__" ? t("未分类") : state.selectedRepositoryTag : t("更多标签")), element("small", "", String(overflowValues.length)));
    summary.setAttribute("aria-label", selectedInMore
      ? t("当前标签：{tag}；打开更多标签", { tag: summary.querySelector("span").textContent })
      : t("打开更多标签，共 {count} 个", { count: overflowValues.length }));
    summary.setAttribute("role", "button");
    summary.setAttribute("aria-expanded", "false");
    const popover = element("div", "repository-tag-popover");
    const search = document.createElement("input");
    search.type = "search";
    search.className = "repository-tag-search";
    search.placeholder = t("搜索标签");
    search.setAttribute("aria-label", t("搜索更多标签"));
    const options = element("div", "repository-tag-more-options");
    overflowValues.forEach((value) => options.append(makeFilter(value, "repository-tag-menu-option")));
    const empty = uiElement("p", "repository-tag-menu-empty", "没有匹配的标签");
    empty.hidden = true;
    search.addEventListener("input", () => {
      const query = search.value.trim().toLocaleLowerCase("zh-CN");
      let visible = 0;
      options.querySelectorAll("[data-tag-search]").forEach((control) => {
        control.hidden = Boolean(query) && !control.dataset.tagSearch.includes(query);
        if (!control.hidden) visible += 1;
      });
      empty.hidden = visible > 0;
    });
    more.addEventListener("toggle", () => {
      summary.setAttribute("aria-expanded", more.open ? "true" : "false");
      if (more.open) window.requestAnimationFrame(() => search.focus());
    });
    popover.append(search, options, empty);
    more.append(summary, popover);
    fragment.append(more);
  }
  switcher.replaceChildren(fragment);
  navigation.hidden = false;
}

const groupWatchStatus = uiModel.groupWatchStatus;
const latestTimestamp = uiModel.latestTimestamp;
const recentEventWatchKeys = uiModel.recentEventWatchKeys;

function latestWatchTime(watches) {
  return uiModel.latestValue(watches, "last_checked_at");
}

function latestWatchSuccessTime(watches) {
  return uiModel.latestValue(watches, "last_success_at");
}

function watchAttentionReason(watch, recentKeys) {
  return uiModel.watchAttentionReason(watch, recentKeys, isRecentlyAdded);
}

function makePreviewRepository(group, recentKeys) {
  const section = element("section", "watch-preview-group");
  const heading = element("div", "watch-preview-repository");
  const [label, modifier] = groupWatchStatus(group.watches);
  const displayLabel = label === "正常" ? "最近变化" : label;
  const previewName = group.alias ? `${group.alias} · ${group.name}` : group.name;
  heading.append(
    element("strong", "", previewName),
    element("span", modifier, t("{count} 个分支 · {status}", {
      count: group.watches.length,
      status: t(displayLabel),
    })),
  );
  const branches = element("div", "watch-preview-branches");
  group.watches.forEach((watch) => {
    const action = button("", "watch-preview-branch", () => openActivityScope(watch));
    const name = element("strong", "", watch.branch);
    const reason = watchAttentionReason(watch, recentKeys);
    const detail = reason ? t(reason) : formatTime(watch.last_checked_at, true);
    const meta = element("span", `watch-preview-meta${reason && reason !== "最近变化" ? " is-alert" : ""}`, detail);
    action.append(name, meta);
    action.setAttribute("aria-label", t("查看 {repository} 的 {branch} 分支动态", {
      repository: repositoryDisplayName(group), branch: watch.branch,
    }));
    branches.append(action);
  });
  section.append(heading, branches);
  return section;
}

function renderWatchPreview(watches) {
  const container = document.getElementById("watch-preview");
  if (!watches.length) {
    container.replaceChildren(emptyState("还没有关注分支", "从本机 Git 项目中选择分支，建立基线后就会出现在这里。", "查看本地项目", openProjectsView));
    return;
  }
  const recentKeys = recentEventWatchKeys(state.dashboard?.events || []);
  const attentionWatches = watches.filter((watch) => watchAttentionReason(watch, recentKeys));
  const pendingDeliveries = Number(state.dashboard?.counts?.pending_deliveries || 0);
  const fragment = document.createDocumentFragment();
  if (pendingDeliveries) {
    const delivery = button("", "attention-delivery", () => { location.hash = "settings"; });
    delivery.append(createIcon("doc"));
    const copy = element("span", "");
    copy.append(
      element("strong", "", t("{count} 条变化等待发布", { count: pendingDeliveries })),
      uiElement("small", "", "前往设置并发布到关联文档"),
    );
    delivery.append(copy, createIcon("arrow"));
    fragment.append(delivery);
  }
  if (!attentionWatches.length && !pendingDeliveries) {
    const clear = element("div", "attention-clear-state");
    const icon = element("span", "attention-clear-icon");
    icon.append(createIcon("check"));
    const copy = element("span", "attention-clear-copy");
    copy.append(
      uiElement("strong", "", "当前无需处理"),
      element("small", "", t("{count} 个关注分支状态正常", { count: watches.length })),
    );
    clear.append(icon, copy);
    container.replaceChildren(clear);
    return;
  }
  const groups = groupWatches(attentionWatches);
  groups.slice(0, 4).forEach((group) => fragment.append(makePreviewRepository(group, recentKeys)));
  if (groups.length > 4) {
    const more = element("p", "watch-preview-more", t("另有 {count} 个仓库需要关注，请在分支页查看。", { count: groups.length - 4 }));
    fragment.append(more);
  }
  container.replaceChildren(fragment);
}

function closeBranchActionMenus(except = null) {
  document.querySelectorAll(".branch-action-menu[open]").forEach((menu) => {
    if (menu !== except) menu.open = false;
  });
}

function makeBranchActionMenu(group, watch) {
  const branchId = watchBranchId(watch);
  const menu = element("details", "branch-action-menu");
  const trigger = element("summary", "icon-button branch-action-trigger");
  trigger.append(createIcon("more"));
  trigger.setAttribute("aria-label", t("打开 {branch} 分支操作", { branch: watch.branch }));
  trigger.setAttribute("role", "button");
  trigger.setAttribute("aria-expanded", "false");
  const content = element("div", "branch-action-popover");
  content.setAttribute("aria-label", t("{branch} 分支操作", { branch: watch.branch }));
  const activity = button(t("查看动态"), "branch-action-item", () => {
    menu.open = false;
    openActivityScope(watch);
  });
  activity.prepend(createIcon("activity"));
  const check = button(t("立即检查"), "branch-action-item", () => {
    menu.open = false;
    if (branchId !== null) submitSafeCheck([branchId], "branch");
  });
  check.disabled = branchId === null || checkOperationBusy();
  check.dataset.checkControl = "";
  check.prepend(createIcon("refresh"));
  const divider = element("span", "branch-action-divider");
  divider.setAttribute("aria-hidden", "true");
  const unwatch = button(t("停止关注"), "branch-action-item is-danger", () => {
    menu.open = false;
    openUnwatchConfirmation(group, watch, trigger);
  });
  unwatch.disabled = branchId === null || checkOperationBusy();
  unwatch.dataset.checkControl = "";
  unwatch.prepend(createIcon("close"));
  content.append(activity, check, divider, unwatch);
  menu.append(trigger, content);
  menu.addEventListener("toggle", () => {
    trigger.setAttribute("aria-expanded", menu.open ? "true" : "false");
    if (menu.open) closeBranchActionMenus(menu);
  });
  return menu;
}

function makeRepositoryBranch(group, watch) {
  const branchId = watchBranchId(watch);
  const selected = branchId !== null && state.selectedWatchIds.has(branchId);
  const row = element("div", `repository-branch-row${isRecentlyAdded(watch) ? " is-new" : ""}${selected ? " is-selected" : ""}`);
  const selection = element("label", "repository-branch-select");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = selected;
  checkbox.disabled = branchId === null || checkOperationBusy();
  checkbox.dataset.checkControl = "";
  if (branchId !== null) checkbox.dataset.branchId = String(branchId);
  checkbox.setAttribute("aria-label", t("选择 {repository} 的 {branch} 分支", {
    repository: watch.repository, branch: watch.branch,
  }));
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) state.selectedWatchIds.add(branchId);
    else state.selectedWatchIds.delete(branchId);
    row.classList.toggle("is-selected", checkbox.checked);
    updateSelectedCheckControls();
  });
  selection.append(checkbox, element("span", "checkbox-mark"));
  const identity = element("div", "repository-branch-name");
  const branchName = element("span", "", watch.branch);
  if (isRecentlyAdded(watch)) branchName.append(uiElement("small", "new-watch-badge", "刚刚添加"));
  identity.append(branchName);
  const [label, modifier] = branchStatus(watch);
  const facts = element("div", "repository-branch-facts");
  const time = element("time", "time-copy", formatTime(watch.last_checked_at, true));
  if (watch.last_checked_at) time.dateTime = watch.last_checked_at;
  facts.append(statusChip(label, modifier), element("code", "sha", shortSha(watch.sha)), time);
  const action = makeBranchActionMenu(group, watch);
  row.append(selection, identity, facts, action);
  if (watch.error) row.append(element("p", "inline-error", watch.error));
  return row;
}

function renderProjectList() {
  if (!state.dashboard) return;
  const queryInput = document.getElementById("project-search");
  const query = queryInput ? queryInput.value.trim().toLowerCase() : "";
  const allWatches = state.dashboard.watches || [];
  const allGroups = groupWatches(allWatches);
  const allGroupsByKey = new Map(allGroups.map((group) => [repositoryGroupKey(group), group]));
  renderRepositoryTagNavigation(allGroups);
  const groups = allGroups.filter((group) => {
    if (state.selectedRepositoryTag === "__untagged__") return !group.tags.length;
    if (state.selectedRepositoryTag) return group.tags.includes(state.selectedRepositoryTag);
    return true;
  }).map((group) => {
    const repositoryText = [group.name, group.alias, ...group.tags].filter(Boolean).join(" ").toLowerCase();
    if (!query || repositoryText.includes(query)) return group;
    return { ...group, watches: group.watches.filter((watch) => watch.branch.toLowerCase().includes(query)) };
  }).filter((group) => group.watches.length);
  const branchCount = groups.reduce((total, group) => total + group.watches.length, 0);
  state.visibleWatchIds = groups.flatMap((group) => (
    group.watches.map(watchBranchId).filter((value) => value !== null)
  ));
  document.getElementById("project-count").textContent = t(
    "{repositories} 个仓库 · {branches} 个分支",
    { repositories: groups.length, branches: branchCount },
  );
  const container = document.getElementById("project-list");
  if (!groups.length) {
    const tagSelected = state.selectedRepositoryTag !== null;
    const message = !allWatches.length ? "还没有关注分支" : tagSelected ? "这个标签下还没有仓库" : "没有符合筛选条件的仓库或分支";
    const description = query
      ? "换一个别名、原项目名、标签或分支名，或者清除搜索条件。"
      : tagSelected
        ? "选择其他标签，或返回查看全部仓库。"
        : "先从本地项目中选择仓库和分支。";
    const actionLabel = query ? "清除搜索" : tagSelected ? "查看全部" : "查看本地项目";
    const action = query ? () => {
      queryInput.value = "";
      renderProjectList();
      queryInput.focus();
    } : tagSelected ? () => {
      state.selectedRepositoryTag = null;
      renderProjectList();
    } : openProjectsView;
    container.replaceChildren(emptyState(message, description, actionLabel, action));
    syncRepositoryExpansionControl();
    updateSelectedCheckControls();
    return;
  }
  const fragment = document.createDocumentFragment();
  groups.forEach((group, groupIndex) => {
    const fullGroup = allGroupsByKey.get(repositoryGroupKey(group)) || group;
    const expanded = repositoryIsExpanded(group, query);
    const repositoryLabel = repositoryDisplayName(group);
    const repositoryDomKey = group.id ?? groupIndex;
    const section = element("section", `repository-group${expanded ? "" : " is-collapsed"}${group.watches.length > 1 ? " is-multi" : ""}`);
    section.dataset.repositoryKey = repositoryGroupKey(group);
    section.dataset.temporaryDisclosure = query ? "true" : "false";
    section.setAttribute("aria-labelledby", `repository-${repositoryDomKey}`);
    const header = element("header", "repository-group-header");
    const disclosure = button("", "repository-group-toggle", () => {
      const nextExpanded = disclosure.getAttribute("aria-expanded") !== "true";
      setRepositoryDisclosure(section, branchList, disclosure, nextExpanded, { persist: !query });
    });
    disclosure.dataset.repositoryLabel = repositoryLabel;
    disclosure.setAttribute("aria-expanded", String(expanded));
    disclosure.setAttribute("aria-controls", `repository-branches-${repositoryDomKey}`);
    disclosure.setAttribute("aria-label", t(
      expanded ? "收起仓库 {repository}" : "展开仓库 {repository}",
      { repository: repositoryLabel },
    ));
    const disclosureIcon = element("span", "repository-disclosure-icon");
    disclosureIcon.append(createIcon("chevron"));
    const identity = element("span", "repository-group-title");
    const titleLine = element("span", "repository-group-title-line");
    const title = element("strong", "", repositoryLabel);
    title.id = `repository-${repositoryDomKey}`;
    titleLine.append(title);
    if (group.tags.length) {
      const tags = element("span", "repository-tags");
      group.tags.forEach((tag) => tags.append(element("span", "repository-tag-chip", tag)));
      titleLine.append(tags);
    }
    const originalCopy = group.alias
      ? t("{repository} · {count} 个关注分支", { repository: group.name, count: group.watches.length })
      : t("{count} 个关注分支", { count: group.watches.length });
    identity.append(titleLine, element("span", "repository-original-copy", originalCopy));
    const [label, modifier] = groupWatchStatus(group.watches);
    const checked = latestWatchTime(group.watches);
    const meta = element("div", "repository-group-meta");
    const repositoryId = Number(fullGroup.id);
    const canCheckRepository = Number.isSafeInteger(repositoryId) && repositoryId > 0;
    const repositoryCheck = button(t("检查仓库"), "button button-quiet button-compact repository-check-button", () => {
      if (canCheckRepository) submitRepositoryCheck(repositoryId);
    });
    const repositoryCheckLabel = canCheckRepository
      ? t("检查仓库 {repository} 的全部 {count} 个关注分支", {
        repository: repositoryLabel,
        count: fullGroup.watches.length,
      })
      : t("仓库 {repository} 缺少本地编号，暂时无法检查", { repository: repositoryLabel });
    repositoryCheck.prepend(createIcon("refresh"));
    repositoryCheck.disabled = !canCheckRepository || checkOperationBusy();
    repositoryCheck.dataset.checkControl = "";
    repositoryCheck.dataset.repositoryId = canCheckRepository ? String(repositoryId) : "";
    repositoryCheck.setAttribute("aria-label", repositoryCheckLabel);
    repositoryCheck.title = repositoryCheckLabel;
    const profile = button(t("编辑资料"), "button button-quiet button-compact repository-profile-button", () => openRepositoryProfile(group));
    profile.prepend(createIcon("edit"));
    profile.setAttribute("aria-label", t("编辑 {repository} 的别名和标签", { repository: repositoryLabel }));
    const summary = element("span", "repository-group-summary");
    summary.append(
      statusChip(label, modifier),
      element("span", "", checked ? t("检查于 {time}", { time: formatTime(checked, true) }) : t("尚未检查")),
    );
    const actions = element("div", "repository-group-actions");
    actions.append(repositoryCheck, profile);
    meta.append(summary, actions);
    disclosure.append(disclosureIcon, identity);
    header.append(disclosure, meta);
    const branchList = element("div", "repository-branch-list");
    branchList.id = `repository-branches-${repositoryDomKey}`;
    branchList.hidden = !expanded;
    branchList.setAttribute("aria-hidden", String(!expanded));
    branchList.toggleAttribute("inert", !expanded);
    const buildBranchRows = () => {
      const rows = document.createDocumentFragment();
      group.watches.forEach((watch) => rows.append(makeRepositoryBranch(group, watch)));
      return rows;
    };
    if (expanded) branchList.append(buildBranchRows());
    else pendingBranchRows.set(branchList, buildBranchRows);
    section.append(header, branchList);
    fragment.append(section);
  });
  container.replaceChildren(fragment);
  syncRepositoryExpansionControl();
  updateSelectedCheckControls();
}

function openRepositoryProfile(group) {
  if (!Number.isSafeInteger(Number(group.id)) || Number(group.id) <= 0) {
    showToast(t("当前仓库缺少可编辑的本地编号"), true);
    return;
  }
  state.repositoryProfileId = Number(group.id);
  document.getElementById("repository-profile-original-name").textContent = group.name;
  document.getElementById("repository-profile-alias").value = group.alias || "";
  document.getElementById("repository-profile-tags").value = group.tags.join("、");
  document.getElementById("repository-profile-error").textContent = "";
  renderRepositoryTagSuggestions();
  openDialog("repository-profile-dialog");
  window.requestAnimationFrame(() => document.getElementById("repository-profile-alias").focus());
}

function openUnwatchConfirmation(group, watch, returnFocus) {
  const branchId = watchBranchId(watch);
  if (branchId === null) {
    showToast(t("当前分支缺少可管理的本地编号"), true);
    return;
  }
  const repository = repositoryDisplayName(group);
  state.pendingUnwatch = {
    branchId,
    branch: watch.branch,
    repository,
    originalRepository: group.name,
    hasAlias: Boolean(group.alias),
    submitting: false,
    returnFocus,
  };
  renderUnwatchLabels();
  document.getElementById("unwatch-error").textContent = "";
  setUnwatchBusy(false);
  openDialog("unwatch-dialog");
  window.requestAnimationFrame(() => document.getElementById("cancel-unwatch-button").focus());
}

function renderUnwatchLabels() {
  const pending = state.pendingUnwatch;
  if (!pending) return;
  document.getElementById("unwatch-branch-label").textContent = pending.branch;
  document.getElementById("unwatch-repository-label").textContent = pending.hasAlias
    ? t("{repository} · 原名 {original}", {
      repository: pending.repository,
      original: pending.originalRepository,
    })
    : pending.originalRepository;
}

function setUnwatchBusy(busy) {
  const dialog = document.getElementById("unwatch-dialog");
  const confirm = document.getElementById("confirm-unwatch-button");
  dialog.setAttribute("aria-busy", busy ? "true" : "false");
  confirm.disabled = busy;
  confirm.textContent = t(busy ? "正在停止…" : "停止关注");
  dialog.querySelectorAll('[data-close-dialog="unwatch-dialog"]').forEach((control) => {
    control.disabled = busy;
  });
}

async function submitUnwatch(event) {
  event.preventDefault();
  const pending = state.pendingUnwatch;
  if (!pending || pending.submitting) return;
  const error = document.getElementById("unwatch-error");
  pending.submitting = true;
  setUnwatchBusy(true);
  error.textContent = "";
  try {
    await api(`/api/v1/watches/${pending.branchId}`, {
      method: "DELETE",
      body: {},
    });
  } catch (requestError) {
    pending.submitting = false;
    setUnwatchBusy(false);
    error.textContent = localizedErrorMessage(requestError, "停止关注失败，请稍后重试");
    return;
  }

  state.selectedWatchIds.delete(pending.branchId);
  closeDialog("unwatch-dialog");
  document.getElementById("project-count")?.focus();
  showToast(t("已停止关注 {repository} / {branch}", {
    repository: pending.repository,
    branch: pending.branch,
  }));
  try {
    await loadDashboard();
  } catch (_refreshError) {
    showToast(t("已停止关注，但状态列表刷新失败；请稍后刷新页面。"), true);
  }
}

const parseRepositoryTags = uiModel.parseRepositoryTags;

function availableRepositoryTags() {
  const tags = [];
  const seen = new Set();
  (state.dashboard?.watches || []).forEach((watch) => {
    (Array.isArray(watch.repository_tags) ? watch.repository_tags : []).forEach((tag) => {
      if (typeof tag !== "string" || !tag.trim()) return;
      const normalized = tag.trim();
      const key = normalized.toLocaleLowerCase("zh-CN");
      if (seen.has(key)) return;
      seen.add(key);
      tags.push(normalized);
    });
  });
  return tags.sort((left, right) => left.localeCompare(right, "zh-CN"));
}

function selectedRepositoryTagKeys() {
  return new Set(
    parseRepositoryTags(document.getElementById("repository-profile-tags").value)
      .map((tag) => tag.toLocaleLowerCase("zh-CN")),
  );
}

function syncRepositoryTagSuggestions() {
  const selected = selectedRepositoryTagKeys();
  document.querySelectorAll("#repository-tag-options [data-repository-tag]").forEach((control) => {
    const active = selected.has(control.dataset.repositoryTag.toLocaleLowerCase("zh-CN"));
    control.classList.toggle("is-selected", active);
    control.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function toggleRepositoryTag(tag) {
  const input = document.getElementById("repository-profile-tags");
  const error = document.getElementById("repository-profile-error");
  const tags = parseRepositoryTags(input.value);
  const key = tag.toLocaleLowerCase("zh-CN");
  const existingIndex = tags.findIndex((item) => item.toLocaleLowerCase("zh-CN") === key);
  if (existingIndex >= 0) {
    tags.splice(existingIndex, 1);
  } else if (tags.length >= 8) {
    error.textContent = t("最多选择 8 个标签；请先移除一个标签。");
    input.focus();
    return;
  } else {
    tags.push(tag);
  }
  input.value = tags.join("、");
  error.textContent = "";
  syncRepositoryTagSuggestions();
}

function renderRepositoryTagSuggestions() {
  const wrapper = document.getElementById("repository-tag-suggestions");
  const container = document.getElementById("repository-tag-options");
  const tags = availableRepositoryTags();
  wrapper.hidden = tags.length === 0;
  if (!tags.length) {
    container.replaceChildren();
    return;
  }
  const fragment = document.createDocumentFragment();
  tags.forEach((tag) => {
    const control = button(tag, "repository-tag-option", () => toggleRepositoryTag(tag));
    control.dataset.repositoryTag = tag;
    control.setAttribute("aria-pressed", "false");
    control.prepend(createIcon("check"));
    fragment.append(control);
  });
  container.replaceChildren(fragment);
  syncRepositoryTagSuggestions();
}

async function saveRepositoryProfile(event) {
  event.preventDefault();
  const repositoryId = state.repositoryProfileId;
  if (!Number.isSafeInteger(repositoryId) || repositoryId <= 0) return;
  const alias = document.getElementById("repository-profile-alias").value.trim();
  const tags = parseRepositoryTags(document.getElementById("repository-profile-tags").value);
  const error = document.getElementById("repository-profile-error");
  const save = document.getElementById("repository-profile-save");
  if (tags.length > 8 || tags.some((tag) => tag.length > 24)) {
    error.textContent = t("最多设置 8 个标签，每个标签不超过 24 个字符。");
    document.getElementById("repository-profile-tags").focus();
    return;
  }

  error.textContent = "";
  save.disabled = true;
  save.textContent = t("保存中…");
  try {
    const result = await api("/api/v1/repositories/profile", {
      method: "POST",
      body: { repository_id: repositoryId, alias, tags },
    });
    const profile = result.repository || {};
    (state.dashboard?.watches || []).forEach((watch) => {
      if (Number(watch.repository_id) !== repositoryId) return;
      watch.repository_alias = profile.alias || null;
      watch.repository_tags = Array.isArray(profile.tags) ? profile.tags : [];
    });
    invalidateWatchGroups();
    renderProjectList();
    closeDialog("repository-profile-dialog");
    showToast(t(profile.alias || profile.tags?.length ? "仓库资料已保存" : "已恢复原始仓库名称"));
  } catch (requestError) {
    error.textContent = localizedErrorMessage(requestError, "仓库资料保存失败");
  } finally {
    save.disabled = false;
    save.textContent = t("保存资料");
  }
}

function visibleWatchIds() {
  // Reads the filtered model rather than the DOM: a collapsed repository is
  // still part of the current list even though its rows are not mounted yet.
  return state.visibleWatchIds;
}

function updateSelectedCheckControls() {
  const count = state.selectedWatchIds.size;
  const button = document.getElementById("check-selected-button");
  const label = button?.querySelector("span");
  if (label) label.textContent = count ? t("检查 {count} 个分支", { count }) : t("检查已选");
  if (button) button.disabled = !count || checkOperationBusy();
  const visibleIds = visibleWatchIds();
  const selectVisible = document.getElementById("select-visible-watches");
  if (selectVisible) {
    const allSelected = visibleIds.length > 0 && visibleIds.every((id) => state.selectedWatchIds.has(id));
    selectVisible.textContent = t(allSelected ? "取消选择当前" : "选择当前列表");
    selectVisible.disabled = !visibleIds.length || checkOperationBusy();
  }
  document.querySelectorAll(".repository-check-button").forEach((control) => {
    control.disabled = !control.dataset.repositoryId || checkOperationBusy();
  });
  syncBatchActionBar();
  syncContextActions();
}

function syncBatchActionBar() {
  const bar = document.getElementById("batch-action-bar");
  if (!bar) return;
  const count = state.selectedWatchIds.size;
  document.getElementById("batch-selected-count").textContent = String(count);
  bar.hidden = !(count && currentView() === "projects" && state.watchWorkspaceTab === "watched");
}

function clearSelectedWatches() {
  if (!state.selectedWatchIds.size) return;
  state.selectedWatchIds.clear();
  renderProjectList();
}

function toggleVisibleWatches() {
  const visibleIds = visibleWatchIds();
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => state.selectedWatchIds.has(id));
  visibleIds.forEach((id) => {
    if (allSelected) state.selectedWatchIds.delete(id);
    else state.selectedWatchIds.add(id);
  });
  if (!allSelected) {
    document.querySelectorAll("#project-list .repository-group").forEach((section) => {
      if (section.dataset.temporaryDisclosure !== "true") {
        rememberRepositoryExpansion(section.dataset.repositoryKey, true);
      }
    });
  }
  renderProjectList();
}

function eventKindLabel(event) {
  return kindCopy[event?.kind] ? t(kindCopy[event.kind]) : (event?.kind || t("变化"));
}

function localizedEventTitle(event) {
  const raw = String(event?.summary?.title || "").trim();
  if (!raw) return t("{repository} 分支变化", { repository: event?.repository || t("仓库") });
  const commitSuffix = " · 新增提交";
  if (raw.endsWith(commitSuffix)) return `${raw.slice(0, -commitSuffix.length)} · ${t("新增提交")}`;
  return t(raw);
}

function localizedEventCategories(metadata) {
  return metadata.categories.map((item) => {
    const known = categoryCopy[item.toLowerCase()];
    return known ? t(known) : item;
  });
}

function eventNeedsAttention(event, metadata = uiModel.normalizeEventMetadata(event)) {
  return ATTENTION_EVENT_KINDS.has(event?.kind)
    || metadata.signals.length > 0
    || event?.analysis_complete === false;
}

function eventAuthorsLabel(metadata) {
  if (!metadata.authors.length) return "—";
  if (metadata.authors.length <= 2) return metadata.authors.join(i18n.locale() === "en" ? ", " : "、");
  return t("{authors} 等 {count} 人", {
    authors: metadata.authors.slice(0, 2).join(i18n.locale() === "en" ? ", " : "、"),
    count: metadata.authors.length,
  });
}

function timelineTimestampMs(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function eventTimelineView(event, item = null) {
  const raw = event?.timeline && typeof event.timeline === "object" ? event.timeline : {};
  const detectedTimeMs = timelineTimestampMs(item?.detectedTimeMs)
    ?? timelineTimestampMs(event?.detected_at);
  const tipCommittedAtMs = timelineTimestampMs(item?.tipCommittedAtMs)
    ?? timelineTimestampMs(raw.tip_committed_at)
    ?? ((item?.timeBasis === "tip_commit" || raw.basis === "tip_commit")
      ? timelineTimestampMs(item?.timeMs ?? item?.plottedTimeMs)
      : null);
  const firstCommittedAtMs = timelineTimestampMs(item?.firstCommittedAtMs)
    ?? timelineTimestampMs(raw.first_committed_at)
    ?? tipCommittedAtMs;
  const lastCommittedAtMs = timelineTimestampMs(item?.lastCommittedAtMs)
    ?? timelineTimestampMs(raw.last_committed_at)
    ?? tipCommittedAtMs;
  const requestedBasis = item?.timeBasis ?? raw.basis;
  const basis = requestedBasis === "tip_commit" && tipCommittedAtMs !== null ? "tip_commit" : "detected";
  const primaryTimeMs = basis === "tip_commit" ? tipCommittedAtMs : detectedTimeMs;
  const checkIsMateriallyDifferent = basis === "tip_commit"
    && detectedTimeMs !== null
    && primaryTimeMs !== null
    && Math.abs(detectedTimeMs - primaryTimeMs) >= 60_000;
  return {
    basis,
    primaryTimeMs,
    detectedTimeMs,
    firstCommittedAtMs,
    lastCommittedAtMs,
    rangeComplete: item?.commitRangeComplete ?? raw.range_complete ?? false,
    timeClamped: Boolean(item?.timeClamped),
    outsideRequestedRange: Boolean(item?.outsideRequestedRange),
    checkIsMateriallyDifferent,
  };
}

function timelineTimeCopy(timeline, { relative = false } = {}) {
  const primaryTime = formatTime(timeline.primaryTimeMs, relative);
  return timeline.basis === "tip_commit"
    ? t("提交于 {time}", { time: primaryTime })
    : t("检查于 {time}", { time: primaryTime });
}

function timelineSecondaryCopy(timeline, { relative = false } = {}) {
  if (!timeline.checkIsMateriallyDifferent) return "";
  return t("检查于 {time}", { time: formatTime(timeline.detectedTimeMs, relative) });
}

function timelineAriaParts(event, item = null) {
  const timeline = eventTimelineView(event, item);
  return [
    timelineTimeCopy(timeline),
    timelineSecondaryCopy(timeline),
    timeline.basis === "detected" ? t("未取得可靠提交时间") : "",
    timeline.outsideRequestedRange ? t("提交时间不在所选发现范围内") : "",
    timeline.timeClamped ? t("提交时间在当前画布范围外") : "",
  ].filter(Boolean);
}

function eventSignalAriaLabel(event, metadata, isNew, item = null) {
  const title = localizedEventTitle(event);
  const categories = localizedEventCategories(metadata);
  return [
    t(isNew ? "本轮新事件" : "事件"),
    title,
    t("{repository} {branch}", { repository: event.repository || t("仓库"), branch: event.branch || t("分支") }),
    eventKindLabel(event),
    ...timelineAriaParts(event, item),
    categories.length ? t("分类 {categories}", { categories: categories.join(i18n.locale() === "en" ? ", " : "、") }) : "",
    metadata.signals.length ? t("高危信号 {signals}", { signals: metadata.signals.join(i18n.locale() === "en" ? "; " : "；") }) : "",
    metadata.authors.length ? t("作者 {authors}", { authors: metadata.authors.join(i18n.locale() === "en" ? ", " : "、") }) : "",
    metadata.size.total ? t("变化体量{size}，{total} 行", { size: t(metadata.size.label), total: metadata.size.total }) : "",
    event.analysis_complete === false ? t("证据不完整") : "",
  ].filter(Boolean).join(i18n.locale() === "en" ? ", " : "，");
}

function formatSignalTick(timestamp, rangeHours) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return t("时间未知");
  if (rangeHours > 24) {
    return localeFormatter("signal-day", (locale) => new Intl.DateTimeFormat(locale, {
      month: "2-digit",
      day: "2-digit",
    })).format(date);
  }
  return localeFormatter("signal-time", (locale) => new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
  })).format(date);
}

function signalFieldTicks(model) {
  const span = Math.max(1, model.endMs - model.startMs);
  return [0, 1 / 3, 2 / 3, 1].map((ratio) => ({
    ratio,
    label: ratio === 1 ? t("现在") : formatSignalTick(model.startMs + (span * ratio), model.rangeHours),
  }));
}

function activityEventById(eventId) {
  return (state.activityEvents || []).find((event) => String(event.id) === String(eventId)) || null;
}

function renderSignalInspector(event, inspector = document.getElementById("signal-field-inspector")) {
  if (!inspector || !event) return;
  const metadata = uiModel.normalizeEventMetadata(event);
  const categories = localizedEventCategories(metadata);
  const risk = eventNeedsAttention(event, metadata);
  inspector.classList.toggle("has-alert", risk);
  inspector.replaceChildren();

  const header = element("div", "signal-inspector-header");
  const kind = element("span", `signal-inspector-kind${risk ? " has-alert" : ""}`, eventKindLabel(event));
  const time = element("time", "", formatTime(event.detected_at, true));
  if (event.detected_at) time.dateTime = event.detected_at;
  header.append(kind, time);

  const main = element("div", "signal-inspector-main");
  const copy = element("div", "signal-inspector-copy");
  copy.append(
    element("code", "", `${event.repository || t("仓库")} / ${event.branch || t("分支")}`),
    element("h3", "", event.summary?.title || t("{repository} 分支变化", { repository: event.repository || t("仓库") })),
    element("p", "", event.summary?.overview || t("查看本次分支变化详情。")),
  );
  const chips = element("div", "signal-inspector-chips");
  categories.slice(0, 4).forEach((category) => chips.append(element("span", "signal-chip", category)));
  metadata.signals.slice(0, 2).forEach((signal) => chips.append(element("span", "signal-chip is-alert", signal)));
  if (event.analysis_complete === false) chips.append(uiElement("span", "signal-chip is-alert", "证据不完整"));
  if (chips.childElementCount) copy.append(chips);

  const facts = element("div", "signal-inspector-facts");
  const author = element("span");
  author.append(uiElement("small", "", "作者"), element("strong", "", eventAuthorsLabel(metadata)));
  const volume = element("span");
  const volumeCopy = metadata.size.total
    ? t(metadata.stats.files ? "{files} 个文件 · {total} 行变化" : "{total} 行变化", {
      files: metadata.stats.files,
      total: metadata.size.total,
    })
    : "—";
  volume.append(uiElement("small", "", "体量"), element("strong", "", volumeCopy));
  const detail = uiElement("button", "button button-secondary signal-detail-button", "查看完整分析");
  detail.type = "button";
  detail.append(createIcon("arrow"));
  detail.addEventListener("click", () => openEvent(event.id));
  facts.append(author, volume, detail);
  main.append(copy, facts);
  inspector.append(header, main);
}

function selectActivitySignal(eventId) {
  const event = activityEventById(eventId);
  if (!event) return;
  state.selectedActivityEventId = String(event.id);
  document.querySelectorAll("#activity-list .signal-marker[data-event-id]").forEach((marker) => {
    const selected = marker.dataset.eventId === state.selectedActivityEventId;
    marker.classList.toggle("is-selected", selected);
    marker.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  renderSignalInspector(event);
}

function makeSignalMarker(item, order, selectedId, animateArrival = false) {
  const event = item.event;
  const metadata = uiModel.normalizeEventMetadata(event);
  const isNew = uiModel.isEventNewForRun(event, state.dashboard?.pending_check_run);
  const risk = eventNeedsAttention(event, metadata);
  const selected = String(event.id) === String(selectedId);
  const marker = element("button", [
    "signal-marker",
    `kind-${event.kind || "unknown"}`,
    `size-${metadata.size.key}`,
    isNew ? "is-new" : "",
    isNew && animateArrival ? "is-arriving" : "",
    risk ? "has-alert" : "",
    risk && !ATTENTION_EVENT_KINDS.has(event.kind) ? "is-risk-only" : "",
    selected ? "is-selected" : "",
    item.position <= 0.24 ? "is-start" : "",
    item.position >= 0.76 ? "is-end" : "",
  ].filter(Boolean).join(" "));
  marker.type = "button";
  marker.dataset.eventId = String(event.id);
  marker.dataset.eventOrder = String(order);
  marker.style.setProperty("--signal-position", `${(item.position * 100).toFixed(3)}%`);
  marker.style.setProperty("--signal-top", `${36 + (item.slot * 28)}px`);
  marker.setAttribute("aria-label", eventSignalAriaLabel(event, metadata, isNew));
  marker.setAttribute("aria-pressed", selected ? "true" : "false");
  marker.setAttribute("aria-controls", "signal-field-inspector");
  const shape = element("span", "signal-marker-shape");
  shape.setAttribute("aria-hidden", "true");
  const tooltip = element("span", "signal-marker-tooltip");
  tooltip.setAttribute("aria-hidden", "true");
  tooltip.append(
    element("strong", "", event.summary?.title || t("{repository} 分支变化", { repository: event.repository || t("仓库") })),
    element("small", "", `${eventKindLabel(event)} · ${formatTime(event.detected_at, true)}`),
  );
  marker.append(shape, tooltip);
  marker.addEventListener("click", () => selectActivitySignal(event.id));
  marker.addEventListener("focus", () => selectActivitySignal(event.id));
  return marker;
}

function makeSignalLane(lane, model, orderById, selectedId, sweep) {
  const row = element("div", "signal-lane");
  row.style.setProperty("--signal-lane-height", `${72 + ((lane.slots - 1) * 28)}px`);
  const label = element("div", "signal-lane-label");
  label.append(element("strong", "", lane.repository || t("仓库")), element("code", "", lane.branch || t("分支")));
  const track = element("div", "signal-lane-track");
  model.scanBands.forEach((band) => {
    const scan = element("span", "signal-scan-band");
    scan.setAttribute("aria-hidden", "true");
    scan.style.setProperty("--scan-position", `${(band.position * 100).toFixed(3)}%`);
    track.append(scan);
  });
  const now = element("span", "signal-now-line");
  now.setAttribute("aria-hidden", "true");
  track.append(now);
  if (sweep) {
    const sweepLine = element("span", "signal-sweep");
    sweepLine.setAttribute("aria-hidden", "true");
    track.append(sweepLine);
  }
  lane.events.forEach((item) => {
    const marker = makeSignalMarker(item, orderById.get(String(item.id)) ?? 0, selectedId, sweep);
    track.append(marker);
  });
  row.append(label, track);
  return row;
}

function renderSignalField(model) {
  const container = document.getElementById("activity-list");
  const pendingRunId = state.dashboard?.pending_check_run?.id ?? null;
  const sweep = Boolean(pendingRunId) && String(state.signalSweepRunId) !== String(pendingRunId);
  if (sweep) state.signalSweepRunId = String(pendingRunId);
  const selectedVisible = model.events.find((item) => String(item.id) === String(state.selectedActivityEventId));
  const selectedItem = selectedVisible
    || model.events.find((item) => uiModel.isEventNewForRun(item.event, state.dashboard?.pending_check_run))
    || model.events[0];
  state.selectedActivityEventId = selectedItem ? String(selectedItem.id) : null;
  const orderById = new Map(model.events.map((item, index) => [String(item.id), index]));

  const axis = element("div", "signal-axis");
  axis.setAttribute("aria-hidden", "true");
  axis.append(uiElement("span", "", "仓库 / 分支"));
  signalFieldTicks(model).forEach((tick) => axis.append(element("span", "", tick.label)));

  const lanes = element("div", "signal-lanes");
  lanes.setAttribute("role", "group");
  lanes.setAttribute("aria-label", t("分支信号场"));
  model.lanes.forEach((lane) => lanes.append(makeSignalLane(
    lane,
    model,
    orderById,
    state.selectedActivityEventId,
    sweep,
  )));

  const legend = element("div", "signal-field-legend");
  [
    ["signal-legend-ring", "本轮新增"],
    ["signal-legend-dot", "普通变化"],
    ["signal-legend-diamond", "历史缺口"],
    ["signal-legend-triangle", "需要处理"],
  ].forEach(([className, label]) => {
    const item = element("span");
    const icon = element("i", className);
    icon.setAttribute("aria-hidden", "true");
    item.append(icon, uiElement("span", "", label));
    legend.append(item);
  });
  if (model.scanBands.length) {
    const batch = element("span");
    const line = element("i", "signal-legend-band");
    line.setAttribute("aria-hidden", "true");
    batch.append(line, uiElement("span", "", "同一检查批次"));
    legend.append(batch);
  }
  const disclosure = uiElement("span", "signal-range-disclosure", "节点大小表示变化体量");
  disclosure.append(" · ", uiElement("span", "", "基于当前已加载记录"));
  legend.append(disclosure);

  const inspector = element("article", "signal-inspector");
  inspector.id = "signal-field-inspector";
  inspector.setAttribute("aria-label", t("信号解读"));
  container.replaceChildren(axis, lanes, legend, inspector);
  renderSignalInspector(selectedItem?.event, inspector);
}

function svgElement(tag, attributes = {}) {
  const value = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([name, attribute]) => {
    if (attribute !== undefined && attribute !== null) value.setAttribute(name, String(attribute));
  });
  return value;
}

function canvasTimeTicks(model) {
  const span = Math.max(1, model.endMs - model.startMs);
  const axisHours = Math.max(model.rangeHours || 0, span / (60 * 60 * 1000));
  return [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    ratio,
    x: 96 + (ratio * Math.max(1, model.latestX - 96)),
    label: formatSignalTick(model.startMs + (span * ratio), axisHours),
  }));
}

function canvasAuthorColor(author) {
  const colors = ["#2f5bea", "#119977", "#c3791d", "#7957c9", "#c94355", "#277caa", "#9b5c9f"];
  let hash = 0;
  Array.from(String(author || "?")).forEach((character) => {
    hash = ((hash << 5) - hash + character.codePointAt(0)) | 0;
  });
  return colors[Math.abs(hash) % colors.length];
}

function canvasSelectionKey(eventIds, author) {
  return `${(eventIds || []).join(",")}:${encodeURIComponent(String(author || ""))}`;
}

function canvasEdgePath(edge) {
  const direction = edge.to.x >= edge.from.x ? 1 : -1;
  const distance = Math.max(32, Math.abs(edge.to.x - edge.from.x));
  const firstBend = edge.from.x + (direction * Math.min(120, distance * 0.42));
  const secondBend = edge.to.x - (direction * Math.min(120, distance * 0.36));
  return `M ${edge.from.x} ${edge.from.y} C ${firstBend} ${edge.from.y}, ${secondBend} ${edge.to.y}, ${edge.to.x} ${edge.to.y}`;
}

function createCanvasTopology(model) {
  const svg = svgElement("svg", {
    class: "branch-canvas-svg",
    viewBox: `0 0 ${model.bounds.width} ${model.bounds.height}`,
    "aria-hidden": "true",
  });
  model.repositories.forEach((repository) => {
    svg.append(svgElement("rect", {
      class: "canvas-repository-band",
      x: 12,
      y: repository.y,
      width: Math.max(1, model.bounds.width - 24),
      height: Math.max(1, repository.height),
      rx: 15,
    }));
  });
  canvasTimeTicks(model).forEach((tick) => {
    svg.append(svgElement("line", {
      class: "canvas-time-grid",
      x1: tick.x,
      x2: tick.x,
      y1: 28,
      y2: Math.max(28, model.bounds.height - 28),
    }));
  });
  if (Number.isFinite(Number(model.nowX))) {
    svg.append(svgElement("line", {
      class: "canvas-time-now",
      x1: model.nowX,
      x2: model.nowX,
      y1: 28,
      y2: Math.max(28, model.bounds.height - 28),
    }));
  }
  model.lanes.forEach((lane) => {
    svg.append(svgElement("path", {
      class: `canvas-lane-line${lane.events.some((item) => eventNeedsAttention(item.event, item.metadata)) ? " is-alert" : ""}`,
      d: `M 72 ${lane.y} L ${model.latestX} ${lane.y}`,
    }));
  });
  model.edges.forEach((edge) => {
    svg.append(svgElement("path", {
      class: "canvas-lineage-edge",
      d: canvasEdgePath(edge),
    }));
  });
  return svg;
}

function canvasEventButton(item, order) {
  const event = item.event;
  const metadata = item.metadata;
  const isNew = uiModel.isEventNewForRun(event, state.dashboard?.pending_check_run);
  const selected = String(item.id) === String(state.canvasSelectedEventId);
  const button = element("button", [
    "branch-event-node",
    `kind-${event.kind || "unknown"}`,
    isNew ? "is-new" : "",
    selected ? "is-selected" : "",
  ].filter(Boolean).join(" "));
  button.type = "button";
  button.dataset.eventId = String(item.id);
  button.dataset.eventOrder = String(order);
  button.style.left = `${item.x}px`;
  button.style.top = `${item.y}px`;
  button.setAttribute("aria-label", eventSignalAriaLabel(event, metadata, isNew, item));
  button.setAttribute("aria-controls", "branch-author-inspector");
  button.addEventListener("click", () => {
    state.canvasSelectedEventId = String(item.id);
    document.querySelectorAll("#activity-list .branch-event-node[data-event-id]").forEach((node) => {
      node.classList.toggle("is-selected", node.dataset.eventId === state.canvasSelectedEventId);
    });
    const author = metadata.authors[0];
    if (author) toggleCanvasAuthor([item.id], author);
    else openEvent(item.id);
  });
  return button;
}

function canvasAuthorRange(segment, author) {
  return Array.isArray(segment?.authorRanges)
    ? segment.authorRanges.find((range) => String(range?.name || "") === String(author || "")) || null
    : null;
}

function canvasAuthorTimeCopy(segment, author) {
  const range = canvasAuthorRange(segment, author);
  if (range?.usesCommitTimeline) {
    const startMs = timelineTimestampMs(range.startMs);
    const endMs = timelineTimestampMs(range.endMs) ?? startMs;
    if (startMs !== null && endMs !== null && Math.abs(endMs - startMs) >= 60_000) {
      return t("提交范围 {start} – {end}", {
        start: formatTime(startMs),
        end: formatTime(endMs),
      });
    }
    return t("提交于 {time}", { time: formatTime(endMs) });
  }
  if (range?.fallbackBasis === "tip_commit") {
    return t("跟随变化节点 · 提交于 {time}", { time: formatTime(range.endMs ?? range.startMs) });
  }
  if (range?.fallbackBasis === "detected") {
    return t("跟随变化节点 · 检查于 {time}", { time: formatTime(range.endMs ?? range.startMs) });
  }
  if (range) return t("跟随变化节点时间");
  return "";
}

function canvasAuthorButton(segment, lane, author) {
  const eventIds = segment.eventIds.slice(-8);
  const selectionKey = canvasSelectionKey(eventIds, author);
  const expanded = state.canvasSelectedAuthor?.key === selectionKey;
  const button = element("button", "canvas-author-button");
  button.type = "button";
  button.dataset.canvasAuthor = author;
  button.dataset.selectionKey = selectionKey;
  button.setAttribute("aria-expanded", expanded ? "true" : "false");
  button.setAttribute("aria-controls", "branch-author-inspector");
  const actionLabel = t(expanded ? "收起 {author} 的修改" : "展开 {author} 的修改", { author });
  const authorRange = canvasAuthorRange(segment, author);
  const authorTimeCopy = canvasAuthorTimeCopy(segment, author);
  const authorTimeCaveat = [
    authorRange && !authorRange.usesCommitTimeline
      ? t("未取得可靠作者提交时间")
      : "",
    authorRange?.timeClamped
      ? t("时间在当前画布范围外，节点已定位到边界")
      : "",
  ].filter(Boolean).join(i18n.locale() === "en" ? "; " : "；");
  button.dataset.timeCopy = authorTimeCopy;
  button.dataset.timeCaveat = authorTimeCaveat;
  const accessibleTimeCopy = [authorTimeCopy, authorTimeCaveat].filter(Boolean).join(i18n.locale() === "en" ? ", " : "，");
  button.setAttribute("aria-label", accessibleTimeCopy
    ? t("{action}，{basis}", { action: actionLabel, basis: accessibleTimeCopy })
    : actionLabel);
  const avatar = element("span", "canvas-author-avatar", identityInitial(author, "?"));
  avatar.style.setProperty("--author-color", canvasAuthorColor(author));
  avatar.setAttribute("aria-hidden", "true");
  const copy = element("span", "canvas-author-copy");
  copy.append(element("strong", "", author));
  if (authorTimeCopy) copy.append(element("small", "canvas-author-time", authorTimeCopy));
  if (authorTimeCaveat) copy.append(element("small", "canvas-author-time-note", authorTimeCaveat));
  copy.append(element("small", "", segment.eventCount > eventIds.length
    ? t("最近 {shown} / 共 {total} 个变化段", { shown: eventIds.length, total: segment.eventCount })
    : t("{count} 个变化段", { count: segment.eventCount })));
  button.append(avatar, copy);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleCanvasAuthor(eventIds, author, { lane, totalEvents: segment.eventCount });
  });
  return button;
}

function createCanvasNodes(model) {
  const layer = element("div", "branch-canvas-nodes");
  canvasTimeTicks(model).forEach((tick) => {
    const label = element("span", "canvas-time-label", tick.label);
    label.style.left = `${tick.x}px`;
    label.style.top = "22px";
    layer.append(label);
  });
  if (Number.isFinite(Number(model.nowX)) && Math.abs(Number(model.nowX) - Number(model.latestX)) > 24) {
    const now = uiElement("span", "canvas-time-label is-now", "现在");
    now.style.left = `${model.nowX}px`;
    now.style.top = "36px";
    layer.append(now);
  }
  model.repositories.forEach((repository) => {
    const label = element("span", "canvas-repository-label");
    label.style.left = "28px";
    label.style.top = `${repository.y + 19}px`;
    label.append(
      element("strong", "", repository.repository || t("仓库")),
      element("span", "", repository.lanes.length === 1
        ? t("1 条分支轨道")
        : t("{count} 条分支轨道", { count: repository.lanes.length })),
    );
    layer.append(label);
  });
  const orderById = new Map(model.events.map((item, index) => [String(item.id), index]));
  model.lanes.forEach((lane) => {
    const branch = element("span", "canvas-branch-label", lane.branch || t("分支"));
    branch.style.left = "72px";
    branch.style.top = `${lane.y}px`;
    layer.append(branch);

    const cardPositions = [];
    lane.events.slice().reverse().forEach((item) => {
      layer.append(canvasEventButton(item, orderById.get(String(item.id)) ?? 0));
      if (cardPositions.every((position) => Math.abs(position - item.x) >= 270)) {
        cardPositions.push(item.x);
        const card = element("span", "canvas-event-card");
        const timeline = eventTimelineView(item.event, item);
        card.classList.add(timeline.basis === "tip_commit" ? "uses-commit-time" : "uses-detection-time");
        if (timeline.timeClamped) card.classList.add("has-clamped-time");
        card.style.left = `${item.x}px`;
        card.style.top = `${item.y + 39}px`;
        card.setAttribute("aria-hidden", "true");
        card.append(
          element("strong", "", localizedEventTitle(item.event)),
          element("span", "canvas-event-kind", eventKindLabel(item.event)),
          element("span", `canvas-event-time is-${timeline.basis}`, timelineTimeCopy(timeline, { relative: true })),
        );
        const secondary = timelineSecondaryCopy(timeline, { relative: true });
        if (secondary) card.append(element("span", "canvas-event-check-time", secondary));
        if (timeline.basis === "detected") {
          card.append(uiElement("span", "canvas-event-time-note", "未取得可靠提交时间"));
        } else if (timeline.outsideRequestedRange) {
          card.append(uiElement("span", "canvas-event-time-note", "提交时间不在所选发现范围内"));
        } else if (timeline.timeClamped) {
          card.append(uiElement("span", "canvas-event-time-note", "提交时间在当前画布范围外"));
        }
        layer.append(card);
      }
    });

    lane.authorSegments.forEach((segment) => {
      const authors = segment.authors.length ? segment.authors : [t("未知作者")];
      authors.forEach((author, authorIndex) => {
        const range = canvasAuthorRange(segment, author);
        const pair = Math.floor(authorIndex / 2);
        const tier = Math.floor(pair / 2) + 1;
        const direction = pair % 2 === 0 ? -1 : 1;
        const cluster = element("div", "canvas-author-cluster");
        cluster.style.left = `${range?.centerX ?? segment.centerX}px`;
        cluster.style.top = `${lane.y}px`;
        cluster.style.setProperty("--canvas-author-offset-y", `${direction * tier * 28}px`);
        if (authorIndex % 2 === 1) cluster.classList.add("is-before-anchor");
        cluster.setAttribute("role", "group");
        cluster.setAttribute("aria-label", t("作者变化"));
        cluster.append(canvasAuthorButton(segment, lane, author));
        layer.append(cluster);
      });
    });
  });
  return layer;
}

function canvasWorldViewport() {
  const viewport = document.getElementById("activity-list");
  const rect = viewport?.getBoundingClientRect();
  return {
    width: Math.max(1, rect?.width || viewport?.clientWidth || 1),
    height: Math.max(1, rect?.height || viewport?.clientHeight || 1),
  };
}

function createCanvasMinimap(model) {
  const button = element("button", "branch-canvas-minimap");
  button.id = "branch-canvas-minimap";
  button.type = "button";
  button.setAttribute("aria-label", t("小地图；点击移动当前视口"));
  const svg = svgElement("svg", { viewBox: "0 0 180 92", "aria-hidden": "true" });
  model.lanes.forEach((lane) => {
    const y = 8 + ((lane.y / Math.max(1, model.bounds.height)) * 76);
    svg.append(svgElement("line", { class: "minimap-lane", x1: 8, x2: 172, y1: y, y2: y }));
    lane.events.forEach((item) => {
      svg.append(svgElement("circle", {
        class: "minimap-event",
        cx: 8 + ((item.x / Math.max(1, model.bounds.width)) * 164),
        cy: y,
        r: 1.8,
      }));
    });
  });
  svg.append(svgElement("rect", { id: "branch-canvas-minimap-viewport", class: "minimap-viewport", x: 0, y: 0, width: 1, height: 1, rx: 2 }));
  button.append(svg, uiElement("span", "sr-only", "当前视口"));
  button.addEventListener("click", (event) => {
    if (!state.canvasModel) return;
    const rect = button.getBoundingClientRect();
    const xRatio = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
    const yRatio = Math.max(0, Math.min(1, (event.clientY - rect.top) / Math.max(1, rect.height)));
    const viewport = canvasWorldViewport();
    const zoom = state.canvasTransform.zoom;
    const worldX = state.canvasModel.bounds.x + (xRatio * state.canvasModel.bounds.width);
    const worldY = state.canvasModel.bounds.y + (yRatio * state.canvasModel.bounds.height);
    canvasViewMode = "manual";
    setCanvasTransform({
      x: (viewport.width / 2) - (worldX * zoom),
      y: (viewport.height / 2) - (worldY * zoom),
      zoom,
    });
  });
  return button;
}

function createCanvasLaneKey(model) {
  const key = element("div", "branch-canvas-lane-key");
  key.id = "branch-canvas-lane-key";
  key.setAttribute("aria-hidden", "true");
  model.lanes.forEach((lane) => {
    const label = element("span", "canvas-lane-key-item");
    label.dataset.worldY = String(lane.y);
    label.append(
      element("strong", "", lane.repository || t("仓库")),
      element("span", "", lane.branch || t("分支")),
    );
    key.append(label);
  });
  return key;
}

function updateCanvasLaneKey() {
  const viewport = document.getElementById("activity-list");
  const key = document.getElementById("branch-canvas-lane-key");
  if (!viewport || !key) return;
  const height = Math.max(1, viewport.clientHeight);
  key.querySelectorAll("[data-world-y]").forEach((label) => {
    const screenY = state.canvasTransform.y + (Number(label.dataset.worldY) * state.canvasTransform.zoom);
    label.style.transform = `translateY(${screenY}px)`;
    label.hidden = screenY < 34 || screenY > height - 34;
  });
}

function updateCanvasMinimap() {
  const model = state.canvasModel;
  const rect = document.getElementById("branch-canvas-minimap-viewport");
  if (!model || !rect) return;
  const viewport = canvasWorldViewport();
  const transform = state.canvasTransform;
  const worldX = (-transform.x / transform.zoom) - model.bounds.x;
  const worldY = (-transform.y / transform.zoom) - model.bounds.y;
  rect.setAttribute("x", String(Math.max(-4, (worldX / model.bounds.width) * 180)));
  rect.setAttribute("y", String(Math.max(-4, (worldY / model.bounds.height) * 92)));
  rect.setAttribute("width", String(Math.max(4, Math.min(188, (viewport.width / transform.zoom / model.bounds.width) * 180))));
  rect.setAttribute("height", String(Math.max(4, Math.min(100, (viewport.height / transform.zoom / model.bounds.height) * 92))));
}

function applyCanvasTransform() {
  canvasTransformFrame = null;
  const viewport = document.getElementById("activity-list");
  const world = document.getElementById("branch-canvas-world");
  if (!viewport || !world) return;
  const transform = state.canvasTransform;
  const previousLod = state.canvasLod;
  const lod = uiModel.canvasLod(transform.zoom, previousLod);
  state.canvasLod = lod;
  world.style.transform = `translate3d(${transform.x}px, ${transform.y}px, 0) scale(${transform.zoom})`;
  viewport.style.setProperty("--canvas-zoom", String(transform.zoom));
  viewport.style.setProperty("--canvas-counter-scale", String(1 / transform.zoom));
  viewport.classList.toggle("is-lod-overview", lod === "overview");
  viewport.classList.toggle("is-lod-authors", lod === "authors");
  viewport.classList.toggle("is-lod-detail", lod === "details");
  if (
    lod === "overview"
    && previousLod !== "overview"
    && document.activeElement?.closest?.(".canvas-author-button")
  ) {
    closeCanvasAuthorInspector();
    viewport.focus({ preventScroll: true });
  }
  const output = document.getElementById("branch-canvas-zoom-level");
  if (output) {
    output.value = `${Math.round(transform.zoom * 100)}%`;
    output.textContent = `${Math.round(transform.zoom * 100)}%`;
    output.setAttribute("aria-label", t("缩放级别 {level}", { level: output.textContent }));
  }
  updateCanvasMinimap();
  updateCanvasLaneKey();
}

function setCanvasTransform(transform) {
  state.canvasTransform = {
    x: Number.isFinite(Number(transform?.x)) ? Number(transform.x) : 0,
    y: Number.isFinite(Number(transform?.y)) ? Number(transform.y) : 0,
    zoom: uiModel.clampCanvasZoom(transform?.zoom),
  };
  if (canvasTransformFrame === null) canvasTransformFrame = window.requestAnimationFrame(applyCanvasTransform);
}

function zoomCanvas(factor, anchor = null) {
  const viewport = document.getElementById("activity-list");
  if (!viewport || !state.canvasModel) return;
  const size = canvasWorldViewport();
  const point = anchor || { x: size.width / 2, y: size.height / 2 };
  canvasViewMode = "manual";
  setCanvasTransform(uiModel.zoomAroundPoint(
    state.canvasTransform,
    state.canvasTransform.zoom * factor,
    point,
  ));
}

function fitBranchCanvas() {
  if (!state.canvasModel) return;
  canvasViewMode = "fit";
  setCanvasTransform(uiModel.fitCanvasTransform(state.canvasModel.bounds, canvasWorldViewport(), { padding: 48 }));
}

function showLatestCanvas({ initial = false, targetX = null, targetY = null } = {}) {
  if (!state.canvasModel) return;
  const hasTarget = targetX !== null
    && targetX !== undefined
    && targetX !== ""
    && Number.isFinite(Number(targetX));
  canvasViewMode = hasTarget ? "manual" : "latest";
  const viewport = canvasWorldViewport();
  setCanvasTransform(uiModel.latestCanvasTransform(state.canvasModel.bounds, viewport, {
    padding: Math.min(260, Math.max(120, viewport.width * 0.18)),
    latestX: hasTarget
      ? Number(targetX)
      : (state.canvasModel.nowX ?? state.canvasModel.latestX),
    targetY: hasTarget ? targetY : null,
    zoom: initial ? 0.86 : Math.max(0.86, state.canvasTransform.zoom),
  }));
}

function renderBranchCanvas(model) {
  const container = document.getElementById("activity-list");
  const signature = `${model.rangeHours}:${model.bounds.width}:${model.bounds.height}:${model.events.map((item) => item.id).join(",")}`;
  const shouldFit = !state.canvasModel || state.canvasModelSignature !== signature;
  state.canvasModel = model;
  state.canvasModelSignature = signature;
  if (!model.events.some((item) => String(item.id) === String(state.canvasSelectedEventId))) {
    state.canvasSelectedEventId = model.events[0] ? String(model.events[0].id) : null;
  }
  const world = element("div", "branch-canvas-world");
  world.id = "branch-canvas-world";
  world.style.width = `${model.bounds.width}px`;
  world.style.height = `${model.bounds.height}px`;
  world.append(createCanvasTopology(model), createCanvasNodes(model));
  const corner = element("span", "branch-canvas-corner");
  corner.append(element("i"), uiElement("span", "", "横轴 · 提交时间优先 / 检查时间回退"));
  container.replaceChildren(world, createCanvasLaneKey(model), createCanvasMinimap(model), corner);
  window.requestAnimationFrame(() => {
    if (shouldFit) {
      const newestDiscovery = model.events.reduce((latest, item) => {
        if (!latest) return item;
        const itemDetected = timelineTimestampMs(item.detectedTimeMs) ?? Number.NEGATIVE_INFINITY;
        const latestDetected = timelineTimestampMs(latest.detectedTimeMs) ?? Number.NEGATIVE_INFINITY;
        if (itemDetected !== latestDetected) return itemDetected > latestDetected ? item : latest;
        return Number(item.id) > Number(latest.id) ? item : latest;
      }, null);
      showLatestCanvas({
        initial: true,
        targetX: newestDiscovery?.x,
        targetY: newestDiscovery?.y,
      });
    }
    else applyCanvasTransform();
  });
}

function syncCanvasAuthorButtons() {
  document.querySelectorAll("#activity-list .canvas-author-button").forEach((button) => {
    const expanded = state.canvasSelectedAuthor?.key === button.dataset.selectionKey;
    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    const actionLabel = t(expanded ? "收起 {author} 的修改" : "展开 {author} 的修改", {
      author: button.dataset.canvasAuthor || t("未知作者"),
    });
    const accessibleTimeCopy = [button.dataset.timeCopy, button.dataset.timeCaveat]
      .filter(Boolean)
      .join(i18n.locale() === "en" ? ", " : "，");
    button.setAttribute("aria-label", accessibleTimeCopy
      ? t("{action}，{basis}", { action: actionLabel, basis: accessibleTimeCopy })
      : actionLabel);
  });
}

function closeCanvasAuthorInspector({ restoreFocus = false } = {}) {
  const selection = state.canvasSelectedAuthor;
  state.canvasSelectedAuthor = null;
  canvasDetailRequest += 1;
  const inspector = document.getElementById("branch-author-inspector");
  if (inspector) {
    inspector.hidden = true;
    inspector.replaceChildren();
  }
  syncCanvasAuthorButtons();
  if (restoreFocus && selection?.key) {
    document.querySelector(`#activity-list .canvas-author-button[data-selection-key="${CSS.escape(selection.key)}"]`)?.focus();
  }
}

async function canvasEventDetail(eventId) {
  const key = String(eventId);
  if (eventDetailCache.has(key)) return eventDetailCache.get(key);
  const payload = await api(`/api/v1/events/${encodeURIComponent(key)}`);
  const value = payload.event;
  eventDetailCache.set(key, value);
  if (eventDetailCache.size > 80) eventDetailCache.delete(eventDetailCache.keys().next().value);
  return value;
}

function renderCanvasAuthorInspector(author, details, eventIds, totalEvents = details.length) {
  const inspector = document.getElementById("branch-author-inspector");
  if (!inspector) return;
  inspector.replaceChildren();
  const avatar = element("span", "canvas-author-avatar", identityInitial(author, "?"));
  avatar.style.setProperty("--author-color", canvasAuthorColor(author));
  avatar.setAttribute("aria-hidden", "true");
  const heading = element("div");
  heading.append(
    element("strong", "", author),
    element("span", "", totalEvents > details.length
      ? t("最近 {shown} / 共 {total} 个变化段中的修改", { shown: details.length, total: totalEvents })
      : t("{count} 个变化段中的修改", { count: details.length })),
  );
  const header = element("div", "branch-author-inspector-header");
  header.append(avatar, heading);
  const actions = element("div", "branch-author-inspector-actions");
  const full = uiElement("button", "button button-secondary", "查看完整分析");
  full.type = "button";
  full.addEventListener("click", () => openEvent(eventIds.at(-1)));
  const close = uiElement("button", "button button-quiet", "收起");
  close.type = "button";
  close.addEventListener("click", () => closeCanvasAuthorInspector({ restoreFocus: true }));
  actions.append(full, close);

  const commits = details.flatMap((detail) => (detail?.commits || [])
    .filter((commit) => String(commit.author || "").trim().localeCompare(String(author).trim(), undefined, { sensitivity: "base" }) === 0)
    .map((commit) => ({ commit, detail }))
  ).sort((left, right) => Date.parse(right.commit.commit_date || right.commit.author_date || "") - Date.parse(left.commit.commit_date || left.commit.author_date || ""));
  const list = element("div", "branch-author-commits");
  commits.slice(0, 18).forEach(({ commit, detail }) => {
    const row = element("div", "branch-author-commit");
    row.append(
      element("strong", "", commit.subject || t("无提交说明")),
      element("span", "", `${detail.repository} / ${detail.branch} · ${t("提交于 {time}", {
        time: formatTime(commit.commit_date || commit.author_date),
      })}`),
    );
    list.append(row);
  });
  if (!list.childElementCount) {
    list.append(element("p", "branch-author-event-facts", t("当前详情中没有可展示的作者提交；历史可能已被裁剪。")));
  }
  const totals = details.reduce((result, detail) => ({
    files: result.files + Number(detail?.stats?.files || 0),
    insertions: result.insertions + Number(detail?.stats?.insertions || 0),
    deletions: result.deletions + Number(detail?.stats?.deletions || 0),
  }), { files: 0, insertions: 0, deletions: 0 });
  const facts = element("p", "branch-author-event-facts", t("事件整体共 {files} 个文件，新增 {insertions} 行、删除 {deletions} 行；这些总量不归因给单个作者。", totals));
  inspector.append(header, actions, list, facts);
  inspector.hidden = false;
}

async function toggleCanvasAuthor(eventIds, author, { lane = null, totalEvents = null } = {}) {
  const ids = Array.from(new Set((eventIds || []).map((id) => Number(id)).filter(Number.isSafeInteger)));
  if (!ids.length) return;
  const key = canvasSelectionKey(ids, author);
  if (state.canvasSelectedAuthor?.key === key) {
    closeCanvasAuthorInspector({ restoreFocus: true });
    return;
  }
  const request = ++canvasDetailRequest;
  state.canvasSelectedAuthor = {
    key,
    author,
    eventIds: ids,
    laneKey: lane?.key || null,
    totalEvents: Math.max(ids.length, Number(totalEvents) || ids.length),
  };
  syncCanvasAuthorButtons();
  const inspector = document.getElementById("branch-author-inspector");
  inspector.hidden = false;
  inspector.replaceChildren(emptyState("正在读取作者修改", "正在从本地事件事实中整理提交。"));
  try {
    const details = await Promise.all(ids.map(canvasEventDetail));
    if (request !== canvasDetailRequest || state.canvasSelectedAuthor?.key !== key) return;
    state.canvasSelectedAuthor.details = details;
    renderCanvasAuthorInspector(author, details, ids, state.canvasSelectedAuthor.totalEvents);
  } catch (_error) {
    if (request !== canvasDetailRequest || state.canvasSelectedAuthor?.key !== key) return;
    inspector.replaceChildren(emptyState("作者修改加载失败", "无法加载作者修改", "重新读取", () => {
      state.canvasSelectedAuthor = null;
      toggleCanvasAuthor(ids, author, { lane, totalEvents });
    }));
  }
}

function syncCanvasFocusControl() {
  const button = document.getElementById("branch-canvas-focus");
  if (!button) return;
  const focused = document.body.classList.contains("canvas-focus");
  button.setAttribute("aria-pressed", focused ? "true" : "false");
  button.setAttribute("aria-label", t(focused ? "退出专注模式" : "进入专注模式"));
  const copy = button.querySelector("span");
  if (copy) copy.textContent = t(focused ? "退出专注" : "专注画布");
}

function setCanvasFocus(focused, { restoreFocus = true } = {}) {
  const next = Boolean(focused);
  if (next === state.canvasFocused) return;
  if (next) canvasFocusReturn = document.activeElement;
  state.canvasFocused = next;
  document.body.classList.toggle("canvas-focus", next);
  syncCanvasFocusControl();
  window.requestAnimationFrame(() => {
    applyCanvasTransform();
    if (next) document.getElementById("activity-list")?.focus({ preventScroll: true });
    else if (restoreFocus) {
      const target = canvasFocusReturn?.isConnected ? canvasFocusReturn : document.getElementById("branch-canvas-focus");
      target?.focus({ preventScroll: true });
    }
  });
}

function bindBranchCanvasInteractions() {
  const viewport = document.getElementById("activity-list");
  if (!viewport) return;
  canvasViewportSize = canvasWorldViewport();
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("button, a, input, select")) return;
    canvasPanSession = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startX: state.canvasTransform.x,
      startY: state.canvasTransform.y,
      moved: false,
    };
    canvasViewMode = "manual";
    viewport.setPointerCapture?.(event.pointerId);
    viewport.classList.add("is-dragging");
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!canvasPanSession || canvasPanSession.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - canvasPanSession.startClientX;
    const deltaY = event.clientY - canvasPanSession.startClientY;
    canvasPanSession.moved ||= Math.abs(deltaX) + Math.abs(deltaY) > 4;
    setCanvasTransform({
      x: canvasPanSession.startX + deltaX,
      y: canvasPanSession.startY + deltaY,
      zoom: state.canvasTransform.zoom,
    });
  });
  const finishPan = (event) => {
    if (!canvasPanSession || canvasPanSession.pointerId !== event.pointerId) return;
    viewport.releasePointerCapture?.(event.pointerId);
    canvasPanSession = null;
    viewport.classList.remove("is-dragging");
  };
  viewport.addEventListener("pointerup", finishPan);
  viewport.addEventListener("pointercancel", finishPan);
  viewport.addEventListener("wheel", (event) => {
    if (!state.canvasModel || event.target.closest("button, input, select")) return;
    event.preventDefault();
    canvasViewMode = "manual";
    if (event.ctrlKey) {
      const bounds = viewport.getBoundingClientRect();
      zoomCanvas(Math.exp(-event.deltaY * 0.012), {
        x: event.clientX - bounds.left,
        y: event.clientY - bounds.top,
      });
      return;
    }
    const horizontal = event.shiftKey && Math.abs(event.deltaX) < Math.abs(event.deltaY)
      ? event.deltaY
      : event.deltaX;
    setCanvasTransform({
      x: state.canvasTransform.x - horizontal,
      y: state.canvasTransform.y - (event.shiftKey ? 0 : event.deltaY),
      zoom: state.canvasTransform.zoom,
    });
  }, { passive: false });
  viewport.addEventListener("dblclick", (event) => {
    if (event.target.closest("button")) return;
    const bounds = viewport.getBoundingClientRect();
    zoomCanvas(CANVAS_ZOOM_STEP, { x: event.clientX - bounds.left, y: event.clientY - bounds.top });
  });
  viewport.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const key = event.key.toLowerCase();
    if (["+", "=", "-", "_", "0", "end", "arrowleft", "arrowright", "arrowup", "arrowdown", "f"].includes(key)) {
      event.preventDefault();
      event.stopPropagation();
    }
    if (key === "+" || key === "=") zoomCanvas(CANVAS_ZOOM_STEP);
    else if (key === "-" || key === "_") zoomCanvas(1 / CANVAS_ZOOM_STEP);
    else if (key === "0") fitBranchCanvas();
    else if (key === "end") showLatestCanvas();
    else if (key === "f") setCanvasFocus(!state.canvasFocused);
    else if (key.startsWith("arrow")) {
      const step = event.shiftKey ? 180 : 72;
      canvasViewMode = "manual";
      setCanvasTransform({
        x: state.canvasTransform.x + (key === "arrowleft" ? step : key === "arrowright" ? -step : 0),
        y: state.canvasTransform.y + (key === "arrowup" ? step : key === "arrowdown" ? -step : 0),
        zoom: state.canvasTransform.zoom,
      });
    }
  });
  if (typeof ResizeObserver === "function") {
    canvasResizeObserver = new ResizeObserver(() => scheduleRender("canvas-resize", () => {
      const nextSize = canvasWorldViewport();
      const previousSize = canvasViewportSize;
      canvasViewportSize = nextSize;
      if (!state.canvasModel) {
        applyCanvasTransform();
        return;
      }
      if (canvasViewMode === "latest") {
        showLatestCanvas();
        return;
      }
      if (canvasViewMode === "fit") {
        fitBranchCanvas();
        return;
      }
      if (previousSize) {
        setCanvasTransform({
          x: state.canvasTransform.x + ((nextSize.width - previousSize.width) / 2),
          y: state.canvasTransform.y + ((nextSize.height - previousSize.height) / 2),
          zoom: state.canvasTransform.zoom,
        });
        return;
      }
      applyCanvasTransform();
    }));
    canvasResizeObserver.observe(viewport);
  }
}

function syncSignalRangeControls() {
  document.querySelectorAll("[data-signal-range]").forEach((control) => {
    const active = Number(control.dataset.signalRange) === state.activityRangeHours;
    control.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function setActivityRangeHours(value) {
  const hours = Number(value);
  if (![24, 168].includes(hours) || hours === state.activityRangeHours) return;
  state.activityRangeHours = hours;
  state.selectedActivityEventId = null;
  state.canvasModel = null;
  state.canvasModelSignature = null;
  closeCanvasAuthorInspector();
  loadActivityFeed({ reset: true });
}

function eventSearchText(event) {
  const cached = eventSearchTextCache.get(event);
  if (cached !== undefined) return cached;
  const metadata = uiModel.normalizeEventMetadata(event);
  const value = `${event.repository} ${event.branch} ${metadata.authors.join(" ")} ${metadata.categories.join(" ")} ${metadata.signals.join(" ")} ${event.summary?.title || ""} ${event.summary?.overview || ""}`.toLowerCase();
  eventSearchTextCache.set(event, value);
  return value;
}

function renderActivityList() {
  if (!state.dashboard) return;
  const query = document.getElementById("event-search").value.trim().toLowerCase();
  const kind = document.getElementById("event-kind-filter").value;
  const source = state.activityEvents || [];
  const values = source.filter((event) => {
    return (kind === "all" || event.kind === kind) && eventSearchText(event).includes(query);
  });
  const activityErrorMessage = state.activityError
    ? localizedErrorMessage({ code: state.activityErrorCode, message: state.activityError }, "无法读取分支动态")
    : "";
  const model = uiModel.buildBranchCanvasModel(values, {
    rangeHours: state.activityRangeHours,
    nowMs: Date.now(),
  });
  if (!model.events.length) {
    state.canvasModel = null;
    state.canvasModelSignature = null;
  }
  const rangeExhausted = state.activityRangeHours === 168 && model.events.length === 0;
  renderActivityScope();
  syncSignalRangeControls();
  const status = document.getElementById("activity-result-status");
  if (status) status.textContent = state.activityError
    ? t("动态读取失败")
    : t("显示 {visible} 条 · 已加载 {loaded} 条{more}", {
      visible: model.events.length,
      loaded: source.length,
      more: [
        model.hiddenCount ? ` · ${t("另有 {count} 条发现未进入当前画布", { count: model.hiddenCount })}` : "",
        state.activityHasMore ? t(" · 还有更早记录") : "",
      ].join(""),
    });
  const loadMore = document.getElementById("activity-load-more");
  if (loadMore) {
    loadMore.hidden = !state.activityHasMore;
    loadMore.disabled = state.activityLoading;
    loadMore.textContent = t(state.activityLoading ? "正在加载…" : "加载更早动态");
  }
  const container = document.getElementById("activity-list");
  container.setAttribute("aria-busy", state.activityLoading ? "true" : "false");
  if (state.activityLoading && !source.length) {
    container.replaceChildren(emptyState("正在读取分支动态", "正在从本地事件索引中加载记录。"));
    return;
  }
  if (state.activityError && !source.length) {
    container.replaceChildren(emptyState("动态读取失败", activityErrorMessage, "重新读取", () => loadActivityFeed({ reset: true }), { rawDescription: true }));
    return;
  }
  if (!values.length) {
    if (state.activityScope && !source.length) {
      container.replaceChildren(emptyState("这个分支还没有新动态", "关注时只建立静默基线；基线之后检测到的真实变化才会出现在这里。", "检查此分支", submitScopedCheck));
    } else if (source.length) {
      container.replaceChildren(emptyState("当前已加载记录中没有匹配项", state.activityHasMore ? "可以加载更早动态继续查找，或调整筛选条件。" : "调整搜索或事件类型后重试。", state.activityHasMore ? "加载更早动态" : "清除筛选", state.activityHasMore ? () => loadActivityFeed({ reset: false }) : clearActivityFilters));
    } else if (state.activityRangeHours === 24) {
      // Discovery is filtered by check time on the server, so an empty
      // 24-hour reply says
      // nothing about the week behind it. Offer the wider query instead of
      // declaring that nothing has ever happened.
      container.replaceChildren(emptyState("当前发现范围内没有变化", "切换到近 7 天发现范围继续查找。", "查看近 7 天发现", () => setActivityRangeHours(168)));
    } else {
      container.replaceChildren(emptyState("还没有变更事件", "检查到基线之后的真实变化后，摘要会按时间出现在这里。", (state.dashboard.watches || []).length ? "立即检查全部关注分支" : "选择关注分支", (state.dashboard.watches || []).length ? submitSafeCheck : openProjectsView));
    }
    return;
  }
  if (!model.events.length) {
    const canExpandRange = state.activityRangeHours === 24;
    const hasFilters = Boolean(query) || kind !== "all";
    let description = "可以加载更早动态继续查找，或调整筛选条件。";
    let actionLabel = state.activityHasMore ? "加载更早动态" : "清除筛选";
    let action = state.activityHasMore ? () => loadActivityFeed({ reset: false }) : clearActivityFilters;
    if (canExpandRange) {
      description = "切换到近 7 天发现范围继续查找。";
      actionLabel = "查看近 7 天发现";
      action = () => setActivityRangeHours(168);
    } else if (rangeExhausted) {
      description = "近 7 天未发现匹配的变化；可调整筛选，或等待新的变化。";
      actionLabel = hasFilters ? "清除筛选" : "";
      action = hasFilters ? clearActivityFilters : null;
    }
    container.replaceChildren(emptyState(
      "当前发现范围内没有变化",
      description,
      actionLabel,
      action,
    ));
    return;
  }
  renderBranchCanvas(model);
}

function renderActivityScope() {
  const scope = document.getElementById("activity-scope");
  if (!scope) return;
  scope.hidden = !state.activityScope;
  const label = document.getElementById("activity-scope-label");
  if (label && state.activityScope) label.textContent = `${state.activityScope.repository} / ${state.activityScope.branch}`;
  const check = document.getElementById("activity-scope-check");
  if (check) check.disabled = !state.activityScope?.branchId || Boolean(state.activeJob);
}

function clearActivityFilters() {
  document.getElementById("event-search").value = "";
  setEventKindFilter("all");
  renderActivityList();
}

async function loadActivityFeed({ reset = false } = {}) {
  if (state.activityLoading && !reset) return;
  const request = ++state.activityRequest;
  state.activityLoading = true;
  state.activityError = null;
  state.activityErrorCode = null;
  if (reset) {
    state.activityEvents = [];
    state.activityHasMore = false;
    state.activityNextBeforeId = null;
  }
  renderActivityList();
  const params = new URLSearchParams({
    limit: "40",
    since_hours: String(state.activityRangeHours),
  });
  if (state.activityScope?.repositoryId) params.set("repository_id", state.activityScope.repositoryId);
  if (state.activityScope?.branchId) params.set("branch_id", state.activityScope.branchId);
  if (!reset && state.activityNextBeforeId) params.set("before_id", state.activityNextBeforeId);
  try {
    const payload = await api(`/api/v1/events?${params.toString()}`);
    if (request !== state.activityRequest) return;
    const incoming = Array.isArray(payload.events) ? payload.events : [];
    const merged = reset ? incoming : [...state.activityEvents, ...incoming];
    state.activityEvents = Array.from(new Map(merged.map((event) => [event.id, event])).values());
    state.activityHasMore = Boolean(payload.has_more);
    state.activityNextBeforeId = payload.next_before_id ?? state.activityEvents.at(-1)?.id ?? null;
  } catch (error) {
    if (request === state.activityRequest) {
      state.activityError = error.message || "无法读取分支动态";
      state.activityErrorCode = error.code || null;
      showToast(localizedErrorMessage(error, "无法读取分支动态"), true);
    }
  } finally {
    if (request === state.activityRequest) {
      state.activityLoading = false;
      renderActivityList();
    }
  }
}

function openActivityScope(watch) {
  state.activityScope = {
    repositoryId: watch.repository_id ? String(watch.repository_id) : null,
    branchId: watch.id ? String(watch.id) : (watch.branch_id ? String(watch.branch_id) : null),
    repository: watch.repository || "仓库",
    branch: watch.branch || "分支",
  };
  if (currentView() !== "overview") location.hash = "overview";
  else setView("overview");
  loadActivityFeed({ reset: true });
  window.setTimeout(() => document.getElementById("event-search")?.focus(), 0);
}

function clearActivityScope() {
  state.activityScope = null;
  loadActivityFeed({ reset: true });
}

function renderLinkedLarkDocument(lark) {
  const reference = document.getElementById("lark-document-reference");
  const name = document.getElementById("lark-document-name");
  const link = document.getElementById("lark-document-link");
  const status = document.getElementById("lark-document-status");
  const configured = Boolean(lark?.document_configured);
  reference.hidden = !configured;
  if (!configured) {
    link.hidden = true;
    link.removeAttribute("href");
    return;
  }

  const documentName = identityText(lark.document_name, t("已关联的飞书项目文档"));
  name.textContent = documentName;
  let href = null;
  try {
    const candidate = new URL(lark.document_url || "");
    if (candidate.protocol === "https:") href = candidate.href;
  } catch (_error) {
    href = null;
  }
  link.hidden = !href;
  status.hidden = Boolean(href);
  if (href) {
    link.href = href;
    link.setAttribute("aria-label", t("打开关联文档：{document}", { document: documentName }));
  } else {
    link.removeAttribute("href");
    link.removeAttribute("aria-label");
  }
}

function renderIntegrations(value) {
  state.integrations = value;
  const codex = value?.codex || {};
  const lark = value?.lark || {};
  let codexAnnouncement = "";
  let larkAnnouncement = "";

  if (!value?.checked) {
    setIntegrationIdentity("codex", { name: t("等待检测"), meta: "Codex CLI", status: t("尚未检查"), statusClass: "is-muted" });
    setIntegrationIdentity("lark", { name: t("等待检测"), meta: t("lark-cli 用户身份"), status: t("尚未检查"), statusClass: "is-muted", avatar: i18n.locale() === "en" ? "L" : "飞" });
    document.getElementById("codex-description").textContent = t("刷新后会确认本机 Codex 登录方式。");
    document.getElementById("lark-description").textContent = t("刷新后会确认当前飞书用户与文档关联状态。");
    codexAnnouncement = t("Codex：尚未检查");
    larkAnnouncement = t("飞书：尚未检查");
  } else if (codex.available && codex.authenticated) {
    const codexName = t(codex.auth_mode === "chatgpt" ? "ChatGPT 账号" : (codex.auth_mode === "api_key" ? "API Key" : "已认证账号"));
    setIntegrationIdentity("codex", { name: codexName, meta: identityText(codex.version, "Codex CLI"), status: t("已连接"), statusClass: "" });
    document.getElementById("codex-description").textContent = t("{version} 已可用于显式深度总结。", {
      version: codex.version || "Codex CLI",
    });
    codexAnnouncement = t("Codex：{name}已连接", { name: codexName });
  } else {
    const codexStatus = codex.available ? "需要登录" : (codex.detected ? "启动失败" : "未找到");
    setIntegrationIdentity("codex", { name: t("未连接"), meta: identityText(codex.version, "Codex CLI"), status: t(codexStatus), statusClass: "is-warning" });
    document.getElementById("codex-description").textContent = codex.error
      ? localizedErrorMessage({ message: codex.error }, "Codex 不可用时仍会使用本地规则摘要。")
      : t("Codex 不可用时仍会使用本地规则摘要。");
    codexAnnouncement = t("Codex：{status}", { status: t(codexStatus) });
  }

  if (value?.checked && lark.available && lark.verified) {
    const larkName = identityText(lark.user_name, t("飞书用户"));
    const larkStatus = lark.document_configured ? "已关联文档" : "已连接";
    const larkMeta = t("{version} · 用户身份", { version: identityText(lark.version, "lark-cli") });
    setIntegrationIdentity("lark", { name: larkName, meta: larkMeta, status: t(larkStatus), statusClass: "", avatar: identityInitial(larkName, i18n.locale() === "en" ? "L" : "飞") });
    document.getElementById("lark-description").textContent = t(lark.document_configured
      ? "项目文档已关联，可以发布待处理事件。"
      : "用户身份已验证，还需关联一个项目文档。");
    larkAnnouncement = t("飞书：{name}{status}", { name: larkName, status: t(larkStatus) });
  } else if (value?.checked) {
    const larkStatus = lark.available ? "需要登录" : "未找到";
    setIntegrationIdentity("lark", { name: t("未连接"), meta: identityText(lark.version, t("lark-cli 用户身份")), status: t(larkStatus), statusClass: "is-warning", avatar: i18n.locale() === "en" ? "L" : "飞" });
    document.getElementById("lark-description").textContent = lark.error
      ? localizedErrorMessage({ message: lark.error }, "请在普通 Terminal 中完成 lark-cli 登录。")
      : t("请在普通 Terminal 中完成 lark-cli 登录。");
    larkAnnouncement = t("飞书：{status}", { status: t(larkStatus) });
  }
  if (lark.document_configured) clearPendingLarkCreate();
  document.getElementById("create-lark-button").disabled = Boolean(lark.document_configured || state.larkCreatePending);
  if (state.larkCreatePending && !lark.document_configured) {
    const larkStatus = document.getElementById("lark-status");
    larkStatus.textContent = t("创建结果待确认");
    larkStatus.className = "status-chip is-warning";
    document.getElementById("lark-description").textContent = t("请先在飞书云空间检查，或关联已经创建的文档；暂不允许重复新建。");
    larkAnnouncement = t("飞书：文档创建结果待确认");
  }
  renderLinkedLarkDocument(lark);
  document.getElementById("integration-status-announcement").textContent = [codexAnnouncement, larkAnnouncement].filter(Boolean).join(i18n.locale() === "en" ? "; " : "；");
}

async function loadDashboard() {
  try {
    // The reply seeds the activity list, so it is read through the same window
    // the view is showing.
    const payload = await api(`/api/v1/dashboard?since_hours=${state.activityRangeHours}`);
    renderDashboard(payload);
    clearPersistentError();
    return payload;
  } catch (error) {
    showToast(localizedErrorMessage(error, "无法读取 Branch Radar 状态"), true);
    showPersistentError(localizedErrorMessage(error, "无法读取 Branch Radar 状态"));
    throw error;
  }
}

async function loadCachedIntegrations() {
  try {
    const value = await api("/api/v1/integrations");
    renderIntegrations(value);
    return value;
  } catch (error) {
    showToast(localizedErrorMessage(error, "无法读取连接状态"), true);
    return null;
  }
}

function elapsedJobText(job) {
  const started = Date.parse(job?.started_at || job?.created_at || "");
  if (!Number.isFinite(started)) return t("刚刚开始");
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (seconds < 60) return t(`${seconds} 秒`);
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return t(remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`);
}

function localizedProgressValue(value, fallback, variables = {}) {
  if (!value) return t(fallback, variables);
  const localized = t(value);
  if (i18n.locale() === "en" && localized === value && /[\u3400-\u9fff]/u.test(value)) {
    return t(fallback, variables);
  }
  return localized;
}

function localizedJobProgress(job, fallback = jobCopy[job?.kind] || ["正在处理任务", "请保持页面打开"]) {
  const progress = job?.progress || {};
  const phase = progress.phase || job?.status || "queued";
  const variables = {
    current: Math.max(0, Number(progress.current) || 0),
    total: Math.max(0, Number(progress.total) || 0),
    count: Math.max(0, Number(progress.total) || 0),
  };
  const phaseCopy = {
    watch: {
      queued: ["正在建立分支基线", "正在准备读取所选分支。"],
      inspect: ["正在读取仓库信息", "正在确认 Git remote、当前分支和本地引用。"],
      fetch: ["正在获取所选分支的远程引用", "正在获取 {count} 个所选分支的最新远程引用。"],
      resolve: ["正在解析分支引用", "已核对 {current}/{total} 个分支。"],
      save: ["正在保存静默基线", "已保存 {current}/{total} 个分支。"],
      complete: ["分支关注已完成", "正在刷新页面中的关注分支。"],
    },
    check: {
      queued: ["正在准备分支检查", "正在确认本轮需要检查的关注分支。"],
      fetch: ["正在准备仓库", "已处理 {current}/{total} 个仓库。"],
      inspect: ["正在准备仓库", "已处理 {current}/{total} 个仓库。"],
      resolve: ["正在解析分支引用", "已核对 {current}/{total} 个分支。"],
      finalize: ["正在整理本轮检查", "正在汇总本轮变化与异常。"],
      publish: ["正在对账飞书发布", "正在核验本轮事件和待处理投递。"],
      summarize: ["正在生成本轮综述", "正在使用已脱敏的结构化变化事实。"],
      complete: ["分支检查已完成", "检查结果已经安全保存。"],
    },
  };
  const controlled = job?.status === "failed"
    ? fallback
    : (phaseCopy[job?.kind]?.[phase] || fallback);
  return [
    localizedProgressValue(progress.label, controlled[0], variables),
    localizedProgressValue(progress.detail, controlled[1], variables),
  ];
}

function watchProgressPercent(progress) {
  const total = Number(progress?.total) || 0;
  const current = Math.max(0, Math.min(Number(progress?.current) || 0, total || 0));
  const fraction = total ? current / total : 0;
  if (progress?.phase === "complete") return 100;
  if (progress?.phase === "save") return Math.round(80 + (fraction * 20));
  if (progress?.phase === "resolve") return Math.round(60 + (fraction * 20));
  if (progress?.phase === "inspect") return 8;
  return 0;
}

function resetWatchProgress() {
  const dialog = document.getElementById("branch-dialog");
  const panel = document.getElementById("watch-progress");
  dialog.classList.remove("has-watch-progress", "is-watch-progressing", "is-watch-result", "is-watch-error");
  panel.hidden = true;
  document.getElementById("branch-footer-copy").textContent = t("只会观察本机已有的 checkout");
  document.getElementById("branch-dismiss-button").textContent = t("取消");
  document.getElementById("branch-dialog-close").setAttribute("aria-label", t("关闭分支选择"));
  const candidate = state.selectedCandidate;
  if (candidate) {
    document.getElementById("branch-dialog-title").textContent = t("选择 {repository} 的分支", {
      repository: candidate.name || t("仓库"),
    });
    document.getElementById("branch-dialog-subtitle").textContent = candidate.canonical_remote
      ? t("匹配 {remote}。勾选要观察的分支，或一键全选。", { remote: candidate.canonical_remote })
      : t("这是仅本地仓库。勾选要观察的分支，或一键全选。");
  }
  state.watchProgressPhase = null;
}

function renderWatchProgress(job) {
  const dialog = document.getElementById("branch-dialog");
  if (!dialog?.open || job?.kind !== "watch") return;
  const progress = job.progress || {};
  const isError = job.status === "failed" || progress.phase === "error";
  const isResult = !isError && !["queued", "running"].includes(job.status);
  const isRunning = !isError && !isResult;
  const panel = document.getElementById("watch-progress");
  const branches = state.watchSubmission?.branches || selectedBranchNames();
  const count = branches.length;
  const phase = progress.phase || (job.status === "queued" ? "queued" : "inspect");

  panel.hidden = false;
  document.getElementById("task-bar").hidden = true;
  document.body.classList.remove("has-task-bar");
  dialog.classList.add("has-watch-progress");
  dialog.classList.toggle("is-watch-progressing", isRunning);
  dialog.classList.toggle("is-watch-result", isResult);
  dialog.classList.toggle("is-watch-error", isError);
  document.getElementById("branch-dialog-title").textContent = isError
    ? t("关注任务未完成")
    : isResult
      ? t("分支关注已完成")
      : t("正在关注 {count} 个分支", { count: count || progress.total || t("所选") });
  document.getElementById("branch-dialog-subtitle").textContent = t(isRunning
    ? "正在获取最新引用并建立静默基线，可以安全收起到后台。"
    : isError
      ? "已保留你的选择，可以返回调整或直接重试。"
      : "静默基线已经建立，正在刷新关注列表。");

  const fallbackLabel = isError ? "任务未完成" : isResult ? "分支关注已完成" : "正在处理所选分支";
  const [progressTitle, progressDetail] = localizedJobProgress(job, [
    fallbackLabel,
    isRunning ? "请保持本地服务运行。" : "",
  ]);
  document.getElementById("watch-progress-title").textContent = progressTitle;
  document.getElementById("watch-progress-detail").textContent = progressDetail;
  const elapsed = elapsedJobText(job);
  document.getElementById("watch-progress-elapsed").textContent = elapsed;

  const track = document.getElementById("watch-progress-track");
  const indeterminate = isRunning && (progress.indeterminate || ["queued", "fetch"].includes(phase));
  track.classList.toggle("is-indeterminate", indeterminate);
  if (indeterminate) {
    track.removeAttribute("aria-valuenow");
    track.setAttribute("aria-valuetext", t("{label}，已用时 {elapsed}", {
      label: progressTitle, elapsed,
    }));
  } else {
    const percent = isError ? watchProgressPercent({ ...progress, phase: state.watchProgressPhase }) : watchProgressPercent(progress);
    track.style.setProperty("--watch-progress", String(Math.max(0, Math.min(percent, 100)) / 100));
    track.setAttribute("aria-valuenow", String(percent));
    track.setAttribute("aria-valuetext", isError ? t("任务未完成") : `${percent}%`);
  }

  if (WATCH_PHASE_ORDER.includes(phase)) state.watchProgressPhase = phase;
  const activePhase = phase === "complete" ? WATCH_PHASE_ORDER.length : Math.max(0, WATCH_PHASE_ORDER.indexOf(isError ? state.watchProgressPhase : phase));
  document.querySelectorAll("[data-watch-phase]").forEach((item, index) => {
    item.classList.toggle("is-complete", phase === "complete" || index < activePhase);
    item.classList.toggle("is-active", !isError && phase !== "complete" && index === activePhase);
  });

  const visibleBranches = branches.slice(0, 3).join(i18n.locale() === "en" ? ", " : "、");
  const extra = branches.length > 3
    ? t(" 等 {count} 个分支", { count: branches.length })
    : branches.length ? t(" · 共 {count} 个", { count: branches.length }) : "";
  document.getElementById("watch-progress-selection").textContent = visibleBranches
    ? `${visibleBranches}${extra}`
    : t("正在处理所选分支");
  const footerCopy = document.getElementById("branch-footer-copy");
  const dismiss = document.getElementById("branch-dismiss-button");
  footerCopy.textContent = t(isRunning ? "任务会在后台继续，可安全收起" : isError ? "未完成的分支不会写入基线" : "已完成，正在刷新关注列表");
  dismiss.textContent = t(isRunning ? "收起到后台" : isError ? "返回选择" : "关闭");
  document.getElementById("branch-dialog-close").setAttribute("aria-label", t(isRunning ? "收起任务到后台" : "关闭分支选择"));

  if (state.watchProgressPhase !== dialog.dataset.announcedWatchPhase || isError || isResult) {
    document.getElementById("watch-progress-announcement").textContent = t("{label}。{detail}", {
      label: progressTitle,
      detail: progressDetail,
    });
    dialog.dataset.announcedWatchPhase = state.watchProgressPhase || phase;
  }
}

function renderScanRootPickerProgress(job) {
  if (job?.kind !== "pick_scan_root") return;
  const dialog = document.getElementById("scan-roots-dialog");
  if (!dialog.open) {
    renderScanRootFields(configuredScanRoots());
    document.getElementById("scan-roots-error").textContent = "";
    document.getElementById("scan-root-manual").open = false;
    openDialog("scan-roots-dialog");
  }
  setScanRootPickerBusy(true);
  const [, progressDetail] = localizedJobProgress(job);
  setScanRootPickerStatus(
    "waiting",
    "等待你在系统窗口中选择文件夹…",
    progressDetail || "完成选择或取消后，结果会自动回到这里。",
  );
  document.getElementById("task-bar").hidden = true;
  document.body.classList.remove("has-task-bar");
}

function updateTaskProgress(job) {
  if (job.kind === "clone") {
    renderCloneTask(job);
    return;
  }
  const progress = job.progress || {};
  const [title, detail] = localizedJobProgress(job);
  document.getElementById("task-context").textContent = t("后台任务");
  document.getElementById("task-title").textContent = title;
  document.getElementById("task-description").textContent = detail;
  document.getElementById("task-elapsed").textContent = elapsedJobText(job);
  const taskBar = document.getElementById("task-bar");
  taskBar.classList.remove("is-succeeded", "is-failed", "is-cancelled", "is-expanded");
  document.body.classList.remove("has-expanded-task");
  document.getElementById("task-percent").hidden = true;
  document.getElementById("task-toggle").hidden = true;
  document.getElementById("task-cancel-button").hidden = true;
  document.getElementById("task-details").hidden = true;
  const total = Number(progress.total) || 0;
  const current = Math.max(0, Math.min(Number(progress.current) || 0, total || 0));
  const indeterminate = Boolean(progress.indeterminate) || !total;
  const track = document.getElementById("task-progress-track");
  track.classList.toggle("is-indeterminate", indeterminate);
  taskBar.style.setProperty("--task-progress", String(indeterminate ? 0 : current / total));
  if (indeterminate) {
    track.removeAttribute("aria-valuenow");
    track.setAttribute("aria-valuetext", title);
  } else {
    const percent = Math.round((current / total) * 100);
    track.setAttribute("aria-valuenow", String(percent));
    track.setAttribute("aria-valuetext", `${percent}%`);
  }
  if (job.kind === "watch") renderWatchProgress(job);
  if (job.kind === "pick_scan_root") renderScanRootPickerProgress(job);
}

function showTask(job) {
  const copy = jobCopy[job.kind] || ["正在处理任务", "请保持页面打开"];
  document.getElementById("task-title").textContent = t(copy[0]);
  document.getElementById("task-description").textContent = t(copy[1]);
  document.getElementById("task-bar").hidden = false;
  document.body.classList.add("is-processing", "has-task-bar");
  document.querySelectorAll("#check-button, #check-options-button, #projects-refresh-button, #scan-roots-button, #associate-remote-button, #publish-button, #connections-refresh-button, #confirm-watch-button, #select-all-branches, #select-visible-watches, #clone-submit-button, [data-check-control], #check-form button[type=submit], #publish-form button[type=submit], #create-lark-form button[type=submit], #lark-connect-form button[type=submit]").forEach((control) => {
    control.disabled = true;
  });
  if (job.kind !== "clone") {
    document.querySelectorAll(".local-project-card .button").forEach((control) => {
      control.disabled = true;
    });
  }
  const taskButtonSelectors = {
    check: "#check-form button[type=submit]",
    publish: "#publish-form button[type=submit]",
    lark_create: "#create-lark-form button[type=submit]",
    lark_connect: "#lark-connect-form button[type=submit]",
  };
  const taskButton = document.querySelector(taskButtonSelectors[job.kind] || "#nonexistent-task-button");
  if (taskButton) {
    if (!taskButton.dataset.idleLabel) taskButton.dataset.idleLabel = taskButton.textContent.trim();
    taskButton.textContent = t(copy[0]);
    taskButton.setAttribute("aria-busy", "true");
  }
  if (job.kind === "watch" && state.watchSubmission) {
    setWatchButtonState("running", state.watchSubmission.branches.length);
  }
  syncMonitoringControls();
  updateTaskProgress(job);
}

function hideTask() {
  document.getElementById("task-bar").hidden = true;
  document.body.classList.remove("is-processing", "has-task-bar", "has-expanded-task");
  document.querySelectorAll("#check-button, #check-options-button, #projects-refresh-button, #scan-roots-button, #associate-remote-button, #publish-button, #connections-refresh-button, #confirm-watch-button, #select-all-branches, #select-visible-watches, #clone-submit-button, [data-check-control], #check-form button[type=submit], #publish-form button[type=submit], #create-lark-form button[type=submit], #lark-connect-form button[type=submit]").forEach((control) => {
    control.disabled = false;
  });
  document.querySelectorAll("[data-idle-label]").forEach((control) => {
    control.textContent = control.dataset.idleLabel;
    control.removeAttribute("data-idle-label");
    control.removeAttribute("aria-busy");
  });
  if (document.getElementById("branch-dialog")?.open) updateBranchPickCount();
  if (state.candidates) renderLocalProjects();
  updateSelectedCheckControls();
  renderActivityScope();
  syncScanRootsControls();
  syncMonitoringControls();
}

function showToast(message, isError = false) {
  const activeDialog = document.querySelector("dialog[open]");
  if (activeDialog) {
    activeDialog.querySelector(".modal-feedback")?.remove();
    const feedback = element("div", `modal-feedback ${isError ? "is-error" : ""}`.trim(), message);
    feedback.setAttribute("role", isError ? "alert" : "status");
    feedback.setAttribute("aria-live", isError ? "assertive" : "polite");
    feedback.setAttribute("aria-atomic", "true");
    const shell = activeDialog.querySelector(".modal-shell");
    const header = shell?.querySelector(".modal-header");
    if (shell) shell.insertBefore(feedback, header?.nextSibling || shell.firstChild);
    window.setTimeout(() => feedback.remove(), 5200);
    return;
  }
  const toast = element("div", `toast ${isError ? "is-error" : ""}`.trim(), message);
  document.getElementById("toast-region").append(toast);
  window.setTimeout(() => toast.remove(), 5200);
}

function showPersistentError(message) {
  const banner = document.getElementById("service-error");
  document.getElementById("service-error-copy").textContent = t(
    "{message}。请确认启动终端仍在运行，然后重新连接。",
    { message },
  );
  banner.hidden = false;
}

function clearPersistentError() {
  document.getElementById("service-error").hidden = true;
}

function resetCheckOptions() {
  document.getElementById("check-codex").checked = false;
  document.getElementById("check-publish").checked = false;
}

function savePendingLarkCreate(title) {
  state.larkCreatePending = { requestId: state.larkCreateRequestId, title };
  sessionStorage.setItem("branch-radar-lark-create", JSON.stringify(state.larkCreatePending));
}

function clearPendingLarkCreate() {
  state.larkCreatePending = null;
  state.larkCreateRequestId = null;
  sessionStorage.removeItem("branch-radar-lark-create");
}

async function submitJob(path, payload, {
  method = "POST",
  resumeConflict = true,
  surfaceError = true,
} = {}) {
  try {
    const response = await api(path, { method, body: payload });
    resumeJob(response.job);
    return response.job;
  } catch (error) {
    if (error instanceof ApiError && error.code === "operation_in_progress" && error.payload?.active_job) {
      resumeJob(error.payload.active_job);
      if (resumeConflict) {
        showToast(t("已有任务正在运行，已恢复它的进度。"), true);
        return error.payload.active_job;
      }
    }
    if (surfaceError) showToast(localizedErrorMessage(error, "无法启动任务"), true);
    throw error;
  }
}

function resumeJob(job) {
  if (!job || !job.id) return;
  if (job.kind === "pick_scan_root") {
    rememberScanRootPickerJob(job);
    state.scanRootPickerNeedsReconcile = false;
  }
  if (job.kind === "clone") rememberCloneJob(job);
  const generation = ++state.pollGeneration;
  state.activeJob = job;
  state.pollFailures = 0;
  state.pollAttempts = 0;
  showTask(job);
  if (state.pollingTimer) window.clearTimeout(state.pollingTimer);
  pollJob(job.id, generation);
}

function ensureScanRootPickerRecoveryDialog() {
  const dialog = document.getElementById("scan-roots-dialog");
  if (!state.dashboard || dialog.open) return dialog;
  renderScanRootFields(configuredScanRoots());
  document.getElementById("scan-roots-error").textContent = "";
  document.getElementById("scan-root-manual").open = false;
  document.getElementById("scan-root-manual-input").value = "";
  openDialog("scan-roots-dialog");
  return dialog;
}

function forgetMissingScanRootPickerJob(jobId) {
  clearStoredScanRootPickerJob();
  state.scanRootPickerNeedsReconcile = false;
  state.pollFailures = 0;
  if (state.activeJob?.kind === "pick_scan_root" && (!jobId || state.activeJob.id === jobId)) {
    state.activeJob = null;
    state.pollGeneration += 1;
    if (state.pollingTimer) window.clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
  }
  setScanRootPickerBusy(false);
  hideTask();
  ensureScanRootPickerRecoveryDialog();
  setScanRootPickerStatus("cancelled", "上次选择记录已失效", "任务记录已不存在，请重新选择本机文件夹。");
  clearPersistentError();
  window.requestAnimationFrame(() => document.getElementById("scan-root-picker").focus());
}

function pauseScanRootPickerReconciliation(jobId, message) {
  if (state.activeJob?.kind === "pick_scan_root" && state.activeJob.id === jobId) {
    state.activeJob = null;
    state.pollGeneration += 1;
  }
  if (state.pollingTimer) window.clearTimeout(state.pollingTimer);
  state.pollingTimer = null;
  state.scanRootPickerNeedsReconcile = true;
  hideTask();
  setScanRootPickerBusy(false);
  ensureScanRootPickerRecoveryDialog();
  setScanRootPickerStatus(
    "cancelled",
    "暂时无法确认选择结果",
    "点击“重新读取选择结果”，或关闭窗口后使用页面上的“重新连接”。",
  );
  showPersistentError(message ? t(message) : t("目录选择结果读取失败"));
  window.requestAnimationFrame(() => document.getElementById("scan-root-picker").focus());
}

async function reconcileStoredScanRootPickerJob({ force = false } = {}) {
  const reference = storedScanRootPickerJob();
  if (!reference) return false;
  const activeReference = scanRootPickerJobReference(state.activeJob);
  if (activeReference?.id === reference.id) {
    if (force) resumeJob(state.activeJob);
    return true;
  }
  if (state.activeJob) return false;

  ensureScanRootPickerRecoveryDialog();
  state.scanRootPickerNeedsReconcile = false;
  setScanRootPickerBusy(true);
  let job;
  try {
    const response = await api(`/api/v1/jobs/${encodeURIComponent(reference.id)}`);
    job = response.job;
  } catch (error) {
    if (error instanceof ApiError && error.code === "job_not_found") {
      forgetMissingScanRootPickerJob(reference.id);
      return false;
    }
    state.scanRootPickerNeedsReconcile = true;
    setScanRootPickerBusy(false);
    setScanRootPickerStatus(
      "cancelled",
      "暂时无法读取上次选择结果",
      "任务编号已保留；点击“重新读取选择结果”可以继续对账。",
    );
    showPersistentError(localizedErrorMessage(error, "目录选择结果读取失败"));
    return false;
  }

  const resolvedReference = scanRootPickerJobReference(job);
  if (!resolvedReference || resolvedReference.id !== reference.id) {
    forgetMissingScanRootPickerJob(reference.id);
    return false;
  }
  clearPersistentError();
  rememberScanRootPickerJob(job);
  if (jobIsActive(job)) {
    resumeJob(job);
    return true;
  }
  state.scanRootPickerNeedsReconcile = false;
  if (["succeeded", "completed_with_errors", "reconcile_required"].includes(job.status)) {
    await handleJobResult(job);
  } else {
    handleJobFailure(job);
  }
  return true;
}

async function reconcileStoredCloneJob({ force = false } = {}) {
  const reference = storedCloneTask();
  const jobId = reference?.id;
  if (!jobId) return false;
  if (state.activeJob?.kind === "clone" && state.activeJob.id === jobId) {
    if (force) resumeJob(state.activeJob);
    return true;
  }
  if (state.activeJob) return false;
  let job;
  try {
    const response = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
    job = response.job;
  } catch (error) {
    if (error instanceof ApiError && error.code === "job_not_found") {
      clearStoredCloneJob();
      return false;
    }
    showPersistentError(localizedErrorMessage(error, "任务状态读取失败"));
    return false;
  }
  if (job?.kind !== "clone" || job.id !== jobId) {
    clearStoredCloneJob();
    return false;
  }
  if (!state.cloneSubmission) {
    const roots = configuredScanRoots();
    const restoredParent = roots.includes(reference?.parent) ? reference.parent : "";
    state.cloneSubmission = {
      jobId,
      repository: reference?.repository || "",
      directoryName: reference?.directoryName || "",
      parent: restoredParent,
      status: job.status,
    };
  }
  clearPersistentError();
  if (jobIsActive(job)) {
    resumeJob(job);
    return true;
  }
  if (job.status === "succeeded") await handleJobResult(job);
  else handleJobFailure(job);
  return true;
}

async function pollJob(jobId, generation = state.pollGeneration) {
  if (generation !== state.pollGeneration) return;
  let job;
  try {
    const revision = state.activeJob?.id === jobId ? state.activeJob?.revision : null;
    const query = Number.isSafeInteger(revision) && revision > 0
      ? `?wait=${JOB_WAIT_SECONDS}&revision=${revision}`
      : "";
    const response = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}${query}`);
    if (generation !== state.pollGeneration) return;
    job = response.job;
  } catch (error) {
    if (generation !== state.pollGeneration) return;
    const pickerReference = storedScanRootPickerJob();
    const isScanRootPicker = state.activeJob?.kind === "pick_scan_root" || pickerReference?.id === jobId;
    if (isScanRootPicker && error instanceof ApiError && error.code === "job_not_found") {
      forgetMissingScanRootPickerJob(jobId);
      return;
    }
    const isClone = state.activeJob?.kind === "clone" || storedCloneJobId() === jobId;
    if (isClone && error instanceof ApiError && error.code === "job_not_found") {
      clearStoredCloneJob();
      state.activeJob = null;
      state.pollGeneration += 1;
      if (state.pollingTimer) window.clearTimeout(state.pollingTimer);
      state.pollingTimer = null;
      hideTask();
      showToast(t("克隆任务记录已失效，请重新粘贴仓库地址后重试。"), true);
      return;
    }
    state.pollFailures += 1;
    if (state.pollFailures <= 3) {
      state.pollingTimer = window.setTimeout(
        () => pollJob(jobId, generation),
        750 * (2 ** (state.pollFailures - 1)),
      );
      return;
    }
    if (isScanRootPicker) {
      pauseScanRootPickerReconciliation(jobId, localizedErrorMessage(error, "任务状态读取失败"));
      return;
    }
    hideTask();
    showPersistentError(localizedErrorMessage(error, "任务状态读取失败"));
    return;
  }

  state.pollFailures = 0;
  clearPersistentError();
  state.activeJob = job;
  updateTaskProgress(job);
  if (jobIsActive(job)) {
    state.pollAttempts += 1;
    state.pollingTimer = window.setTimeout(
      () => pollJob(jobId, generation),
      JOB_REPOLL_DELAY_MS,
    );
    return;
  }

  // A terminal result is handled once. UI refresh failures must not replay a
  // completed external operation or restart polling the same terminal job.
  state.activeJob = null;
  hideTask();
  try {
    if (["succeeded", "completed_with_errors", "reconcile_required"].includes(job.status)) {
      await handleJobResult(job);
    } else {
      handleJobFailure(job);
    }
  } catch (error) {
    const message = localizedErrorMessage(error, "任务已完成，但页面刷新失败，请重新连接。");
    showToast(message, true);
    showPersistentError(message);
  } finally {
    if (job.kind !== "clone" && state.cloneTerminalJob && !state.activeJob) {
      renderCloneTask(state.cloneTerminalJob);
    }
  }
}

async function handleJobResult(job) {
  const result = job.result || {};
  if (job.kind === "clone") {
    state.cloneCancelling = false;
    if (state.cloneSubmission) state.cloneSubmission.status = "succeeded";
    const candidate = result.repository;
    if (!candidate || typeof candidate !== "object") {
      const failedJob = {
        ...job,
        status: "failed",
        error_code: "clone_verification_failed",
      };
      state.cloneTerminalJob = failedJob;
      renderCloneTask(failedJob);
      return;
    }
    const candidates = Array.isArray(state.candidates) ? [...state.candidates] : [];
    const existingIndex = candidates.findIndex((item) => (
      (candidate.path && item.path === candidate.path)
      || (candidate.canonical_remote && item.canonical_remote === candidate.canonical_remote)
    ));
    if (existingIndex >= 0) candidates.splice(existingIndex, 1, candidate);
    else candidates.unshift(candidate);
    state.candidates = candidates;
    state.discoveryError = null;
    state.discoveryErrorCode = null;
    state.matchedRemoteCanonical = candidate.canonical_remote || state.clonePreview?.remote?.canonical || null;
    state.localProjectPage = 1;
    state.pendingRemoteUrl = null;
    renderLocalProjects();
    state.cloneTerminalJob = {
      ...job,
      progress: { ...(job.progress || {}), phase: "complete", percent: 100 },
    };
    rememberCloneJob(state.cloneTerminalJob);
    renderCloneTask(state.cloneTerminalJob);
    showToast(t(result.reused ? "仓库已经存在且验证通过，可从任务记录选择分支。" : "克隆完成，可从任务记录继续选择分支。"));
    return;
  }
  if (job.kind === "discover") {
    setLocalProjects(result.repositories || []);
    if (state.pendingRemoteUrl) {
      const url = state.pendingRemoteUrl;
      state.pendingRemoteUrl = null;
      await matchRemoteUrl(url);
    }
    return;
  }
  if (job.kind === "pick_scan_root") {
    clearStoredScanRootPickerJob();
    state.scanRootPickerNeedsReconcile = false;
    setScanRootPickerBusy(false);
    const scanRoot = typeof result.scan_root === "string" ? result.scan_root : "";
    if (result.cancelled || !scanRoot.trim()) {
      setScanRootPickerStatus("cancelled", "没有选择文件夹", "扫描范围保持不变，你可以再次选择或直接保存。");
    } else if (state.scanRootsDraft.includes(scanRoot)) {
      setScanRootPickerStatus("success", "这个文件夹已在列表中", "扫描范围没有重复添加。");
    } else if (state.scanRootsDraft.length >= MAX_SCAN_ROOTS) {
      setScanRootPickerStatus(
        "cancelled",
        "已达到目录上限",
        "最多可设置 {count} 个目录；移除一个目录后再重新选择。",
        { count: MAX_SCAN_ROOTS },
      );
    } else {
      renderScanRootFields([...state.scanRootsDraft, scanRoot]);
      setScanRootPickerStatus("success", "文件夹已加入扫描范围", "尚未保存；可继续选择或保存并重新扫描。");
    }
    window.requestAnimationFrame(() => {
      const picker = document.getElementById("scan-root-picker");
      (picker.disabled ? document.getElementById("scan-roots-save") : picker).focus();
    });
    return;
  }
  if (job.kind === "integrations") {
    renderIntegrations(result);
    return;
  }
  if (job.kind === "monitoring") {
    const submittedHere = state.monitoringSubmissionJobId === job.id;
    state.monitoringSaving = false;
    if (submittedHere) {
      state.monitoringSubmissionJobId = null;
      state.monitoringDirty = false;
    }
    const preserveDraft = !submittedHere && state.monitoringDirty;
    const value = await loadMonitoringSettings({ force: true, preserveDraft });
    if (value) {
      if (state.dashboard?.settings) state.dashboard.settings.poll_interval_seconds = value.interval_seconds;
      setMonitoringMessage(submittedHere ? "监控设置已保存。" : preserveDraft ? "设置尚未保存。" : "监控设置已与本机同步。", {
        kind: submittedHere ? "success" : "",
      });
    }
    syncMonitoringControls();
    syncContextActions();
    return;
  }
  if (job.kind === "watch") {
    const watched = result.watched?.length || 0;
    const skipped = result.skipped?.length || 0;
    const pending = (result.watched || []).filter((item) => item.baseline_pending || item.warning).length;
    const submittedCandidate = state.watchSubmission?.candidate;
    if (submittedCandidate && watched) {
      const added = (result.watched || []).map((item) => item.branch).filter(Boolean);
      submittedCandidate.repository_id = result.watched[0]?.repository_id ?? submittedCandidate.repository_id;
      submittedCandidate.watched_branches = uniqueNames([
        ...(submittedCandidate.watched_branches || []),
        ...added,
      ]);
    }
    state.recentlyAddedBranches = (result.watched || []).map((item) => ({
      repository_id: item.repository_id,
      branch_id: item.branch_id,
      repository: item.repository,
      branch: item.branch,
    }));
    state.recentlyAddedWatchIds = new Set(
      state.recentlyAddedBranches.map((item) =>
        `${item.repository_id ?? item.repository}:${item.branch_id ?? item.branch}`
      ),
    );
    const needsAttention = Boolean(skipped || pending);
    setWatchButtonState(needsAttention ? "partial" : "success", watched);
    const firstSkipped = result.skipped?.[0];
    let watchMessage = t("已建立 {count} 个静默基线。", { count: watched });
    if (pending) {
      watchMessage = t("已关注 {watched} 个分支，其中 {pending} 个将在首次成功刷新后建立基线。", { watched, pending });
    }
    if (skipped) {
      watchMessage = t("已关注 {watched} 个分支；{branch}：{error}", {
        watched,
        branch: firstSkipped?.branch || t("另有分支"),
        error: firstSkipped?.error || t("需要处理"),
      });
      if (pending) watchMessage += t(" 另有 {count} 个等待建立基线。", { count: pending });
    }
    if (document.getElementById("branch-dialog")?.open) {
      renderWatchProgress({
        ...job,
        progress: {
          phase: "complete",
          label: needsAttention ? "关注完成，部分分支需要处理" : "静默基线已建立",
          detail: watchMessage,
          current: watched,
          total: watched + skipped,
          indeterminate: false,
        },
      });
    } else {
      showToast(watchMessage, needsAttention);
    }
  } else if (job.kind === "check") {
    closeDialog("check-dialog");
    resetCheckOptions();
    const submission = state.checkSubmission?.jobId === job.id ? state.checkSubmission : null;
    const values = result.results || [];
    const changed = values.filter((item) => item.status === "changed").length;
    const errors = values.filter((item) => item.status === "error").length;
    const publishErrors = (result.publications || []).filter((item) => ["error", "unknown"].includes(item.status)).length;
    const checked = values.length;
    const message = errors || publishErrors
      ? t("已检查 {checked} 个分支：{changed} 个变化，{errors} 个分支错误，{publishErrors} 个发布问题。", { checked, changed, errors, publishErrors })
      : changed
        ? t("已检查 {checked} 个分支，发现 {changed} 个变化。", { checked, changed })
        : t("已检查 {checked} 个分支，没有发现新的变化。", { checked });
    showToast(message, Boolean(errors || publishErrors));
    if (submission?.scope === "selected") state.selectedWatchIds.clear();
    if (submission) state.checkSubmission = null;
  } else if (job.kind === "publish") {
    closeDialog("publish-dialog");
    const outcomes = result.outcomes || [];
    const errors = outcomes.filter((item) => ["error", "unknown"].includes(item.status)).length;
    showToast(errors
      ? t("发布完成，{count} 项仍需对账或重试。", { count: errors })
      : outcomes.length
        ? t("已处理 {count} 条飞书发布。", { count: outcomes.length })
        : t("没有待发布事件。"), Boolean(errors));
  } else if (job.kind === "lark_connect") {
    clearPendingLarkCreate();
    showToast(t("飞书文档已验证并关联。"));
  } else if (job.kind === "lark_create") {
    clearPendingLarkCreate();
    closeDialog("create-lark-dialog");
    showToast(t("飞书项目文档已创建并关联。"));
  }
  try {
    await loadDashboard();
    if (job.kind === "check" && state.activityScope) {
      await loadActivityFeed({ reset: true });
    }
    if (["lark_connect", "lark_create", "publish"].includes(job.kind)) await refreshIntegrations();
  } finally {
    if (job.kind === "watch") {
      await new Promise((resolve) => window.setTimeout(resolve, 1600));
      state.watchSubmission = null;
      closeDialog("branch-dialog");
      state.selectedCandidate = null;
      setWatchButtonState("idle");
      if (currentView() !== "overview") location.hash = "overview";
      else setView("overview");
    }
  }
}

function handleJobFailure(job) {
  const unknown = job.error_code === "lark_create_unknown";
  if (job.kind === "clone") {
    state.cloneCancelling = false;
    if (state.cloneSubmission) state.cloneSubmission.status = job.status || "failed";
    state.cloneTerminalJob = job;
    rememberCloneJob(job);
    renderCloneTask(job);
  } else if (job.kind === "pick_scan_root") {
    clearStoredScanRootPickerJob();
    state.scanRootPickerNeedsReconcile = false;
    setScanRootPickerBusy(false);
    resetScanRootPickerStatus();
    const error = document.getElementById("scan-roots-error");
    error.textContent = localizedErrorMessage(job, "无法完成目录选择，请重试或使用手动路径。");
    error.focus();
  } else if (job.kind === "watch" && document.getElementById("branch-dialog")?.open) {
    renderWatchProgress(job);
  } else if (job.kind === "monitoring") {
    if (state.monitoringSubmissionJobId === job.id) state.monitoringSubmissionJobId = null;
    state.monitoringSaving = false;
    setMonitoringMessage(localizedErrorMessage(job, "监控设置保存失败，请重试。"), { kind: "error", raw: true });
    syncMonitoringControls();
    syncContextActions();
    if (currentView() === "settings") {
      window.requestAnimationFrame(() => document.getElementById("monitoring-save-status").focus({ preventScroll: true }));
    }
  } else {
    showToast(localizedErrorMessage(job, "任务没有完成"), true);
  }
  if (unknown) {
    const status = document.getElementById("lark-status");
    status.textContent = t("创建结果待确认");
    status.className = "status-chip is-warning";
    document.getElementById("lark-description").textContent = t("请先在飞书云空间检查是否已创建文档，不要直接重复创建。");
    document.getElementById("create-lark-button").disabled = true;
    closeDialog("create-lark-dialog");
  } else if (job.kind === "lark_create") {
    clearPendingLarkCreate();
  }
  if (job.kind === "discover") {
    const scan = document.getElementById("scan-status");
    scan.hidden = false;
    scan.classList.add("is-complete");
    scan.querySelector("strong").textContent = t("扫描未完成");
    scan.querySelector("p").textContent = localizedErrorMessage(job, "请检查扫描目录后重试");
    state.discoveryError = job.error || "请检查扫描目录后重试";
    state.discoveryErrorIsRaw = Boolean(job.error);
    state.discoveryErrorCode = job.error_code || null;
    state.candidates = [];
    state.pendingRemoteUrl = null;
    renderLocalProjects();
  }
  if (job.kind === "watch") {
    const count = state.watchSubmission?.branches?.length || 0;
    setWatchButtonState("error", count);
  }
  if (job.kind === "check" && (!state.checkSubmission?.jobId || state.checkSubmission.jobId === job.id)) {
    state.checkSubmission = null;
  }
}

async function refreshIntegrations() {
  if (state.activeJob) return;
  try {
    await submitJob("/api/v1/integrations/refresh", {});
  } catch (_error) {
    // submitJob has already surfaced the actionable error.
  }
}

function openDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog.open) dialog.showModal();
}

function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog?.open) dialog.close();
}

function openProjectsView() {
  if (currentView() !== "projects") location.hash = "projects/add";
  else setWatchWorkspaceTab("add");
}

function currentScanRootDraftValues() {
  return [...state.scanRootsDraft];
}

function normalizedScanRoots(values) {
  const roots = [];
  const seen = new Set();
  (Array.isArray(values) ? values : []).forEach((value) => {
    if (typeof value !== "string") return;
    if (!value.trim() || seen.has(value)) return;
    seen.add(value);
    roots.push(value);
  });
  return roots;
}

function updateScanRootDraftCount() {
  document.getElementById("scan-roots-count").textContent = t("{count} 个目录", { count: state.scanRootsDraft.length });
}

function syncScanRootCapacity() {
  const full = state.scanRootsDraft.length >= MAX_SCAN_ROOTS;
  const busy = state.scanRootsSaving || state.scanRootPickerPending;
  const unresolved = state.scanRootPickerNeedsReconcile;
  const picker = document.getElementById("scan-root-picker");
  const manualInput = document.getElementById("scan-root-manual-input");
  const manualAdd = document.getElementById("scan-root-manual-add");
  picker.disabled = busy || (full && !unresolved);
  manualInput.disabled = busy || full || unresolved;
  manualAdd.disabled = busy || full || unresolved;
  document.querySelectorAll("#scan-roots-list .scan-root-remove").forEach((control) => {
    control.disabled = busy || unresolved;
  });
  picker.title = unresolved
    ? t("重新读取上一次目录选择结果")
    : full
      ? t("最多可设置 {count} 个扫描目录；移除一个目录后可继续选择", { count: MAX_SCAN_ROOTS })
      : "";
  document.getElementById("scan-roots-help").textContent = unresolved
    ? t("先重新读取上一次选择结果，再修改或保存扫描范围。")
    : full
    ? t("已达到 {count} 个目录上限；移除一个目录后可继续添加。", { count: MAX_SCAN_ROOTS })
    : t("可保存空列表；此时 Branch Radar 不会自动扫描本地文件。");
}

function renderScanRootFields(values, focusIndex = null) {
  const roots = normalizedScanRoots(values);
  state.scanRootsDraft = roots;
  const container = document.getElementById("scan-roots-list");
  const fragment = document.createDocumentFragment();
  if (!roots.length) {
    fragment.append(uiElement("div", "scan-roots-empty", "还没有选择目录。使用系统窗口选择文件夹，或展开下方的手动输入。"));
  } else {
    roots.forEach((root, index) => {
      const row = element("div", "scan-root-row");
      const identity = element("div", "scan-root-identity");
      const glyph = element("span", "scan-root-glyph");
      const path = element("code", "", root);
      path.title = root;
      glyph.append(createIcon("folder"));
      identity.append(glyph, path);
      const remove = button("", "icon-button scan-root-remove", () => {
        const next = currentScanRootDraftValues();
        next.splice(index, 1);
        document.getElementById("scan-roots-error").textContent = "";
        resetScanRootPickerStatus();
        renderScanRootFields(next, next.length ? Math.min(index, next.length - 1) : -1);
      });
      remove.setAttribute("aria-label", t("移除扫描目录：{root}", { root }));
      remove.append(createIcon("close"));
      row.append(identity, remove);
      fragment.append(row);
    });
  }
  container.replaceChildren(fragment);
  updateScanRootDraftCount();
  syncScanRootCapacity();
  if (focusIndex !== null) {
    window.requestAnimationFrame(() => {
      if (focusIndex < 0) document.getElementById("scan-root-picker").focus();
      else container.querySelectorAll(".scan-root-remove")[focusIndex]?.focus();
    });
  }
}

function setScanRootPickerStatus(mode, title, copy, variables = {}) {
  const status = document.getElementById("scan-root-picker-status");
  status.className = `scan-root-picker-status${mode ? ` is-${mode}` : ""}`;
  status.dataset.titleSource = title;
  status.dataset.copySource = copy;
  status.dataset.copyVariables = JSON.stringify(variables);
  document.getElementById("scan-root-picker-status-title").textContent = t(title, variables);
  document.getElementById("scan-root-picker-status-copy").textContent = t(copy, variables);
  status.hidden = false;
}

function resetScanRootPickerStatus() {
  const status = document.getElementById("scan-root-picker-status");
  status.hidden = true;
  status.className = "scan-root-picker-status";
  delete status.dataset.titleSource;
  delete status.dataset.copySource;
  delete status.dataset.copyVariables;
}

function syncScanRootsFormBusy() {
  const form = document.getElementById("scan-roots-form");
  const save = document.getElementById("scan-roots-save");
  const picker = document.getElementById("scan-root-picker");
  const busy = state.scanRootsSaving || state.scanRootPickerPending;
  form.setAttribute("aria-busy", busy ? "true" : "false");
  form.querySelectorAll("input, button").forEach((control) => {
    control.disabled = busy;
  });
  const manualSummary = document.querySelector("#scan-root-manual > summary");
  const manualLocked = busy || state.scanRootPickerNeedsReconcile;
  manualSummary.setAttribute("aria-disabled", manualLocked ? "true" : "false");
  manualSummary.tabIndex = manualLocked ? -1 : 0;
  save.classList.toggle("is-busy", state.scanRootsSaving);
  save.disabled = busy || state.scanRootPickerNeedsReconcile;
  save.querySelector("span").textContent = t(state.scanRootsSaving ? "正在保存…" : "保存并重新扫描");
  picker.classList.toggle("is-busy", state.scanRootPickerPending);
  picker.querySelector("span").textContent = t(state.scanRootPickerPending
    ? "等待系统窗口…"
    : state.scanRootPickerNeedsReconcile
      ? "重新读取选择结果"
      : "选择本机文件夹");
  document.getElementById("scan-roots-footer-copy").textContent = t(state.scanRootsSaving
    ? "正在保存本机偏好，请稍候"
    : state.scanRootPickerPending
      ? "先在系统窗口完成选择或取消"
      : state.scanRootPickerNeedsReconcile
        ? "先确认上一次目录选择结果"
        : "保存后会使用新范围重新扫描");
  syncScanRootCapacity();
  syncScanRootsControls();
}

function setScanRootsBusy(busy) {
  state.scanRootsSaving = busy;
  syncScanRootsFormBusy();
}

function setScanRootPickerBusy(busy) {
  state.scanRootPickerPending = busy;
  syncScanRootsFormBusy();
  if (busy) {
    setScanRootPickerStatus(
      "waiting",
      "等待你在系统窗口中选择文件夹…",
      "完成选择或取消后，结果会自动回到这里。",
    );
  }
}

function openScanRootsDialog() {
  if (!state.dashboard || state.activeJob || state.scanRootsSaving || state.scanRootPickerPending) return;
  document.getElementById("scan-roots-error").textContent = "";
  renderScanRootFields(configuredScanRoots());
  state.scanRootPickerNeedsReconcile = Boolean(storedScanRootPickerJob());
  if (state.scanRootPickerNeedsReconcile) {
    setScanRootPickerStatus("cancelled", "上次选择结果等待确认", "点击“重新读取选择结果”继续对账，不会重新打开系统窗口。");
  } else {
    resetScanRootPickerStatus();
  }
  document.getElementById("scan-root-manual").open = false;
  document.getElementById("scan-root-manual-input").value = "";
  syncScanRootsFormBusy();
  openDialog("scan-roots-dialog");
  window.requestAnimationFrame(() => {
    const picker = document.getElementById("scan-root-picker");
    (picker.disabled ? document.querySelector("#scan-roots-list .scan-root-remove") || document.getElementById("scan-roots-save") : picker).focus();
  });
}

function addManualScanRoot() {
  const input = document.getElementById("scan-root-manual-input");
  const error = document.getElementById("scan-roots-error");
  const value = input.value.trim();
  error.textContent = "";
  if (state.scanRootPickerNeedsReconcile) {
    error.textContent = t("请先重新读取上一次目录选择结果。");
    error.focus();
    return;
  }
  if (!value) {
    error.textContent = t("请输入要添加的目录路径。");
    input.focus();
    return;
  }
  if (state.scanRootsDraft.length >= MAX_SCAN_ROOTS) {
    error.textContent = t("最多可设置 {count} 个扫描目录；请先移除一个目录。", { count: MAX_SCAN_ROOTS });
    error.focus();
    return;
  }
  if (state.scanRootsDraft.includes(value)) {
    error.textContent = t("这个目录已经在已选列表中。");
    input.focus();
    return;
  }
  renderScanRootFields([...state.scanRootsDraft, value]);
  input.value = "";
  setScanRootPickerStatus("success", "路径已添加", "保存后会使用新的扫描范围重新扫描。");
  window.requestAnimationFrame(() => {
    (state.scanRootsDraft.length >= MAX_SCAN_ROOTS ? document.getElementById("scan-roots-save") : input).focus();
  });
}

async function startScanRootPicker() {
  if (state.activeJob || state.scanRootsSaving || state.scanRootPickerPending) return;
  const error = document.getElementById("scan-roots-error");
  error.textContent = "";
  if (storedScanRootPickerJob()) {
    state.scanRootPickerNeedsReconcile = true;
    syncScanRootsFormBusy();
    await reconcileStoredScanRootPickerJob({ force: true });
    return;
  }
  if (state.scanRootsDraft.length >= MAX_SCAN_ROOTS) {
    error.textContent = t("最多可设置 {count} 个扫描目录；请先移除一个目录。", { count: MAX_SCAN_ROOTS });
    error.focus();
    return;
  }
  state.scanRootPickerNeedsReconcile = false;
  setScanRootPickerBusy(true);
  try {
    const response = await api("/api/v1/preferences/scan-roots/picker", { method: "POST", body: {} });
    if (response.job?.kind !== "pick_scan_root") {
      throw new Error(t("系统目录选择没有正确启动，请重试或使用手动路径。"));
    }
    resumeJob(response.job);
  } catch (caught) {
    if (
      caught instanceof ApiError
      && caught.code === "operation_in_progress"
      && caught.payload?.active_job?.kind === "pick_scan_root"
    ) {
      resumeJob(caught.payload.active_job);
      return;
    }
    setScanRootPickerBusy(false);
    resetScanRootPickerStatus();
    error.textContent = caught instanceof ApiError && caught.code === "operation_in_progress"
      ? t("当前有其他任务正在运行，完成后再选择文件夹。")
      : localizedErrorMessage(caught, "无法打开系统目录选择窗口，请重试或使用手动路径。");
    error.focus();
  }
}

async function saveScanRoots(event) {
  event.preventDefault();
  if (state.scanRootsSaving || state.scanRootPickerPending || state.scanRootPickerNeedsReconcile || state.activeJob) return;
  const roots = currentScanRootDraftValues();
  const error = document.getElementById("scan-roots-error");
  error.textContent = "";
  setScanRootsBusy(true);
  try {
    const payload = await api("/api/v1/preferences/scan-roots", { method: "POST", body: { roots } });
    const savedRoots = Array.isArray(payload.scan_roots) ? normalizedScanRoots(payload.scan_roots) : roots;
    state.dashboard = {
      ...(state.dashboard || {}),
      settings: { ...(state.dashboard?.settings || {}), scan_roots: savedRoots },
    };
    state.candidates = null;
    state.discoveryError = null;
    state.discoveryErrorCode = null;
    setScanRootsBusy(false);
    closeDialog("scan-roots-dialog");
    syncScanRootsControls();
    if (savedRoots.length) {
      showToast(t("已保存 {count} 个扫描目录，正在重新扫描。", { count: savedRoots.length }));
      await startDiscovery();
    } else {
      state.pendingRemoteUrl = null;
      renderLocalProjects();
      showToast(t("已清空扫描范围；在重新设置前不会自动扫描本地文件。"));
    }
  } catch (caught) {
    setScanRootsBusy(false);
    error.textContent = localizedErrorMessage(caught, "扫描范围保存失败，请稍后重试。");
    error.focus();
  }
}

function ensureLocalProjects() {
  if (!state.dashboard) return;
  if (!hasConfiguredScanRoots()) {
    renderLocalProjects();
    return;
  }
  if (state.candidates !== null || state.activeJob) return;
  startDiscovery();
}

async function startDiscovery() {
  if (!state.dashboard || state.activeJob) return;
  if (!hasConfiguredScanRoots()) {
    renderLocalProjects();
    return;
  }
  const roots = configuredScanRoots();
  state.candidates = null;
  state.discoveryError = null;
  state.discoveryErrorIsRaw = false;
  state.discoveryErrorCode = null;
  document.getElementById("scan-roots-copy").textContent = scanRootsSummary(roots);
  const scan = document.getElementById("scan-status");
  scan.hidden = false;
  scan.classList.remove("is-complete");
  scan.querySelector("strong").textContent = t("正在扫描本地仓库");
  scan.querySelector("p").textContent = scanRootsSummary(roots);
  document.getElementById("local-project-count").textContent = t("正在扫描");
  document.getElementById("local-project-search").disabled = true;
  document.getElementById("local-project-page-status").textContent = t("正在扫描");
  document.getElementById("local-project-pagination").hidden = true;
  document.getElementById("local-project-list").replaceChildren(
    emptyState("正在读取本地项目", t("只扫描已配置的 {count} 个目录。", { count: roots.length })),
  );
  try {
    const job = await submitJob("/api/v1/discoveries", {});
    if (job?.kind !== "discover") {
      state.discoveryError = "当前有其他任务正在运行，完成后可重新扫描";
      state.discoveryErrorIsRaw = false;
      state.discoveryErrorCode = null;
      if (!Array.isArray(state.candidates)) state.candidates = [];
      renderLocalProjects();
    }
  } catch (error) {
    state.discoveryError = error.message || "无法启动本地项目扫描";
    state.discoveryErrorIsRaw = Boolean(error.message);
    state.discoveryErrorCode = error.code || null;
    if (!Array.isArray(state.candidates)) state.candidates = [];
    renderLocalProjects();
  }
}

function uniqueNames(values) {
  const result = [];
  const seen = new Set();
  values.forEach((value) => {
    if (!value || seen.has(value)) return;
    seen.add(value);
    result.push(value);
  });
  return result;
}

function watchedBranchesFor(candidate) {
  const known = Array.isArray(candidate.watched_branches) ? candidate.watched_branches : [];
  if (candidate.repository_id !== null && candidate.repository_id !== undefined) {
    const current = (state.dashboard?.watches || [])
      .filter((watch) => String(watch.repository_id) === String(candidate.repository_id))
      .map((watch) => watch.branch);
    return uniqueNames([...known, ...current]);
  }
  // Discovery joins watched state by exact checkout path. A basename fallback
  // would merge unrelated repositories that happen to share the same name.
  return uniqueNames(known);
}

function candidateBranches(candidate) {
  return uiModel.prioritizeBranchNames(candidate.branches, [
    candidate.current_branch,
    candidate.default_branch,
    candidate.upstream_branch,
  ], 200);
}

function setLocalProjects(values) {
  state.discoveryError = null;
  state.discoveryErrorIsRaw = false;
  state.discoveryErrorCode = null;
  state.candidates = Array.isArray(values) ? values : [];
  state.localProjectPage = 1;
  state.matchedRemoteCanonical = null;
  renderLocalProjects();
}

function localProjectSearchText(candidate) {
  return [
    candidate.name,
    candidate.canonical_remote,
    candidate.current_branch,
    candidate.default_branch,
    candidate.upstream_branch,
  ].filter(Boolean).join(" ").toLowerCase();
}

function renderLocalProjects() {
  const container = document.getElementById("local-project-list");
  const scan = document.getElementById("scan-status");
  const queryInput = document.getElementById("local-project-search");
  if (state.dashboard && !hasConfiguredScanRoots()) {
    scan.hidden = true;
    queryInput.disabled = true;
    document.getElementById("local-project-count").textContent = t("未设置范围");
    document.getElementById("local-project-page-status").textContent = t("等待设置");
    document.getElementById("local-project-pagination").hidden = true;
    container.replaceChildren(emptyState(
      "先设置扫描目录",
      "Branch Radar 只扫描你明确选择的目录，不会使用写死路径或默认遍历主目录。",
      "设置扫描范围",
      openScanRootsDialog,
    ));
    syncScanRootsControls();
    return;
  }
  if (!Array.isArray(state.candidates)) return;
  queryInput.disabled = false;
  scan.hidden = false;
  scan.classList.add("is-complete");
  const available = state.candidates.filter((item) => !item.error && candidateBranches(item).length);
  const discoveryMessage = state.discoveryError
    ? localizedErrorMessage({ code: state.discoveryErrorCode, message: state.discoveryError }, "无法启动本地项目扫描")
    : "";
  if (state.discoveryError) {
    scan.querySelector("strong").textContent = t("扫描未完成");
    scan.querySelector("p").textContent = discoveryMessage;
  } else {
    scan.querySelector("strong").textContent = t("找到 {count} 个 Git checkout", { count: state.candidates.length });
    scan.querySelector("p").textContent = t("{count} 个可以关联并选择分支", { count: available.length });
  }
  const query = queryInput?.value.trim().toLowerCase() || "";
  const filtered = state.candidates.filter((candidate) => localProjectSearchText(candidate).includes(query));
  const pageCount = Math.max(1, Math.ceil(filtered.length / state.localProjectPageSize));
  state.localProjectPage = Math.min(Math.max(1, state.localProjectPage), pageCount);
  const offset = (state.localProjectPage - 1) * state.localProjectPageSize;
  const pageValues = filtered.slice(offset, offset + state.localProjectPageSize);
  document.getElementById("local-project-count").textContent = t("{count} 个项目", { count: state.candidates.length });
  const pageStatus = document.getElementById("local-project-page-status");
  if (pageStatus) {
    pageStatus.textContent = filtered.length
      ? t("第 {page} / {pages} 页 · {count} 个结果", {
        page: state.localProjectPage,
        pages: pageCount,
        count: filtered.length,
      })
      : t("0 个结果");
  }
  const pagination = document.getElementById("local-project-pagination");
  if (pagination) pagination.hidden = filtered.length === 0;
  const previous = document.getElementById("local-project-prev");
  const next = document.getElementById("local-project-next");
  if (previous) previous.disabled = state.localProjectPage <= 1;
  if (next) next.disabled = state.localProjectPage >= pageCount;
  if (state.discoveryError) {
    container.replaceChildren(emptyState("暂时无法读取本地项目", discoveryMessage, "重新扫描", startDiscovery, { rawDescription: true }));
    return;
  }
  if (!state.candidates.length) {
    container.replaceChildren(emptyState("没有发现 Git 仓库", "当前扫描目录中没有可识别的仓库。把仓库 clone 到扫描目录后刷新，或粘贴 GitHub 地址匹配已有 checkout。", "重新扫描", startDiscovery));
    return;
  }
  if (!filtered.length) {
    container.replaceChildren(emptyState("没有匹配的本地项目", "换一个项目名、远程仓库或分支名，或者清除搜索条件。", "清除搜索", () => {
      queryInput.value = "";
      state.localProjectPage = 1;
      renderLocalProjects();
      queryInput.focus();
    }));
    return;
  }
  const fragment = document.createDocumentFragment();
  pageValues.forEach((candidate) => {
    const branches = candidateBranches(candidate);
    const watched = watchedBranchesFor(candidate);
    const disabled = Boolean(candidate.error || !branches.length);
    const matched = Boolean(state.matchedRemoteCanonical && candidate.canonical_remote === state.matchedRemoteCanonical);
    const card = element("article", `local-project-card${disabled ? " is-disabled" : ""}${matched ? " is-matched" : ""}`);
    card.dataset.path = candidate.path || "";
    card.dataset.canonical = candidate.canonical_remote || "";
    const copy = element("div", "local-project-copy");
    const title = element("div", "local-project-title");
    title.append(element("strong", "", candidate.name || t("未命名仓库")));
    if (candidate.is_worktree) title.append(element("span", "status-chip is-muted", "worktree"));
    copy.append(
      title,
      element("span", "local-project-remote", candidate.canonical_remote || t("仅本地仓库")),
      element("small", "", watched.length
        ? t("当前 {branch} · 已关注 {count} 个分支", { branch: candidate.current_branch || t("未知分支"), count: watched.length })
        : t("当前 {branch} · {count} 个可选分支", { branch: candidate.current_branch || t("未知分支"), count: branches.length })),
    );
    const action = button(t(disabled ? "无法关联" : (watched.length ? "添加其他分支" : "选择分支")), "button button-secondary", () => openBranchPicker(candidate));
    action.disabled = disabled;
    card.append(copy, action);
    if (candidate.error) {
      card.append(element("p", "candidate-error", localizedErrorMessage({ message: candidate.error }, "这个仓库暂时不可用。")));
    }
    fragment.append(card);
  });
  container.replaceChildren(fragment);
}

function openBranchPicker(candidate) {
  if ((state.activeJob && state.activeJob.kind !== "clone") || state.watchSubmission) {
    showToast(t("当前任务完成后再选择其他分支。"), true);
    return;
  }
  const branches = candidateBranches(candidate);
  if (!branches.length) {
    showToast(t("这个仓库还没有可关注的分支。"), true);
    return;
  }
  state.selectedCandidate = candidate;
  state.watchSubmission = null;
  state.branchQuery = "";
  document.getElementById("branch-search").value = "";
  const preferred = candidate.upstream_branch || candidate.current_branch || candidate.default_branch || branches[0];
  const watched = new Set(watchedBranchesFor(candidate));
    document.getElementById("branch-dialog-title").textContent = t("选择 {repository} 的分支", {
      repository: candidate.name || t("仓库"),
    });
  document.getElementById("branch-dialog-subtitle").textContent = candidate.canonical_remote
    ? t("匹配 {remote}。勾选要观察的分支，或一键全选。", { remote: candidate.canonical_remote })
    : t("这是仅本地仓库。勾选要观察的分支，或一键全选。");
  const list = document.getElementById("branch-pick-list");
  const fragment = document.createDocumentFragment();
  branches.forEach((branch) => {
    const row = element("label", "branch-pick-row");
    row.dataset.branchSearch = branch.toLowerCase();
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = branch;
    checkbox.checked = branch === preferred || watched.has(branch);
    checkbox.disabled = watched.has(branch);
    checkbox.dataset.watched = watched.has(branch) ? "true" : "false";
    checkbox.addEventListener("change", updateBranchPickCount);
    const copy = element("span", "branch-pick-copy");
    copy.append(element("strong", "", branch));
    const notes = [];
    if (branch === candidate.current_branch) notes.push("当前分支");
    if (branch === candidate.default_branch && branch !== candidate.current_branch) notes.push("默认分支");
    if (watched.has(branch)) notes.push("已关注");
    if (notes.length) {
      const note = element("small", "", notes.map((item) => t(item)).join(" · "));
      note.dataset.branchNotes = JSON.stringify(notes);
      copy.append(note);
    }
    if (watched.has(branch)) row.classList.add("is-watched");
    row.append(checkbox, element("i"), copy);
    fragment.append(row);
  });
  list.replaceChildren(fragment);
  filterBranchRows();
  setWatchButtonState("idle");
  openDialog("branch-dialog");
  window.setTimeout(() => document.getElementById("branch-search")?.focus(), 0);
}

function selectedBranchNames() {
  return Array.from(document.querySelectorAll("#branch-pick-list input[type=checkbox]:checked:not(:disabled)"))
    .map((input) => input.value.trim())
    .filter(Boolean);
}

function localizeOpenBranchPicker() {
  const dialog = document.getElementById("branch-dialog");
  const candidate = state.selectedCandidate;
  if (!dialog?.open || !candidate || (state.activeJob?.kind === "watch" && jobIsActive(state.activeJob))) return;
  document.getElementById("branch-dialog-title").textContent = t("选择 {repository} 的分支", {
    repository: candidate.name || t("仓库"),
  });
  document.getElementById("branch-dialog-subtitle").textContent = candidate.canonical_remote
    ? t("匹配 {remote}。勾选要观察的分支，或一键全选。", { remote: candidate.canonical_remote })
    : t("这是仅本地仓库。勾选要观察的分支，或一键全选。");
  document.querySelectorAll("[data-branch-notes]").forEach((node) => {
    try {
      node.textContent = JSON.parse(node.dataset.branchNotes).map((item) => t(item)).join(" · ");
    } catch (_error) {
      // A malformed presentation hint must not affect branch selection.
    }
  });
  updateBranchPickCount();
}

function visibleAddableBranchBoxes() {
  return Array.from(document.querySelectorAll("#branch-pick-list .branch-pick-row:not([hidden]) input[type=checkbox]:not(:disabled)"));
}

function updateBranchPickCount() {
  const boxes = Array.from(document.querySelectorAll("#branch-pick-list input[type=checkbox]"));
  const selected = boxes.filter((input) => input.checked && input.dataset.watched !== "true").length;
  const watched = boxes.filter((input) => input.dataset.watched === "true").length;
  const visibleRows = Array.from(document.querySelectorAll("#branch-pick-list .branch-pick-row:not([hidden])")).length;
  document.getElementById("branch-pick-count").textContent = t(
    selected
      ? "已选 {selected} 个新增分支 · 显示 {visible}/{total}{watched}"
      : "尚未选择新增分支 · 显示 {visible}/{total}{watched}",
    {
      selected,
      visible: visibleRows,
      total: boxes.length,
      watched: watched ? t(" · {count} 个已关注", { count: watched }) : "",
    },
  );
  document.getElementById("confirm-watch-button").disabled = selected === 0 || Boolean(state.activeJob) || Boolean(state.watchSubmission);
  const selectAll = document.getElementById("select-all-branches");
  const visible = visibleAddableBranchBoxes();
  const allOn = visible.length > 0 && visible.every((input) => input.checked);
  const scoped = Boolean(state.branchQuery);
  selectAll.textContent = t(allOn ? (scoped ? "取消筛选结果" : "取消全选") : (scoped ? "全选筛选结果" : "全选可添加分支"));
  selectAll.disabled = !visible.length || Boolean(state.activeJob) || Boolean(state.watchSubmission);
}

function toggleSelectAllBranches() {
  const boxes = visibleAddableBranchBoxes();
  const allOn = boxes.length > 0 && boxes.every((input) => input.checked);
  boxes.forEach((input) => {
    input.checked = !allOn;
  });
  updateBranchPickCount();
}

function filterBranchRows() {
  state.branchQuery = document.getElementById("branch-search").value.trim().toLowerCase();
  const rows = Array.from(document.querySelectorAll("#branch-pick-list .branch-pick-row"));
  let visible = 0;
  rows.forEach((row) => {
    const matches = !state.branchQuery || row.dataset.branchSearch.includes(state.branchQuery);
    row.hidden = !matches;
    if (matches) visible += 1;
  });
  document.getElementById("branch-search-empty")?.remove();
  if (!visible && rows.length) {
    const empty = emptyState("没有匹配的分支", "换一个分支名称，或清除搜索条件。", "清除搜索", () => {
      const input = document.getElementById("branch-search");
      input.value = "";
      filterBranchRows();
      input.focus();
    });
    empty.id = "branch-search-empty";
    empty.classList.add("branch-search-empty");
    document.getElementById("branch-pick-list").append(empty);
  }
  updateBranchPickCount();
}

function setWatchButtonState(mode, count = 0) {
  const control = document.getElementById("confirm-watch-button");
  const label = document.getElementById("confirm-watch-label");
  const icon = ["submitting", "running"].includes(mode)
    ? "#icon-refresh"
    : ["success", "partial"].includes(mode)
      ? "#icon-check"
      : "#icon-plus";
  control.querySelector("use")?.setAttribute("href", icon);
  control.classList.toggle("is-success", mode === "success");
  const locked = ["submitting", "running", "success", "partial"].includes(mode);
  document.querySelectorAll("#branch-pick-list input[type=checkbox]").forEach((input) => {
    input.disabled = input.dataset.watched === "true" || locked;
  });
  control.setAttribute("aria-busy", ["submitting", "running"].includes(mode) ? "true" : "false");
  if (mode === "submitting") label.textContent = t("正在启动…");
  else if (mode === "running") label.textContent = t("正在建立基线…");
  else if (mode === "success") label.textContent = t("已关注 {count} 个分支", { count });
  else if (mode === "partial") label.textContent = t("已关注 {count} 个，部分待处理", { count });
  else if (mode === "error") label.textContent = t("重试关注");
  else label.textContent = t(WATCH_BUTTON_DEFAULT);
  if (mode === "idle") resetWatchProgress();
  if (mode === "idle" || mode === "error") state.watchSubmission = null;
  updateBranchPickCount();
}

async function submitWatchSelection(event) {
  event.preventDefault();
  const candidate = state.selectedCandidate;
  const branches = selectedBranchNames();
  if (!candidate?.path || !branches.length) {
    showToast(t("请至少选择一个分支。"), true);
    return;
  }
  if (state.activeJob || state.watchSubmission) return;
  state.watchSubmission = { candidate, branches, status: "submitting" };
  setWatchButtonState("submitting", branches.length);
  renderWatchProgress({
    kind: "watch",
    status: "queued",
    created_at: new Date().toISOString(),
    progress: {
      phase: "queued",
      label: "正在启动关注任务",
      detail: "即将读取仓库并获取所选分支的最新引用。",
      current: 0,
      total: branches.length,
      indeterminate: true,
    },
  });
  try {
    const job = await submitJob("/api/v1/watches", { entries: [{ path: candidate.path, branches }] });
    if (job?.kind === "watch") {
      if (state.watchSubmission) state.watchSubmission.status = "running";
      setWatchButtonState("running", state.watchSubmission?.branches.length || branches.length);
    } else {
      setWatchButtonState("error", branches.length);
    }
  } catch (_error) {
    setWatchButtonState("error", branches.length);
  }
}

function defaultCloneDirectory(remote) {
  const project = typeof remote?.project === "string" ? remote.project : "";
  const candidate = project.split("/").filter(Boolean).pop() || "repository";
  return candidate
    .replace(/\.git$/i, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 180) || "repository";
}

function cloneParentOptions() {
  return Array.from(document.querySelectorAll("#clone-parent-listbox [role=option]"));
}

function cloneParentSelectedIndex() {
  const select = document.getElementById("clone-parent");
  return Math.max(0, Array.from(select.options).findIndex((option) => option.value === select.value));
}

function setCloneParentInvalid(invalid) {
  const trigger = document.getElementById("clone-parent-trigger");
  trigger.toggleAttribute("aria-invalid", Boolean(invalid));
}

function activateCloneParentOption(index, { scroll = true } = {}) {
  const options = cloneParentOptions();
  if (!options.length) return;
  const bounded = Math.max(0, Math.min(options.length - 1, Number(index) || 0));
  options.forEach((option, optionIndex) => {
    option.classList.toggle("is-active", optionIndex === bounded);
  });
  const active = options[bounded];
  document.getElementById("clone-parent-trigger").setAttribute("aria-activedescendant", active.id);
  if (scroll && typeof active.scrollIntoView === "function") {
    active.scrollIntoView({ block: "nearest" });
  }
}

function closeCloneParentPicker({ restoreFocus = false } = {}) {
  const trigger = document.getElementById("clone-parent-trigger");
  const listbox = document.getElementById("clone-parent-listbox");
  if (!trigger || !listbox) return;
  listbox.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  trigger.removeAttribute("aria-activedescendant");
  cloneParentOptions().forEach((option) => option.classList.remove("is-active"));
  if (restoreFocus) trigger.focus();
}

function openCloneParentPicker({ edge = null, move = 0 } = {}) {
  const trigger = document.getElementById("clone-parent-trigger");
  const listbox = document.getElementById("clone-parent-listbox");
  const options = cloneParentOptions();
  if (!trigger || !listbox || trigger.disabled || !options.length) return;
  listbox.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  let activeIndex = cloneParentSelectedIndex();
  if (edge === "first") activeIndex = 0;
  else if (edge === "last") activeIndex = options.length - 1;
  else if (move) activeIndex = uiModel.moveListboxActiveIndex(options.length, activeIndex, move > 0 ? "ArrowDown" : "ArrowUp");
  activateCloneParentOption(activeIndex);
}

function setCloneParentSelection(value, { close = false, focus = false, emit = false } = {}) {
  const select = document.getElementById("clone-parent");
  const trigger = document.getElementById("clone-parent-trigger");
  const label = document.getElementById("clone-parent-value");
  const options = cloneParentOptions();
  const selectedIndex = Array.from(select.options).findIndex((option) => option.value === value);
  if (selectedIndex < 0) return false;
  select.value = value;
  label.dataset.i18nSkip = "true";
  label.textContent = value;
  trigger.title = value;
  options.forEach((option, index) => {
    const selected = index === selectedIndex;
    option.classList.toggle("is-selected", selected);
    option.setAttribute("aria-selected", selected ? "true" : "false");
  });
  setCloneParentInvalid(false);
  if (emit) select.dispatchEvent(new Event("change", { bubbles: true }));
  if (close) closeCloneParentPicker({ restoreFocus: focus });
  return true;
}

function renderCloneParentPicker(roots) {
  const select = document.getElementById("clone-parent");
  const listbox = document.getElementById("clone-parent-listbox");
  closeCloneParentPicker();
  select.replaceChildren(...roots.map((root) => {
    const option = element("option", "", root);
    option.value = root;
    option.dataset.i18nSkip = "true";
    return option;
  }));
  const fragment = document.createDocumentFragment();
  roots.forEach((root, index) => {
    const option = element("li", "clone-parent-option");
    option.id = `clone-parent-option-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", index === 0 ? "true" : "false");
    option.dataset.value = root;
    option.title = root;
    const folder = element("span", "clone-parent-option-icon");
    folder.setAttribute("aria-hidden", "true");
    folder.append(createIcon("folder"));
    const path = element("span", "clone-parent-option-path", root);
    path.dataset.i18nSkip = "true";
    const check = createIcon("check");
    check.classList.add("clone-parent-option-check");
    option.append(folder, path, check);
    option.addEventListener("pointermove", () => activateCloneParentOption(index, { scroll: false }));
    option.addEventListener("click", () => setCloneParentSelection(root, { close: true, focus: true, emit: true }));
    fragment.append(option);
  });
  listbox.replaceChildren(fragment);
  if (roots.length) setCloneParentSelection(roots[0]);
}

function bindCloneParentPicker() {
  const picker = document.getElementById("clone-parent-picker");
  const trigger = document.getElementById("clone-parent-trigger");
  const listbox = document.getElementById("clone-parent-listbox");
  if (!picker || !trigger || !listbox) return;
  trigger.addEventListener("click", () => {
    if (listbox.hidden) openCloneParentPicker();
    else closeCloneParentPicker();
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      closeCloneParentPicker();
      return;
    }
    if (event.key === "Escape") {
      if (listbox.hidden) return;
      event.preventDefault();
      event.stopPropagation();
      closeCloneParentPicker();
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const options = cloneParentOptions();
      if (!options.length) return;
      if (listbox.hidden) {
        if (event.key === "Home") openCloneParentPicker({ edge: "first" });
        else if (event.key === "End") openCloneParentPicker({ edge: "last" });
        else openCloneParentPicker({ move: event.key === "ArrowDown" ? 1 : -1 });
        return;
      }
      const current = options.findIndex((option) => option.classList.contains("is-active"));
      activateCloneParentOption(uiModel.moveListboxActiveIndex(options.length, current, event.key));
      return;
    }
    if (!["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    if (listbox.hidden) {
      openCloneParentPicker();
      return;
    }
    const options = cloneParentOptions();
    const active = options.find((option) => option.classList.contains("is-active")) || options[cloneParentSelectedIndex()];
    if (active) setCloneParentSelection(active.dataset.value, { close: true, focus: true, emit: true });
  });
  document.addEventListener("pointerdown", (event) => {
    if (!picker.contains(event.target)) closeCloneParentPicker();
  });
}

function cloneRequestInFlight() {
  return state.cloneSubmission?.status === "submitting"
    || (state.activeJob?.kind === "clone" && jobIsActive(state.activeJob));
}

function cloneErrorMessage(code, fallback = "") {
  return t(ERROR_COPY[code] || fallback || "克隆失败。请检查网络连接以及当前 Git 凭证是否有权访问该仓库。");
}

function resetCloneDialog() {
  const dialog = document.getElementById("clone-dialog");
  closeCloneParentPicker();
  dialog.removeAttribute("aria-busy");
  document.getElementById("clone-confirmation").hidden = false;
  document.getElementById("clone-error").textContent = "";
  document.getElementById("clone-submit-button").hidden = false;
  document.getElementById("clone-submit-button").disabled = false;
  document.getElementById("clone-submit-button").querySelector("span").textContent = t("开始克隆");
  document.getElementById("clone-submit-button").querySelector("use")?.setAttribute("href", "#icon-folder");
  document.getElementById("clone-dismiss-button").hidden = false;
  document.getElementById("clone-dismiss-button").disabled = false;
  document.getElementById("clone-dialog-close").disabled = false;
  document.getElementById("clone-parent-trigger").disabled = false;
  document.getElementById("clone-footer-copy").textContent = t("克隆仓库时会访问远程地址并写入所选本地目录。");
  state.cloneProgressPhase = null;
  state.cloneCancelling = false;
  state.cloneValidationErrorCode = null;
  state.cloneTerminalJob = null;
}

function localizeCloneConfirmation() {
  const dialog = document.getElementById("clone-dialog");
  if (!dialog?.open || state.activeJob?.kind === "clone" || state.cloneTerminalJob) return;
  document.getElementById("clone-submit-button").querySelector("span").textContent = t("开始克隆");
  document.getElementById("clone-footer-copy").textContent = t("克隆仓库时会访问远程地址并写入所选本地目录。");
  const code = state.cloneValidationErrorCode;
  if (!code) return;
  document.getElementById("clone-error").textContent = code === "directory_required"
    ? t("请输入目录名。")
    : cloneErrorMessage(code);
}

function openCloneConfirmation(remote, url) {
  const roots = configuredScanRoots();
  if (!roots.length) {
    state.pendingRemoteUrl = url;
    openScanRootsDialog();
    return;
  }
  if (state.cloneTerminalJob) dismissCloneTask();
  state.clonePreview = { remote, url };
  state.cloneSubmission = null;
  resetCloneDialog();
  const canonical = remote?.canonical || url;
  document.getElementById("clone-repository").textContent = canonical;
  renderCloneParentPicker(roots);
  document.getElementById("clone-directory-name").value = defaultCloneDirectory(remote);
  setCloneParentInvalid(false);
  document.getElementById("clone-directory-name").removeAttribute("aria-invalid");
  openDialog("clone-dialog");
  window.requestAnimationFrame(() => document.getElementById("clone-parent-trigger").focus());
}

function normalizedClonePhase(job) {
  const phase = job?.progress?.phase || job?.status || "prepare";
  return {
    queued: "prepare",
    validate: "prepare",
    prepare: "prepare",
    connect: "connect",
    clone: "download",
    download: "download",
    inspect: "verify",
    verify: "verify",
    finalize: "complete",
    complete: "complete",
  }[phase] || state.cloneProgressPhase || "prepare";
}

function clonePhaseCopy(phase, job) {
  if (job?.status === "cancelling" || job?.progress?.phase === "cancelling") {
    return ["正在取消克隆", "正在停止 Git 并清理未完成的目标目录。"];
  }
  if (job?.status === "cancelled" || ["clone_cancelled", "operation_cancelled"].includes(job?.error_code)) {
    return ["克隆已取消", "已取消克隆；未完成的目标目录不会被保留。"];
  }
  return {
    prepare: ["正在准备克隆", "正在验证仓库地址和目标目录。"],
    connect: ["正在连接远程仓库", "Git 正在连接远程并确认访问权限。"],
    download: ["正在下载仓库对象", "下载时间取决于仓库大小和当前网络。"],
    verify: ["正在验证本地仓库", "正在读取分支并确认 checkout 可以安全使用。"],
    complete: ["正在完成克隆", "仓库已经就绪，正在准备分支选择。"],
  }[phase] || ["正在克隆仓库", "请保持本地服务运行。"];
}

function cloneActualProgressPercent(job) {
  if (job?.status === "succeeded") return 100;
  const rawPercent = job?.progress?.percent;
  if (rawPercent === null || rawPercent === undefined || rawPercent === "") return null;
  const percent = Math.max(0, Math.min(Number(rawPercent), 100));
  return Number.isFinite(percent) ? Math.round(percent) : null;
}

function cloneTaskRepository(job) {
  const candidate = job?.result?.repository;
  return identityText(
    candidate?.canonical_remote
      || state.cloneSubmission?.repository
      || state.clonePreview?.remote?.canonical
      || storedCloneTask()?.repository
      || candidate?.name,
    t("GitHub 仓库"),
  );
}

function setCloneTaskExpanded(expanded, { focus = false } = {}) {
  state.cloneTaskExpanded = Boolean(expanded);
  const details = document.getElementById("task-details");
  const toggle = document.getElementById("task-toggle");
  details.hidden = !state.cloneTaskExpanded;
  toggle.setAttribute("aria-expanded", state.cloneTaskExpanded ? "true" : "false");
  toggle.setAttribute("aria-label", t(state.cloneTaskExpanded ? "收起任务详情" : "展开任务详情"));
  document.getElementById("task-bar").classList.toggle("is-expanded", state.cloneTaskExpanded);
  document.body.classList.toggle("has-expanded-task", state.cloneTaskExpanded);
  if (focus) window.requestAnimationFrame(() => toggle.focus());
}

function renderCloneTask(job) {
  if (job?.kind !== "clone") return;
  const progress = job.progress || {};
  const phase = normalizedClonePhase(job);
  const active = jobIsActive(job);
  const cancelled = job.status === "cancelled" || ["clone_cancelled", "operation_cancelled"].includes(job.error_code);
  const failed = job.status === "failed" && !cancelled;
  const succeeded = job.status === "succeeded";
  if (!active) state.cloneTerminalJob = job;
  if (!cancelled && phase !== "complete" && CLONE_PHASE_ORDER.includes(phase)) state.cloneProgressPhase = phase;
  let [fallbackTitle, fallbackDetail] = clonePhaseCopy(phase, job);
  if (failed) {
    fallbackTitle = "克隆失败";
    fallbackDetail = cloneErrorMessage(job.error_code, job.error);
  }
  const useBackendCopy = i18n.locale() === "zh-CN";
  const title = useBackendCopy && progress.label ? progress.label : fallbackTitle;
  const detail = useBackendCopy && progress.detail ? progress.detail : fallbackDetail;
  const requiresNewUrl = ["invalid_remote", "remote_required", "invalid_clone_request"].includes(job.error_code);
  const canRetry = Boolean(state.clonePreview?.url && state.cloneSubmission?.parent && state.cloneSubmission?.directoryName) && !requiresNewUrl;
  const taskBar = document.getElementById("task-bar");
  taskBar.hidden = false;
  taskBar.classList.toggle("is-succeeded", succeeded);
  taskBar.classList.toggle("is-failed", failed);
  taskBar.classList.toggle("is-cancelled", cancelled);
  document.body.classList.add("has-task-bar");
  document.getElementById("task-context").textContent = t("克隆仓库 · {repository}", {
    repository: cloneTaskRepository(job),
  });
  document.getElementById("task-title").textContent = t(title);
  document.getElementById("task-description").textContent = t(active
    ? "任务在本机后台继续，可以浏览其他页面。"
    : succeeded
      ? "仓库已经就绪，可以继续选择关注分支。"
      : cancelled
        ? "克隆已安全取消。"
        : "任务没有完成，展开可查看原因和重试选项。");
  document.getElementById("task-elapsed").textContent = elapsedJobText(job);

  const percent = cloneActualProgressPercent(job);
  const percentCopy = document.getElementById("task-percent");
  percentCopy.hidden = percent === null;
  percentCopy.textContent = percent === null ? "" : `${percent}%`;
  const track = document.getElementById("task-progress-track");
  const indeterminate = active && phase !== "complete" && percent === null;
  track.classList.toggle("is-indeterminate", indeterminate);
  taskBar.style.setProperty("--task-progress", String(percent === null ? 0 : percent / 100));
  if (indeterminate) {
    track.removeAttribute("aria-valuenow");
    track.setAttribute("aria-valuetext", t("{label}，已用时 {elapsed}", {
      label: title,
      elapsed: elapsedJobText(job),
    }));
  } else if (percent === null) {
    track.removeAttribute("aria-valuenow");
    track.setAttribute("aria-valuetext", failed || cancelled ? cloneErrorMessage(job.error_code, job.error) : t(title));
  } else {
    track.setAttribute("aria-valuenow", String(percent));
    track.setAttribute("aria-valuetext", t("{stage} · {percent}%", { stage: title, percent }));
  }

  const fallbackPhase = state.cloneProgressPhase || "prepare";
  const displayedPhase = CLONE_PHASE_ORDER.includes(phase) ? phase : fallbackPhase;
  const activePhase = Math.max(0, CLONE_PHASE_ORDER.indexOf(displayedPhase));
  document.querySelectorAll("#task-stages [data-clone-phase]").forEach((item, index) => {
    item.classList.toggle("is-complete", succeeded || index < activePhase);
    item.classList.toggle("is-active", active && index === activePhase);
  });
  const message = failed || cancelled ? cloneErrorMessage(job.error_code, job.error) : "";
  document.getElementById("task-detail-copy").textContent = state.cloneSubmission?.directoryName
    ? t("{detail} 目标目录：{directory}。", { detail, directory: state.cloneSubmission.directoryName })
    : t(detail);
  document.getElementById("task-error").textContent = message;
  document.getElementById("task-stages").hidden = false;
  const cancel = document.getElementById("task-cancel-button");
  cancel.hidden = !active;
  cancel.disabled = !job.id || state.cloneCancelling || job.status === "cancelling";
  cancel.textContent = t(job.status === "cancelling" ? "正在取消" : "取消");
  const toggle = document.getElementById("task-toggle");
  toggle.hidden = false;
  const edit = document.getElementById("task-edit-button");
  const retry = document.getElementById("task-retry-button");
  const open = document.getElementById("task-open-button");
  const dismiss = document.getElementById("task-dismiss-button");
  document.querySelector("#task-details .task-actions").hidden = active;
  edit.hidden = active || succeeded;
  retry.hidden = active || succeeded || !canRetry;
  open.hidden = !succeeded;
  dismiss.hidden = active;
  if (!active && !state.cloneTaskExpanded) state.cloneTaskExpanded = true;
  setCloneTaskExpanded(state.cloneTaskExpanded);
  const announcementKey = `${job.id || "pending"}:${job.status}:${phase}:${percent ?? "unknown"}`;
  if (taskBar.dataset.announcementKey !== announcementKey) {
    document.getElementById("task-announcement").textContent = t("{label}。{detail}", {
      label: title,
      detail: failed || cancelled ? message : detail,
    });
    taskBar.dataset.announcementKey = announcementKey;
  }
}

async function submitClone(event) {
  event.preventDefault();
  if (state.activeJob || state.cloneSubmission?.status === "submitting") return;
  const preview = state.clonePreview;
  const parent = document.getElementById("clone-parent");
  const parentTrigger = document.getElementById("clone-parent-trigger");
  const directory = document.getElementById("clone-directory-name");
  const error = document.getElementById("clone-error");
  error.textContent = "";
  state.cloneValidationErrorCode = null;
  setCloneParentInvalid(false);
  directory.removeAttribute("aria-invalid");
  const parentValue = parent.value;
  const directoryName = directory.value.trim();
  if (!configuredScanRoots().includes(parentValue)) {
    state.cloneValidationErrorCode = "clone_parent_required";
    setCloneParentInvalid(true);
    error.textContent = t("请选择一个已配置的扫描目录。");
    parentTrigger.focus();
    return;
  }
  if (!directoryName) {
    state.cloneValidationErrorCode = "directory_required";
    directory.setAttribute("aria-invalid", "true");
    error.textContent = t("请输入目录名。");
    directory.focus();
    return;
  }
  if ([".", ".."].includes(directoryName) || /[\\/\0]/.test(directoryName)) {
    state.cloneValidationErrorCode = "invalid_clone_name";
    directory.setAttribute("aria-invalid", "true");
    error.textContent = cloneErrorMessage("invalid_clone_name");
    directory.focus();
    return;
  }
  if (!preview?.url) {
    state.cloneValidationErrorCode = "invalid_remote";
    error.textContent = cloneErrorMessage("invalid_remote");
    return;
  }
  state.cloneProgressPhase = null;
  await startCloneRequest({
    url: preview.url,
    parent: parentValue,
    directoryName,
    repository: preview.remote?.canonical || "",
  }, { closeConfirmation: true });
}

async function startCloneRequest(submission, { closeConfirmation = false } = {}) {
  if (state.activeJob || state.cloneSubmission?.status === "submitting") return false;
  const optimisticJob = {
    kind: "clone",
    status: "queued",
    created_at: new Date().toISOString(),
    progress: { phase: "prepare", indeterminate: true },
  };
  const previousTerminalJob = state.cloneTerminalJob;
  state.cloneTerminalJob = null;
  state.cloneTaskExpanded = false;
  state.cloneCancelling = false;
  state.cloneProgressPhase = null;
  state.cloneSubmission = { ...submission, status: "submitting" };
  const submit = document.getElementById("clone-submit-button");
  const dialog = document.getElementById("clone-dialog");
  if (dialog.open) {
    dialog.setAttribute("aria-busy", "true");
    submit.disabled = true;
    submit.querySelector("span").textContent = t("正在启动…");
  } else {
    renderCloneTask(optimisticJob);
  }
  try {
    const job = await submitJob("/api/v1/repository-clones", {
      url: submission.url,
      parent: submission.parent,
      directory_name: submission.directoryName,
    });
    if (job?.kind !== "clone") {
      state.cloneSubmission = { ...submission, status: "failed" };
      state.cloneTerminalJob = previousTerminalJob;
      if (dialog.open) {
        document.getElementById("clone-error").textContent = t("已有任务正在运行，请等待当前任务完成。");
      }
      return false;
    }
    if (state.cloneSubmission) {
      state.cloneSubmission.jobId = job.id;
      state.cloneSubmission.status = "running";
    }
    rememberCloneJob(job);
    if (closeConfirmation && dialog.open) closeDialog("clone-dialog");
    return true;
  } catch (caught) {
    const message = localizedErrorMessage(caught, "克隆失败。请检查网络连接以及当前 Git 凭证是否有权访问该仓库。");
    state.cloneSubmission = { ...submission, status: "failed" };
    const failedJob = {
      ...optimisticJob,
      status: "failed",
      error_code: caught.code,
      error: message,
    };
    state.cloneTerminalJob = failedJob;
    if (dialog.open) {
      state.cloneValidationErrorCode = caught.code || "clone_failed";
      document.getElementById("clone-error").textContent = message;
      document.getElementById("clone-error").focus();
    } else {
      renderCloneTask(failedJob);
    }
    return false;
  } finally {
    if (dialog.open) {
      dialog.removeAttribute("aria-busy");
      submit.disabled = false;
      submit.querySelector("span").textContent = t("开始克隆");
    }
  }
}

async function cancelClone() {
  const job = state.activeJob;
  if (job?.kind !== "clone" || !job.id || !jobIsActive(job) || state.cloneCancelling) return;
  state.cloneCancelling = true;
  renderCloneTask({
    ...job,
    status: "cancelling",
    progress: { ...(job.progress || {}), phase: "cancelling" },
  });
  try {
    const response = await api(`/api/v1/jobs/${encodeURIComponent(job.id)}/cancel`, {
      method: "POST",
      body: {},
    });
    if (state.activeJob?.id !== job.id) return;
    if (response.job?.id === job.id) {
      if (jobIsActive(response.job)) {
        resumeJob(response.job);
      } else {
        state.pollGeneration += 1;
        if (state.pollingTimer) window.clearTimeout(state.pollingTimer);
        state.pollingTimer = null;
        state.activeJob = null;
        hideTask();
        if (response.job.status === "succeeded") await handleJobResult(response.job);
        else handleJobFailure(response.job);
      }
    }
  } catch (caught) {
    state.cloneCancelling = false;
    if (state.activeJob?.id === job.id) resumeJob(state.activeJob);
    document.getElementById("task-error").textContent = caught.code
      ? cloneErrorMessage(caught.code, caught.message)
      : localizedErrorMessage(caught, "操作失败");
    document.getElementById("task-cancel-button").disabled = false;
    setCloneTaskExpanded(true);
  }
}

function cloneTaskSafeRemote() {
  if (state.clonePreview?.url) return state.clonePreview.url;
  const canonical = identityText(state.cloneSubmission?.repository || storedCloneTask()?.repository, "");
  const match = /^github\.com\/([A-Za-z0-9][A-Za-z0-9-]*)\/([A-Za-z0-9._-]+)$/i.exec(canonical);
  return match ? `https://github.com/${match[1]}/${match[2]}.git` : "";
}

async function retryCloneTask() {
  if (state.activeJob) return;
  const url = cloneTaskSafeRemote();
  const submission = state.cloneSubmission;
  if (!url || !submission?.parent || !submission.directoryName) {
    editCloneTask();
    return;
  }
  await startCloneRequest({ ...submission, url, repository: submission.repository || storedCloneTask()?.repository || "" });
}

function editCloneTask() {
  if (state.activeJob?.kind === "clone") return;
  const reference = storedCloneTask();
  const submission = state.cloneSubmission || {};
  const canonical = identityText(submission.repository || reference?.repository, "");
  const url = cloneTaskSafeRemote();
  if (!url) {
    dismissCloneTask();
    location.hash = "projects";
    window.requestAnimationFrame(() => document.getElementById("remote-url")?.focus());
    return;
  }
  const parts = canonical.split("/");
  const remote = { canonical, host: "github.com", project: parts.slice(1).join("/") };
  const parent = submission.parent;
  const directoryName = submission.directoryName || reference?.directoryName;
  dismissCloneTask({ preservePreview: true });
  openCloneConfirmation(remote, url);
  if (parent && configuredScanRoots().includes(parent)) setCloneParentSelection(parent);
  if (directoryName) document.getElementById("clone-directory-name").value = directoryName;
}

function openCloneTaskResult() {
  const candidate = state.cloneTerminalJob?.result?.repository;
  if (!candidate || typeof candidate !== "object") return;
  dismissCloneTask();
  openBranchPicker(candidate);
}

function dismissCloneTask({ preservePreview = false } = {}) {
  clearStoredCloneJob();
  state.cloneTerminalJob = null;
  state.cloneSubmission = null;
  state.cloneTaskExpanded = false;
  state.cloneProgressPhase = null;
  if (!preservePreview) state.clonePreview = null;
  hideTask();
}

async function associateRemote(event) {
  event.preventDefault();
  const input = document.getElementById("remote-url");
  const error = document.getElementById("remote-url-error");
  error.textContent = "";
  input.removeAttribute("aria-invalid");
  const url = input.value.trim();
  if (!url) {
    error.textContent = t("请输入 GitHub 仓库地址。");
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return;
  }
  if (!hasConfiguredScanRoots()) {
    state.pendingRemoteUrl = url;
    error.textContent = t("请先设置扫描目录；保存后会在所选范围中查找这个仓库。");
    openScanRootsDialog();
    return;
  }
  if (state.candidates === null) {
    state.pendingRemoteUrl = url;
    await startDiscovery();
    return;
  }
  await matchRemoteUrl(url);
}

async function matchRemoteUrl(url) {
  const error = document.getElementById("remote-url-error");
  const input = document.getElementById("remote-url");
  error.textContent = "";
  try {
    const payload = await api("/api/v1/remotes/preview", { method: "POST", body: { url } });
    const canonical = payload.remote?.canonical;
    const remoteHost = String(payload.remote?.host || "").toLowerCase();
    const matches = (state.candidates || []).filter((item) => {
      if (!item.canonical_remote || !canonical) return false;
      return remoteHost === "github.com"
        ? item.canonical_remote.toLowerCase() === canonical.toLowerCase()
        : item.canonical_remote === canonical;
    });
    if (!matches.length) {
      if (remoteHost !== "github.com") {
        const message = t("未找到本地 checkout，自动克隆目前仅支持 GitHub。");
        error.textContent = message;
        input.setAttribute("aria-invalid", "true");
        showToast(message, true);
        return;
      }
      input.removeAttribute("aria-invalid");
      openCloneConfirmation(payload.remote || { canonical }, url);
      return;
    }
    const usable = matches.find((item) => !item.error && candidateBranches(item).length) || matches[0];
    state.matchedRemoteCanonical = usable.canonical_remote || canonical;
    const localSearch = document.getElementById("local-project-search");
    localSearch.value = "";
    const targetIndex = Math.max(0, state.candidates.indexOf(usable));
    state.localProjectPage = Math.floor(targetIndex / state.localProjectPageSize) + 1;
    renderLocalProjects();
    const card = Array.from(document.querySelectorAll(".local-project-card")).find(
      (item) => item.dataset.path === (usable.path || ""),
    );
    card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (matches.length > 1) {
      showToast(t("找到 {count} 个本地 checkout，已打开第一个可关联的仓库。", { count: matches.length }));
    }
    openBranchPicker(usable);
  } catch (caught) {
    error.textContent = localizedErrorMessage(caught, "无法识别这个仓库地址。");
    input.setAttribute("aria-invalid", "true");
    showToast(localizedErrorMessage(caught, "无法识别这个仓库地址。"), true);
  }
}

async function submitCheck(event) {
  event.preventDefault();
  const payload = {
    fetch: true,
    use_codex: document.getElementById("check-codex").checked,
    publish: document.getElementById("check-publish").checked,
  };
  try {
    const job = await submitCheckJob(payload, "all");
    if (job?.kind === "check") {
      closeDialog("check-dialog");
      resetCheckOptions();
    }
  } catch (_error) {
    // Job submission error is already visible.
  }
}

function pendingCheckProgress(scope, payload) {
  const count = Array.isArray(payload.branch_ids) ? payload.branch_ids.length : 0;
  const label = scope === "repository"
    ? "正在提交仓库检查"
    : count
      ? t("正在提交 {count} 个分支的检查", { count })
      : "正在提交检查任务";
  return {
    kind: "check",
    status: "queued",
    created_at: new Date().toISOString(),
    progress: {
      label,
      detail: "任务创建后会继续显示抓取与分析进度。",
      current: 0,
      total: 0,
      indeterminate: true,
    },
  };
}

async function submitCheckJob(payload, scope) {
  if (checkOperationBusy()) {
    showToast(t("已有任务正在运行，请等待当前任务完成。"), true);
    return null;
  }
  const submission = { scope, pending: true, jobId: null };
  state.checkSubmission = submission;
  showTask(pendingCheckProgress(scope, payload));
  try {
    const job = await submitJob("/api/v1/checks", payload);
    if (state.checkSubmission === submission) {
      if (job?.kind === "check") {
        submission.pending = false;
        submission.jobId = job.id;
      } else {
        state.checkSubmission = null;
      }
    }
    return job;
  } catch (error) {
    if (state.checkSubmission === submission) state.checkSubmission = null;
    if (!state.activeJob) hideTask();
    throw error;
  }
}

async function submitSafeCheck(branchIds = null, scope = "all") {
  const payload = {
    fetch: true,
    use_codex: false,
    publish: false,
  };
  if (Array.isArray(branchIds)) payload.branch_ids = branchIds;
  try {
    await submitCheckJob(payload, scope);
  } catch (_error) {
    // Job submission error is already visible.
  }
}

async function submitRepositoryCheck(repositoryId) {
  if (!Number.isSafeInteger(repositoryId) || repositoryId <= 0) {
    showToast(t("当前仓库缺少可用的本地编号。"), true);
    return;
  }
  try {
    await submitCheckJob({
      fetch: true,
      use_codex: false,
      publish: false,
      repository_id: repositoryId,
    }, "repository");
  } catch (_error) {
    // Job submission error is already visible.
  }
}

function submitSelectedCheck() {
  const branchIds = Array.from(state.selectedWatchIds);
  if (!branchIds.length) {
    showToast(t("请先选择要检查的分支。"), true);
    return;
  }
  submitSafeCheck(branchIds, "selected");
}

function submitScopedCheck() {
  const branchId = Number(state.activityScope?.branchId);
  if (!Number.isSafeInteger(branchId) || branchId <= 0) {
    showToast(t("当前分支无法检查，请返回关注分支后重试。"), true);
    return;
  }
  submitSafeCheck([branchId], "branch");
}

function openPublishConfirmation() {
  if (!state.dashboard?.settings?.lark_document_configured) {
    location.hash = "settings";
    showToast(t("请先关联飞书文档。"), true);
    return;
  }
  const count = Number(state.dashboard?.counts?.pending_deliveries || 0);
  document.getElementById("publish-count-copy").textContent = count
    ? t("将对账并处理 {count} 条待发布事件", { count })
    : t("当前没有已知待发布事件，将执行一次安全对账");
  openDialog("publish-dialog");
}

async function publishPending(event) {
  event.preventDefault();
  try {
    const job = await submitJob("/api/v1/publications", { limit: 1000 });
    if (job?.kind === "publish") closeDialog("publish-dialog");
  } catch (_error) {
    // Job submission error is already visible.
  }
}

async function connectLark(event) {
  event.preventDefault();
  const input = document.getElementById("lark-document");
  const error = document.getElementById("lark-document-error");
  error.textContent = "";
  if (!input.value.trim()) {
    error.textContent = t("请输入飞书文档链接或 token。");
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return;
  }
  input.removeAttribute("aria-invalid");
  try {
    await submitJob("/api/v1/lark/connect", { document: input.value.trim() });
  } catch (_error) {
    // Job submission error is already visible.
  }
}

function newRequestId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

async function createLark(event) {
  event.preventDefault();
  const title = document.getElementById("lark-title").value.trim();
  if (!title) {
    document.getElementById("lark-title").focus();
    showToast(t("请输入飞书文档标题。"), true);
    return;
  }
  if (!state.larkCreateRequestId) state.larkCreateRequestId = newRequestId();
  savePendingLarkCreate(title);
  try {
    const job = await submitJob("/api/v1/lark/create", { title, request_id: state.larkCreateRequestId });
    if (job?.kind === "lark_create") closeDialog("create-lark-dialog");
  } catch (_error) {
    // Keep the same request id so an accidental retry cannot create a second document.
  }
}

async function openEvent(eventId) {
  const request = ++state.eventRequest;
  const status = document.getElementById("event-detail-status");
  status.textContent = t("正在读取事件详情");
  document.getElementById("event-dialog-title").textContent = t("正在读取事件");
  document.getElementById("event-dialog-meta").textContent = "CHANGE EVENT";
  document.getElementById("event-dialog-subtitle").textContent = "";
  openDialog("event-dialog");
  const content = document.getElementById("event-detail");
  content.setAttribute("aria-busy", "true");
  content.replaceChildren(emptyState("正在读取事件详情", "正在组合摘要、提交和文件信息。"));
  try {
    const payload = await api(`/api/v1/events/${eventId}`);
    if (request !== state.eventRequest) return;
    renderEventDetail(payload.event);
  } catch (error) {
    if (request !== state.eventRequest) return;
    status.textContent = t("事件详情读取失败");
    content.replaceChildren(emptyState("事件详情不可用", localizedErrorMessage(error, "请稍后重试。"), "重新读取", () => openEvent(eventId), { rawDescription: true }));
  } finally {
    if (request === state.eventRequest) content.setAttribute("aria-busy", "false");
  }
}

function eventTimelineContext(event) {
  const timeline = eventTimelineView(event);
  const context = element("section", `event-time-context is-${timeline.basis}`);
  context.setAttribute("aria-label", t("事件时间"));
  const primary = element("time", "event-time-primary", timelineTimeCopy(timeline));
  if (timeline.primaryTimeMs !== null) primary.dateTime = new Date(timeline.primaryTimeMs).toISOString();
  context.append(primary);

  const secondaryCopy = timelineSecondaryCopy(timeline);
  if (secondaryCopy) {
    const secondary = element("time", "event-time-secondary", secondaryCopy);
    secondary.dateTime = new Date(timeline.detectedTimeMs).toISOString();
    context.append(secondary);
  }
  if (
    timeline.basis === "tip_commit"
    && timeline.rangeComplete
    && timeline.firstCommittedAtMs !== null
    && timeline.lastCommittedAtMs !== null
    && Math.abs(timeline.lastCommittedAtMs - timeline.firstCommittedAtMs) >= 60_000
  ) {
    context.append(uiElement("span", "event-time-range", "提交范围 {start} – {end}", {
      start: formatTime(timeline.firstCommittedAtMs),
      end: formatTime(timeline.lastCommittedAtMs),
    }));
  }
  if (timeline.basis === "detected") {
    const caveat = element("p", "event-time-caveat");
    caveat.append(createIcon("warning"), uiElement("span", "", "未取得可靠提交时间；此变化按检查时间定位。"));
    context.append(caveat);
  }
  return context;
}

function renderEventDetail(event) {
  state.currentEvent = event;
  document.getElementById("event-dialog-title").textContent = event.summary?.title || t("{repository} 分支变化", {
    repository: event.repository,
  });
  const eventKind = kindCopy[event.kind] ? t(kindCopy[event.kind]) : event.kind;
  const timeline = eventTimelineView(event);
  document.getElementById("event-dialog-meta").textContent = `${eventKind || t("变化")} / ${event.summary_source === "codex" ? "CODEX" : "RULES"}`;
  document.getElementById("event-dialog-subtitle").textContent = `${event.repository} · ${event.branch} · ${timelineTimeCopy(timeline)}`;
  document.getElementById("event-detail-status").textContent = t("事件详情已加载");
  const container = document.getElementById("event-detail");
  container.replaceChildren();
  const grid = element("div", "event-detail-grid");
  const main = element("div");
  main.append(eventTimelineContext(event));
  main.append(element("p", "detail-overview", event.summary?.overview || t("本次事件没有可用摘要。")));
  if (!event.analysis_complete || event.commits_truncated) {
    const alert = element("div", "detail-alert");
    alert.append(createIcon("warning"), uiElement("span", "", "历史信息不完整或已裁剪；提交数量与净差异可能只是样本，请结合远程仓库确认。"));
    main.append(alert);
  }
  const stats = element("div", "detail-stats");
  [
    [event.stats?.commits ?? "—", "新增提交"],
    [event.stats?.files ?? 0, "影响文件"],
    [`+${event.stats?.insertions ?? 0}`, "新增行"],
    [`−${event.stats?.deletions ?? 0}`, "删除行"],
  ].forEach(([value, label]) => {
    const item = element("div", "detail-stat");
    item.append(element("strong", "", value), uiElement("span", "", label));
    stats.append(item);
  });
  main.append(stats);
  appendDetailList(main, "变化要点", event.summary?.highlights || []);
  appendDetailList(main, "验证建议", event.summary?.verification || []);
  const risks = (event.summary?.risks || []).map((item) => t("{area}：{reason}", {
    area: item.area || t("风险"),
    reason: item.reason || t("需要确认"),
  }));
  appendDetailList(main, "风险提示", risks);

  const side = element("aside");
  if (event.commits?.length) {
    const section = element("section", "detail-section");
    section.append(element("h3", "", t("新增提交 · {count} · 最新优先", { count: event.commits.length })));
    const list = element("div", "commit-list");
    event.commits.slice(0, 12).forEach((commit) => {
      const row = element("div", "commit-row");
      row.append(
        element("strong", "", commit.subject || t("无提交说明")),
        element("span", "", t("{author} · {time}", {
          author: commit.author || t("未知作者"),
          time: t("提交于 {time}", { time: formatTime(commit.commit_date || commit.author_date) }),
        })),
      );
      list.append(row);
    });
    section.append(list);
    side.append(section);
  }
  if (event.files?.length) {
    const section = element("section", "detail-section");
    section.append(element("h3", "", t("影响文件 · {count}", { count: event.files.length })));
    const list = element("div", "file-list");
    event.files.forEach((file) => list.append(element("code", "", file)));
    section.append(list);
    side.append(section);
  }
  if (side.childElementCount) {
    grid.append(main, side);
  } else {
    grid.classList.add("is-single");
    grid.append(main);
  }
  container.append(grid);
}

function appendDetailList(container, title, values) {
  if (!values?.length) return;
  const section = element("section", "detail-section");
  section.append(uiElement("h3", "", title));
  const list = element("ul", "detail-list");
  values.forEach((value) => list.append(element("li", "", value)));
  section.append(list);
  container.append(section);
}

function bindKindFilter() {
  const select = document.getElementById("event-kind-filter");
  const trigger = document.getElementById("event-kind-trigger");
  const menu = document.getElementById("event-kind-menu");
  if (!select || !trigger || !menu) return;

  Array.from(select.options).forEach((option) => {
    const item = element("li", `kind-filter-option kind-${option.value}`);
    item.id = `event-kind-option-${option.value}`;
    item.setAttribute("role", "option");
    item.tabIndex = -1;
    item.dataset.value = option.value;
    item.setAttribute("aria-selected", option.selected ? "true" : "false");
    const swatch = element("i", `kind-swatch kind-${option.value}`);
    swatch.setAttribute("aria-hidden", "true");
    const check = createIcon("check");
    check.classList.add("kind-check");
    item.append(swatch, element("span", "", option.textContent), check);
    item.addEventListener("click", () => setEventKindFilter(option.value, { close: true, focusTrigger: true }));
    menu.append(item);
  });
  syncKindFilter();

  trigger.addEventListener("click", () => {
    if (menu.hidden) openKindFilter();
    else closeKindFilter();
  });
  trigger.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      openKindFilter(event.key === "ArrowUp" ? "last" : "selected");
    }
  });
  menu.addEventListener("keydown", (event) => {
    const options = kindFilterOptions();
    const current = options.findIndex((item) => item.classList.contains("is-active"));
    if (event.key === "Escape") {
      event.preventDefault();
      closeKindFilter(true);
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      activateKindOption(options[0]);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      activateKindOption(options.at(-1));
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activateKindOption(options[(Math.max(current, 0) + 1) % options.length]);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      activateKindOption(options[(current <= 0 ? options.length : current) - 1]);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const active = options[current] || options.find((item) => item.classList.contains("is-selected"));
      if (active) setEventKindFilter(active.dataset.value, { close: true, focusTrigger: true });
    }
  });
  document.addEventListener("pointerdown", (event) => {
    const wrap = trigger.closest(".kind-filter");
    if (wrap && !wrap.contains(event.target)) closeKindFilter();
  });
}

function kindFilterOptions() {
  return Array.from(document.querySelectorAll("#event-kind-menu [role=option]"));
}

function activateKindOption(option) {
  if (!option) return;
  kindFilterOptions().forEach((item) => {
    item.classList.toggle("is-active", item === option);
    item.tabIndex = item === option ? 0 : -1;
  });
  document.getElementById("event-kind-menu").setAttribute("aria-activedescendant", option.id);
  option.focus();
}

function openKindFilter(focus = "selected") {
  const trigger = document.getElementById("event-kind-trigger");
  const menu = document.getElementById("event-kind-menu");
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  const options = kindFilterOptions();
  const selected = options.find((item) => item.classList.contains("is-selected")) || options[0];
  const target = focus === "last" ? options.at(-1) : selected;
  options.forEach((item) => {
    item.tabIndex = item === target ? 0 : -1;
  });
  activateKindOption(target);
}

function closeKindFilter(restoreFocus = false) {
  const trigger = document.getElementById("event-kind-trigger");
  const menu = document.getElementById("event-kind-menu");
  if (menu.hidden) return;
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  trigger.removeAttribute("aria-activedescendant");
  kindFilterOptions().forEach((item) => item.classList.remove("is-active"));
  if (restoreFocus) trigger.focus();
}

function setShortcutHelp(open, { restoreFocus = false } = {}) {
  const trigger = document.getElementById("shortcut-help-trigger");
  const popover = document.getElementById("shortcut-help-popover");
  if (!trigger || !popover) return;
  popover.hidden = !open;
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
  if (!open && restoreFocus) trigger.focus();
}

function shortcutSearchInput() {
  if (currentView() === "overview") return document.getElementById("event-search");
  if (currentView() === "projects") {
    return document.getElementById(state.watchWorkspaceTab === "add" ? "local-project-search" : "project-search");
  }
  return null;
}

function moveEventFocus(direction) {
  if (currentView() !== "overview") return false;
  const markers = Array.from(document.querySelectorAll("#activity-list .branch-event-node[data-event-id]"))
    .sort((left, right) => Number(left.dataset.eventOrder) - Number(right.dataset.eventOrder));
  if (!markers.length) return false;
  const ids = markers.map((marker) => marker.dataset.eventId);
  const activeMarker = document.activeElement?.closest?.(".branch-event-node[data-event-id]");
  const nextId = uiModel.moveEventSelection(ids, activeMarker?.dataset.eventId ?? null, direction);
  const next = markers.find((marker) => String(marker.dataset.eventId) === String(nextId));
  if (!next) return false;
  next.focus({ preventScroll: true });
  next.scrollIntoView({
    block: "nearest",
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
  });
  return true;
}

function handleGlobalShortcut(event) {
  const otherMenuOpen = Boolean(document.querySelector(
    "#event-kind-menu:not([hidden]), .branch-action-menu[open], .repository-tag-more[open]",
  ));
  const shortcutHelpOpen = !document.getElementById("shortcut-help-popover")?.hidden;
  const key = String(event.key || "").toLowerCase();
  const context = {
    view: currentView(),
    dialogOpen: Boolean(document.querySelector("dialog[open]")),
    menuOpen: otherMenuOpen || (shortcutHelpOpen && key !== "?"),
  };
  if (!uiModel.shouldHandleShortcut(event, context)) return;

  if (key === "/") {
    const input = shortcutSearchInput();
    if (!input || input.disabled || !input.getClientRects().length) return;
    event.preventDefault();
    input.focus();
    input.select?.();
    return;
  }
  if (key === "r") {
    const control = document.getElementById("check-button");
    if (!state.dashboard?.watches?.length || state.activeJob || control?.disabled) return;
    event.preventDefault();
    submitSafeCheck();
    return;
  }
  if (key === "j" || key === "k") {
    if (moveEventFocus(key === "j" ? 1 : -1)) event.preventDefault();
    return;
  }
  if (key === "?") {
    event.preventDefault();
    setShortcutHelp(!shortcutHelpOpen);
  }
}

function topbarScrollY() {
  const value = document.scrollingElement?.scrollTop ?? window.scrollY;
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

function syncStickyTopbarClearance(observedHeight = null) {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  const measuredHeight = Number.isFinite(observedHeight) && observedHeight > 0
    ? observedHeight
    : topbar.getBoundingClientRect().height;
  const height = Math.ceil(measuredHeight);
  if (!height) return;
  document.documentElement.style.setProperty("--topbar-clearance", `${height + 12}px`);
}

function syncStickyTopbar() {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  const scrollY = topbarScrollY();
  const compact = stickyTopbarCompact
    ? scrollY > TOPBAR_COMPACT_EXIT_Y
    : scrollY >= TOPBAR_COMPACT_ENTER_Y;
  if (compact === stickyTopbarCompact) return;
  stickyTopbarCompact = compact;
  topbar.classList.toggle("is-compact", compact);
}

function setEventKindFilter(value, { close = false, focusTrigger = false } = {}) {
  const select = document.getElementById("event-kind-filter");
  if (select.value !== value) {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    syncKindFilter();
    renderActivityList();
  }
  if (close) closeKindFilter(focusTrigger);
}

function syncKindFilter() {
  const select = document.getElementById("event-kind-filter");
  const trigger = document.getElementById("event-kind-trigger");
  const label = document.getElementById("event-kind-label");
  const swatch = trigger?.querySelector(".kind-swatch");
  if (!select || !trigger || !label || !swatch) return;
  const option = select.selectedOptions[0];
  label.textContent = option?.textContent || t("全部类型");
  swatch.className = `kind-swatch kind-${select.value || "all"}`;
  kindFilterOptions().forEach((item) => {
    const selected = item.dataset.value === select.value;
    const optionCopy = Array.from(select.options).find((candidate) => candidate.value === item.dataset.value)?.textContent;
    if (optionCopy) item.querySelector("span").textContent = optionCopy;
    item.classList.toggle("is-selected", selected);
    item.setAttribute("aria-selected", selected ? "true" : "false");
  });
}

function renderLocalizedInterface() {
  syncThemeControls();
  if (state.dashboard) renderDashboard(state.dashboard);
  if (state.integrations) renderIntegrations(state.integrations);
  if (state.monitoring) renderMonitoringSettings(state.monitoring, { preserveDraft: true });
  if (state.monitoringMessage) setMonitoringMessage(
    state.monitoringMessage.source,
    { ...state.monitoringMessage, remember: false },
  );
  if (state.changelog) renderChangelog(state.changelog);
  if (Array.isArray(state.candidates)) renderLocalProjects();
  if (state.activeJob) updateTaskProgress(state.activeJob);
  else if (state.cloneTerminalJob) renderCloneTask(state.cloneTerminalJob);
  if (document.getElementById("scan-roots-dialog")?.open) {
    syncScanRootsFormBusy();
    updateScanRootDraftCount();
    const roots = currentScanRootDraftValues();
    document.querySelectorAll("#scan-roots-list .scan-root-remove").forEach((control, index) => {
      control.setAttribute("aria-label", t("移除扫描目录：{root}", { root: roots[index] || "" }));
    });
    const emptyRoots = document.querySelector("#scan-roots-list .scan-roots-empty");
    if (emptyRoots) emptyRoots.textContent = t("还没有选择目录。使用系统窗口选择文件夹，或展开下方的手动输入。");
    const pickerStatus = document.getElementById("scan-root-picker-status");
    if (pickerStatus?.dataset.titleSource) {
      let variables = {};
      try {
        variables = JSON.parse(pickerStatus.dataset.copyVariables || "{}");
      } catch (_error) {
        variables = {};
      }
      document.getElementById("scan-root-picker-status-title").textContent = t(pickerStatus.dataset.titleSource, variables);
      document.getElementById("scan-root-picker-status-copy").textContent = t(pickerStatus.dataset.copySource || "", variables);
    }
  }
  if (document.getElementById("event-dialog")?.open && state.currentEvent) {
    renderEventDetail(state.currentEvent);
  }
  if (state.canvasSelectedAuthor?.details) {
    renderCanvasAuthorInspector(
      state.canvasSelectedAuthor.author,
      state.canvasSelectedAuthor.details,
      state.canvasSelectedAuthor.eventIds,
      state.canvasSelectedAuthor.totalEvents,
    );
  }
  localizeOpenBranchPicker();
  localizeCloneConfirmation();
  if (document.getElementById("unwatch-dialog")?.open && state.pendingUnwatch) {
    renderUnwatchLabels();
    setUnwatchBusy(Boolean(state.pendingUnwatch.submitting));
  }
  setView(currentView());
  syncKindFilter();
  i18n.translateDOM(document);
  syncRailCollapseControl();
  syncCanvasFocusControl();
  scheduleRender("language-topbar-clearance", syncStickyTopbarClearance);
}

function bindInteractions() {
  window.addEventListener("branch-radar:languagechange", renderLocalizedInterface);
  document.getElementById("rail-collapse-button").addEventListener("click", () => {
    setRailCollapsed(!document.body.classList.contains("rail-collapsed"));
  });
  document.getElementById("branch-canvas-zoom-out").addEventListener("click", () => zoomCanvas(1 / CANVAS_ZOOM_STEP));
  document.getElementById("branch-canvas-zoom-in").addEventListener("click", () => zoomCanvas(CANVAS_ZOOM_STEP));
  document.getElementById("branch-canvas-fit").addEventListener("click", fitBranchCanvas);
  document.getElementById("branch-canvas-latest").addEventListener("click", showLatestCanvas);
  document.getElementById("branch-canvas-focus").addEventListener("click", () => setCanvasFocus(!state.canvasFocused));
  bindBranchCanvasInteractions();
  syncRailCollapseControl();
  syncCanvasFocusControl();
  let projectViewport = window.innerWidth;
  window.addEventListener("resize", () => {
    const nextViewport = window.innerWidth;
    scheduleRender("topbar-resize", syncStickyTopbar);
    if ((projectViewport <= 560) === (nextViewport <= 560) && (projectViewport <= 1180) === (nextViewport <= 1180)) return;
    projectViewport = nextViewport;
    if (currentView() === "projects" && state.watchWorkspaceTab === "watched" && state.dashboard) {
      scheduleRender("project-viewport", renderProjectList);
    }
  });
  window.addEventListener("scroll", () => scheduleRender("topbar-scroll", syncStickyTopbar), { passive: true });
  if (typeof ResizeObserver === "function") {
    stickyTopbarObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      const borderBoxSize = entry?.borderBoxSize;
      const borderBox = Array.isArray(borderBoxSize) ? borderBoxSize[0] : borderBoxSize;
      stickyTopbarObservedHeight = Number.isFinite(borderBox?.blockSize) ? borderBox.blockSize : null;
      scheduleRender("topbar-clearance", () => syncStickyTopbarClearance(stickyTopbarObservedHeight));
    });
    stickyTopbarObserver.observe(document.querySelector(".topbar"));
  }
  window.addEventListener("hashchange", () => {
    // Opening a new secure link in an already-open Branch Radar tab is a
    // same-document navigation. Capture the fresh token, then reload so every
    // request and in-memory task uses the new authenticated session.
    if (captureLaunchSession()) {
      location.reload();
      return;
    }
    setView(currentView());
    document.getElementById("main-content").focus({ preventScroll: true });
  });
  bindWatchWorkspaceTabs();
  document.getElementById("scan-roots-button").addEventListener("click", openScanRootsDialog);
  document.getElementById("scan-root-picker").addEventListener("click", startScanRootPicker);
  document.getElementById("scan-root-manual-add").addEventListener("click", addManualScanRoot);
  document.getElementById("scan-root-manual-input").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addManualScanRoot();
  });
  document.querySelector("#scan-root-manual > summary").addEventListener("click", (event) => {
    if (!state.scanRootPickerPending && !state.scanRootsSaving && !state.scanRootPickerNeedsReconcile) return;
    event.preventDefault();
  });
  document.getElementById("scan-roots-form").addEventListener("submit", saveScanRoots);
  document.getElementById("projects-refresh-button").addEventListener("click", startDiscovery);
  document.getElementById("local-project-search").addEventListener("input", () => {
    state.localProjectPage = 1;
    state.matchedRemoteCanonical = null;
    scheduleRender("local-project-search", renderLocalProjects);
  });
  document.getElementById("local-project-prev").addEventListener("click", () => {
    state.localProjectPage = Math.max(1, state.localProjectPage - 1);
    renderLocalProjects();
    document.getElementById("local-project-search").focus();
  });
  document.getElementById("local-project-next").addEventListener("click", () => {
    state.localProjectPage += 1;
    renderLocalProjects();
    document.getElementById("local-project-search").focus();
  });
  document.getElementById("remote-associate-form").addEventListener("submit", associateRemote);
  bindCloneParentPicker();
  document.getElementById("clone-form").addEventListener("submit", submitClone);
  document.getElementById("task-cancel-button").addEventListener("click", cancelClone);
  document.getElementById("task-toggle").addEventListener("click", () => {
    setCloneTaskExpanded(!state.cloneTaskExpanded, { focus: true });
  });
  document.getElementById("task-retry-button").addEventListener("click", retryCloneTask);
  document.getElementById("task-edit-button").addEventListener("click", editCloneTask);
  document.getElementById("task-open-button").addEventListener("click", openCloneTaskResult);
  document.getElementById("task-dismiss-button").addEventListener("click", () => dismissCloneTask());
  document.getElementById("repository-profile-form").addEventListener("submit", saveRepositoryProfile);
  document.getElementById("unwatch-form").addEventListener("submit", submitUnwatch);
  document.getElementById("repository-profile-tags").addEventListener("input", syncRepositoryTagSuggestions);
  document.getElementById("branch-selection-form").addEventListener("submit", submitWatchSelection);
  document.getElementById("branch-dismiss-button").addEventListener("click", () => {
    const dialog = document.getElementById("branch-dialog");
    if (dialog.classList.contains("is-watch-error")) {
      setWatchButtonState("idle");
      document.getElementById("branch-search").focus();
      return;
    }
    closeDialog("branch-dialog");
  });
  document.getElementById("branch-search").addEventListener("input", () => {
    scheduleRender("branch-search", filterBranchRows);
  });
  document.getElementById("select-all-branches").addEventListener("click", toggleSelectAllBranches);
  document.getElementById("check-button").addEventListener("click", submitSafeCheck);
  document.getElementById("shortcut-help-trigger").addEventListener("click", () => {
    const popover = document.getElementById("shortcut-help-popover");
    setShortcutHelp(Boolean(popover?.hidden));
  });
  document.getElementById("check-run-toggle").addEventListener("click", toggleCheckRunDetails);
  document.getElementById("check-run-acknowledge").addEventListener("click", acknowledgeCheckRun);
  document.getElementById("select-visible-watches").addEventListener("click", toggleVisibleWatches);
  document.getElementById("toggle-all-repositories").addEventListener("click", toggleAllRepositories);
  document.getElementById("clear-selected-watches").addEventListener("click", clearSelectedWatches);
  document.getElementById("check-selected-button").addEventListener("click", submitSelectedCheck);
  document.getElementById("check-options-button").addEventListener("click", () => {
    resetCheckOptions();
    openDialog("check-dialog");
  });
  document.getElementById("check-form").addEventListener("submit", submitCheck);
  document.getElementById("publish-form").addEventListener("submit", publishPending);
  document.getElementById("project-search").addEventListener("input", () => {
    scheduleRender("project-search", renderProjectList);
  });
  document.getElementById("event-search").addEventListener("input", () => {
    scheduleRender("event-search", renderActivityList);
  });
  bindKindFilter();
  document.getElementById("event-kind-filter").addEventListener("change", () => {
    syncKindFilter();
    renderActivityList();
  });
  document.querySelectorAll("[data-signal-range]").forEach((control) => {
    control.addEventListener("click", () => setActivityRangeHours(control.dataset.signalRange));
  });
  document.getElementById("activity-scope-clear").addEventListener("click", clearActivityScope);
  document.getElementById("activity-scope-check").addEventListener("click", submitScopedCheck);
  document.getElementById("activity-load-more").addEventListener("click", () => loadActivityFeed({ reset: false }));
  document.getElementById("connections-refresh-button").addEventListener("click", refreshSettingsStatus);
  document.getElementById("monitoring-form").addEventListener("submit", saveMonitoringSettings);
  document.getElementById("monitoring-reload-button").addEventListener("click", () => loadMonitoringSettings({ force: true }));
  document.querySelectorAll("#monitoring-form input").forEach((control) => {
    control.addEventListener("change", markMonitoringDirty);
  });
  document.querySelectorAll("#monitoring-quiet-start, #monitoring-quiet-end").forEach((control) => {
    control.addEventListener("blur", () => validateMonitoringQuietHours());
  });
  document.getElementById("publish-button").addEventListener("click", openPublishConfirmation);
  document.getElementById("lark-connect-form").addEventListener("submit", connectLark);
  document.getElementById("create-lark-button").addEventListener("click", () => {
    if (state.larkCreatePending) {
      showToast(t("已有同标题创建请求待确认，请先检查飞书云空间。"), true);
      return;
    }
    state.larkCreateRequestId = newRequestId();
    openDialog("create-lark-dialog");
  });
  document.getElementById("create-lark-form").addEventListener("submit", createLark);
  document.querySelectorAll("[data-close-dialog]").forEach((control) => {
    control.addEventListener("click", () => closeDialog(control.dataset.closeDialog));
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("cancel", (event) => {
      if (dialog.id === "unwatch-dialog" && state.pendingUnwatch?.submitting) {
        event.preventDefault();
      }
      if (dialog.id === "scan-roots-dialog" && (state.scanRootsSaving || state.scanRootPickerPending)) {
        event.preventDefault();
      }
      if (dialog.id === "clone-dialog" && cloneRequestInFlight()) {
        event.preventDefault();
      }
    });
    dialog.addEventListener("close", () => {
      if (dialog.id === "check-dialog") resetCheckOptions();
      if (dialog.id === "branch-dialog") {
        state.selectedCandidate = null;
        if (state.activeJob) {
          document.getElementById("task-bar").hidden = false;
          document.body.classList.add("has-task-bar");
        }
        if (!state.activeJob && !state.watchSubmission) setWatchButtonState("idle");
      }
      if (dialog.id === "event-dialog") {
        state.eventRequest += 1;
        state.currentEvent = null;
      }
      if (dialog.id === "repository-profile-dialog") state.repositoryProfileId = null;
      if (dialog.id === "scan-roots-dialog") {
        document.getElementById("scan-roots-error").textContent = "";
        state.scanRootsDraft = [];
        resetScanRootPickerStatus();
        if (!hasConfiguredScanRoots()) state.pendingRemoteUrl = null;
      }
      if (dialog.id === "clone-dialog") closeCloneParentPicker();
      if (dialog.id === "clone-dialog" && !cloneRequestInFlight()) {
        state.clonePreview = null;
        state.cloneSubmission = null;
        resetCloneDialog();
      }
      if (dialog.id === "unwatch-dialog") {
        const pending = state.pendingUnwatch;
        state.pendingUnwatch = null;
        document.getElementById("unwatch-error").textContent = "";
        if (!pending?.submitting && pending?.returnFocus?.isConnected) {
          window.requestAnimationFrame(() => pending.returnFocus.focus());
        }
      }
    });
    dialog.addEventListener("click", (event) => {
      if (
        event.target === dialog
        && !(dialog.id === "unwatch-dialog" && state.pendingUnwatch?.submitting)
        && !(dialog.id === "scan-roots-dialog" && (state.scanRootsSaving || state.scanRootPickerPending))
        && !(dialog.id === "clone-dialog" && cloneRequestInFlight())
      ) dialog.close();
    });
  });
  document.addEventListener("pointerdown", (event) => {
    document.querySelectorAll(".branch-action-menu[open], .repository-tag-more[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
    const shortcut = document.querySelector(".shortcut-help");
    if (shortcut && !shortcut.contains(event.target)) setShortcutHelp(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      handleGlobalShortcut(event);
      return;
    }
    if (state.canvasFocused) {
      event.preventDefault();
      setCanvasFocus(false);
      return;
    }
    if (state.canvasSelectedAuthor) {
      event.preventDefault();
      closeCanvasAuthorInspector({ restoreFocus: true });
      return;
    }
    const shortcutPopover = document.getElementById("shortcut-help-popover");
    if (shortcutPopover && !shortcutPopover.hidden) {
      event.preventDefault();
      setShortcutHelp(false, { restoreFocus: true });
      return;
    }
    const openMenu = document.querySelector(".branch-action-menu[open], .repository-tag-more[open]");
    if (!openMenu) return;
    event.preventDefault();
    openMenu.open = false;
    openMenu.querySelector("summary")?.focus();
  });
  document.getElementById("retry-dashboard").addEventListener("click", async () => {
    state.pollFailures = 0;
    try {
      const dashboard = await loadDashboard();
      await reconcileStoredScanRootPickerJob({ force: !dashboard.active_job });
      await reconcileStoredCloneJob({ force: true });
      if (!state.activeJob) await loadCachedIntegrations();
    } catch (_error) {
      // Persistent recovery banner remains visible.
    }
  });
  syncStickyTopbar();
  syncStickyTopbarClearance();
}

async function initialize() {
  initializeThemeControls();
  bindInteractions();
  setView(currentView());
  if (state.larkCreatePending) {
    document.getElementById("lark-title").value = state.larkCreatePending.title || "";
    document.getElementById("create-lark-button").disabled = true;
  }
  try {
    const dashboard = await loadDashboard();
    await reconcileStoredScanRootPickerJob();
    await reconcileStoredCloneJob();
    await loadCachedIntegrations();
    if (!dashboard.active_job && !state.activeJob && !(dashboard.watches || []).length && !state.autoDiscoveryStarted) {
      state.autoDiscoveryStarted = true;
      if (currentView() !== "projects" || state.watchWorkspaceTab !== "add") location.hash = "projects/add";
      else ensureLocalProjects();
    }
  } catch (_error) {
    // The error toast contains the recovery cue.
  }
}

initialize();
