"use strict";

const CONVERSATION_STORAGE_KEY = "deviceops-conversation-id";

function createConversationId() {
  return globalThis.crypto?.randomUUID?.()
    || `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function conversationId() {
  try {
    const existing = globalThis.localStorage?.getItem(
      CONVERSATION_STORAGE_KEY,
    );
    if (existing) {
      return existing;
    }
    const created = createConversationId();
    globalThis.localStorage?.setItem(CONVERSATION_STORAGE_KEY, created);
    return created;
  } catch {
    return createConversationId();
  }
}

const state = {
  busy: false,
  pendingThreadId: null,
  ports: new Map(),
  allowedPorts: [1, 2, 3, 4, 5],
  streamedSteps: 0,
  conversationId: conversationId(),
  activeController: null,
  progressArticle: null,
  progressLabel: null,
  progressElapsed: null,
  progressStartedAt: 0,
  progressTimer: null,
  recentPorts: new Set(),
  activeProfileId: "",
  switchingEnabled: false,
  devices: new Map(),
  deviceSnapshot: null,
  statusLoading: true,
  plannerMode: "",
  progressAdaptive: false,
};

const TRACE_LABELS = {
  validate: "校验指令契约",
  query: "读取设备状态",
  collect_device_status: "采集整机状态",
  collect_port_status: "采集端口状态",
  collect_charging_status: "采集充电状态",
  collect_pd_status: "采集 PD 协商详情",
  collect_temperature_mode: "采集温控模式",
  analyze_diagnosis: "生成诊断结论",
  analyze_device_diagnosis: "分析整机异常",
  retrieve_knowledge: "检索固件知识库",
  prepare_control: "读取操作前状态",
  control: "执行控制指令",
  verify_control: "二次读取验证",
  respond_chat: "生成业务回复",
  answer_knowledge: "查询固件知识",
  general_knowledge_fallback: "使用通用领域知识",
  explain_previous: "解释上一条结果",
  unsupported: "检查能力边界",
  clarify: "请求澄清",
  summarize: "汇总回答",
};

const ACTION_LABELS = {
  respond_chat: "业务对话",
  answer_knowledge: "知识问答",
  explain_previous: "解释结果",
  unsupported_request: "范围外请求",
  get_status: "查询整机",
  get_port_status: "查询端口",
  diagnose_device: "诊断整机",
  diagnose_port: "诊断端口",
  set_port_power: "控制端口",
  ask_clarification: "补充信息",
};

const PORT_MODE_LABELS = {
  charging: "充电中",
  standby: "待机",
  off: "关闭",
};

function traceLabel(step) {
  return TRACE_LABELS[step] || String(step);
}

const elements = {
  form: document.querySelector("#request-form"),
  input: document.querySelector("#request-input"),
  send: document.querySelector("#send-button"),
  messages: document.querySelector("#messages"),
  confirmation: document.querySelector("#confirmation"),
  confirmationQuestion: document.querySelector("#confirmation-question"),
  confirmationBefore: document.querySelector("#confirmation-before"),
  approve: document.querySelector("#approve-control"),
  reject: document.querySelector("#reject-control"),
  session: document.querySelector("#session-indicator"),
  healthDot: document.querySelector("#health-dot"),
  healthLabel: document.querySelector("#health-label"),
  backendLabel: document.querySelector("#backend-label"),
  plannerLabel: document.querySelector("#planner-label"),
  knowledgeLabel: document.querySelector("#knowledge-label"),
  controlLabel: document.querySelector("#control-label"),
  devicePanelLabel: document.querySelector("#device-panel-label"),
  devicePowerBudget: document.querySelector("#device-power-budget"),
  deviceModelLabel: document.querySelector("#device-model-label"),
  deviceLed: document.querySelector("#device-led"),
  deviceVisualPorts: document.querySelector("#device-ports"),
  deviceLastUpdated: document.querySelector("#device-last-updated"),
  deviceContext: document.querySelector("#device-context"),
  deviceProfileSelect: document.querySelector("#device-profile-select"),
  refresh: document.querySelector("#refresh-status"),
  portList: document.querySelector("#port-list"),
  runStatus: document.querySelector("#run-status"),
  commandAction: document.querySelector("#command-action"),
  commandPort: document.querySelector("#command-port"),
  commandTarget: document.querySelector("#command-target"),
  verification: document.querySelector("#verification-status"),
  duration: document.querySelector("#run-duration"),
  usage: document.querySelector("#run-usage"),
  trace: document.querySelector("#trace-list"),
  evidence: document.querySelector("#evidence-list"),
  knowledgeStatus: document.querySelector("#knowledge-status"),
  knowledgeList: document.querySelector("#knowledge-list"),
  warningList: document.querySelector("#warning-list"),
  newSession: document.querySelector("#new-session"),
  inspector: document.querySelector("#execution-panel"),
  inspectorToggle: document.querySelector("#toggle-inspector"),
  inspectorToggleLabel: document.querySelector("#inspector-toggle-label"),
  inspectorClose: document.querySelector("#close-inspector"),
  inspectorBackdrop: document.querySelector("#inspector-backdrop"),
  starterActions: document.querySelector("#starter-actions"),
  welcomeMessage: document.querySelector("#welcome-message"),
};

function currentTime() {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date());
}

function createSvg(paths) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const definition of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", definition);
    svg.append(path);
  }
  return svg;
}

function appendMessage(role, text, isError = false, run = null) {
  const article = document.createElement("article");
  article.className = `message ${role}-message`;
  if (isError) {
    article.classList.add("error-message");
  }

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.append(
    role === "assistant"
      ? createSvg(["M22 12h-4l-3 9L9 3l-3 9H2"])
      : createSvg([
          "M20 21a8 8 0 0 0-16 0",
          "M12 13a4 4 0 1 0 0-8 4 4 0 0 0 0 8",
        ]),
  );

  const content = document.createElement("div");
  content.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const author = document.createElement("strong");
  author.textContent = role === "assistant" ? "DeviceOps" : "Operator";
  const time = document.createElement("span");
  time.textContent = currentTime();
  meta.append(author, time);

  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  content.append(meta, paragraph);
  if (role === "assistant" && run) {
    appendMessageRunFacts(content, run);
    appendMessageKnowledge(content, run);
    appendMessageSuggestions(content, run);
  }
  article.append(avatar, content);
  elements.messages.append(article);
  elements.messages.scrollTop = elements.messages.scrollHeight;
  return article;
}

function appendMessageSuggestions(content, run) {
  const suggestions = Array.isArray(run.suggestions) ? run.suggestions : [];
  if (suggestions.length === 0) {
    return;
  }
  const navigation = document.createElement("nav");
  navigation.className = "message-suggestions";
  navigation.setAttribute("aria-label", "相关追问");
  for (const suggestion of suggestions.slice(0, 3)) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.suggestedRequest = suggestion.request || "";
    button.dataset.kind = suggestion.kind || "query";
    button.textContent = suggestion.label || suggestion.request || "继续追问";
    button.title = suggestion.reason || "";
    button.addEventListener("click", () => {
      if (!state.busy && button.dataset.suggestedRequest) {
        void sendRequest(button.dataset.suggestedRequest);
      }
    });
    navigation.append(button);
  }
  content.append(navigation);
}

function clearFollowUpSuggestions() {
  document.querySelectorAll(".message-suggestions").forEach((item) => {
    item.remove();
  });
}

function appendMessageRunFacts(content, run) {
  const result = run.result || {};
  const facts = document.createElement("div");
  facts.className = "message-run-facts";

  const source = document.createElement("span");
  source.className = "message-run-fact";
  if (result.response_kind === "general_knowledge") {
    source.textContent = "来源 · 模型常识";
    source.dataset.tone = "warning";
  } else if (result.response_kind === "knowledge") {
    source.textContent = "来源 · 知识库";
    source.dataset.tone = run.knowledge?.length ? "verified" : "warning";
  } else if (
    result.device || result.port || result.diagnosis || result.verification
  ) {
    source.textContent = "来源 · 设备实测";
    source.dataset.tone = result.ok === false ? "warning" : "verified";
  } else {
    source.textContent = "来源 · 业务路由";
  }
  facts.append(source);

  const verification = result.verification?.status;
  if (verification) {
    const item = document.createElement("span");
    item.className = "message-run-fact";
    item.textContent = `验证 · ${verification}`;
    item.dataset.tone = ["verified", "already_satisfied"].includes(
      verification,
    ) ? "verified" : "warning";
    facts.append(item);
  }

  const duration = formatDuration(run.metrics?.duration_ms);
  if (duration) {
    const item = document.createElement("span");
    item.className = "message-run-fact";
    item.textContent = `耗时 · ${duration}`;
    facts.append(item);
  }
  content.append(facts);
}

function appendMessageKnowledge(content, run) {
  const citations = Array.isArray(run.knowledge) ? run.knowledge : [];
  const warnings = Array.isArray(run.warnings) ? run.warnings : [];
  if (citations.length === 0 && warnings.length === 0) {
    return;
  }
  const details = document.createElement("div");
  details.className = "message-details";
  for (const warning of warnings) {
    const item = document.createElement("span");
    item.className = "message-warning";
    item.textContent = warning;
    details.append(item);
  }
  for (const [index, citation] of citations.entries()) {
    const item = document.createElement("span");
    item.className = "message-citation";
    item.textContent = `【${index + 1}】${citation.title}`;
    item.title = citation.section || citation.id || "";
    details.append(item);
  }
  content.append(details);
}

function setBusy(busy) {
  state.busy = busy;
  elements.send.disabled = busy;
  elements.approve.disabled = busy;
  elements.reject.disabled = busy;
  elements.refresh.disabled = busy;
  elements.newSession.disabled = busy;
  elements.deviceProfileSelect.disabled = busy || !state.switchingEnabled;
  document.querySelectorAll(
    ".quick-actions button, .message-suggestions button",
  ).forEach((button) => {
    button.disabled = busy;
  });
  elements.session.textContent = busy ? "执行中" : "就绪";
  elements.session.classList.toggle("busy", busy);
}

function startProgress(label = "正在分析请求", {adaptive = true} = {}) {
  stopProgress();
  const article = document.createElement("article");
  article.className = "message assistant-message progress-message";

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.append(createSvg(["M22 12h-4l-3 9L9 3l-3 9H2"]));

  const content = document.createElement("div");
  content.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const author = document.createElement("strong");
  author.textContent = "DeviceOps";
  const time = document.createElement("span");
  time.textContent = "处理中";
  meta.append(author, time);

  const card = document.createElement("div");
  card.className = "progress-card";
  card.setAttribute("role", "status");
  card.setAttribute("aria-live", "polite");
  const spinner = document.createElement("span");
  spinner.className = "progress-spinner";
  spinner.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div");
  copy.className = "progress-copy";
  const status = document.createElement("strong");
  status.textContent = label;
  const elapsed = document.createElement("span");
  elapsed.textContent = "已用时 0 秒";
  copy.append(status, elapsed);
  const stop = document.createElement("button");
  stop.className = "stop-run-button";
  stop.type = "button";
  stop.textContent = "停止";
  stop.addEventListener("click", cancelActiveRun);
  card.append(spinner, copy, stop);
  content.append(meta, card);
  article.append(avatar, content);
  elements.messages.append(article);

  state.progressArticle = article;
  state.progressLabel = status;
  state.progressElapsed = elapsed;
  state.progressStartedAt = performance.now();
  state.progressAdaptive = adaptive;
  state.progressTimer = globalThis.setInterval(updateProgressElapsed, 1000);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function updateProgress(step) {
  if (state.progressLabel) {
    state.progressLabel.textContent = traceLabel(step);
  }
  state.progressAdaptive = false;
  updateProgressElapsed();
}

function updateProgressElapsed() {
  if (!state.progressElapsed || !state.progressStartedAt) {
    return;
  }
  const seconds = Math.max(
    0,
    Math.floor((performance.now() - state.progressStartedAt) / 1000),
  );
  if (state.progressAdaptive && state.progressLabel) {
    if (seconds >= 10) {
      state.progressLabel.textContent = state.plannerMode === "ollama"
        ? "本地模型仍在规划，尚未调用设备"
        : "模型仍在规划，尚未调用设备";
    } else if (seconds >= 4) {
      state.progressLabel.textContent = state.plannerMode === "ollama"
        ? "本地模型正在规划工具"
        : "正在规划工具调用";
    }
  }
  state.progressElapsed.textContent = `已用时 ${seconds} 秒`;
}

function stopProgress() {
  if (state.progressTimer !== null) {
    globalThis.clearInterval(state.progressTimer);
  }
  state.progressArticle?.remove();
  state.progressArticle = null;
  state.progressLabel = null;
  state.progressElapsed = null;
  state.progressStartedAt = 0;
  state.progressTimer = null;
  state.progressAdaptive = false;
}

function cancelActiveRun() {
  state.activeController?.abort();
}

function isAbortError(error) {
  return error instanceof DOMException && error.name === "AbortError";
}

function parseSseFrame(frame) {
  let name = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  try {
    return {name, data: JSON.parse(dataLines.join("\n"))};
  } catch {
    return null;
  }
}

/** POST to an SSE endpoint and invoke onEvent for each frame as it arrives. */
async function streamEvents(url, body, onEvent, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      }
    } catch {
      // Non-JSON error body; keep the status-based message.
    }
    throw new Error(detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const {done, value} = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, {stream: true});
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const event = parseSseFrame(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event) {
        onEvent(event);
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}

/** Drive one workflow run, rendering trace steps as the graph emits them. */
async function runWorkflow(url, body, signal) {
  beginTrace();
  let finalRun = null;
  let streamError = null;
  await streamEvents(url, body, (event) => {
    if (event.name === "trace") {
      appendTraceStep(event.data.step, event.data.duration_ms);
    } else if (event.name === "run") {
      finalRun = event.data.run;
    } else if (event.name === "error") {
      streamError = new Error(event.data.detail || "工作流执行失败。");
    }
  }, signal);
  if (streamError) {
    throw streamError;
  }
  if (!finalRun) {
    throw new Error("服务未返回执行结果。");
  }
  return finalRun;
}

async function sendRequest(requestText, options = {}) {
  const text = requestText.trim();
  if (!text || state.busy) {
    return;
  }
  clearFollowUpSuggestions();
  elements.starterActions.hidden = true;
  if (!options.silentUser) {
    appendMessage("user", text);
  }
  const controller = new AbortController();
  state.activeController = controller;
  setBusy(true);
  startProgress();
  elements.runStatus.textContent = "RUNNING";
  try {
    const run = await runWorkflow("/v1/requests/stream", {
      request: text,
      conversation_id: state.conversationId,
    }, controller.signal);
    stopProgress();
    handleRun(run);
  } catch (error) {
    stopProgress();
    if (isAbortError(error)) {
      renderCancelledRun();
    } else {
      renderFailure(error);
    }
  } finally {
    if (state.activeController === controller) {
      state.activeController = null;
    }
    setBusy(false);
  }
}

async function resumeControl(approved) {
  if (!state.pendingThreadId || state.busy) {
    return;
  }
  const threadId = state.pendingThreadId;
  appendMessage("user", approved ? "确认执行" : "取消操作");
  const controller = new AbortController();
  state.activeController = controller;
  setBusy(true);
  startProgress(
    approved ? "正在执行控制并验证" : "正在取消操作",
    {adaptive: false},
  );
  elements.runStatus.textContent = approved ? "EXECUTING" : "CANCELLING";
  try {
    const run = await runWorkflow(
      `/v1/requests/${encodeURIComponent(threadId)}/resume/stream`,
      {approved},
      controller.signal,
    );
    stopProgress();
    hideConfirmation();
    handleRun(run);
  } catch (error) {
    stopProgress();
    if (isAbortError(error)) {
      renderCancelledRun();
    } else {
      renderFailure(error);
    }
  } finally {
    if (state.activeController === controller) {
      state.activeController = null;
    }
    setBusy(false);
  }
}

function beginTrace() {
  state.streamedSteps = 0;
  elements.trace.replaceChildren();
  const item = document.createElement("li");
  item.className = "trace-empty";
  item.textContent = "启动工作流…";
  elements.trace.append(item);
}

function appendTraceStep(step, durationMs) {
  if (state.streamedSteps === 0) {
    elements.trace.replaceChildren();
  }
  state.streamedSteps += 1;
  updateProgress(step);
  elements.trace.append(traceItem(step, durationMs, "trace-live"));
  elements.trace.scrollTop = elements.trace.scrollHeight;
}

function traceItem(step, durationMs, className) {
  const item = document.createElement("li");
  if (className) {
    item.className = className;
  }
  item.append(traceLabel(step));
  const elapsed = formatDuration(durationMs);
  if (elapsed) {
    const timing = document.createElement("span");
    timing.className = "trace-timing";
    timing.textContent = elapsed;
    item.append(timing);
  }
  return item;
}

function formatDuration(milliseconds) {
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) {
    return "";
  }
  return value >= 1000
    ? `${(value / 1000).toFixed(2)} s`
    : `${Math.round(value)} ms`;
}

function renderMetrics(metrics) {
  if (!metrics) {
    elements.duration.textContent = "-";
    elements.usage.textContent = "-";
    return;
  }
  const total = formatDuration(metrics.duration_ms) || "-";
  const planner = formatDuration(metrics.planner_ms);
  elements.duration.textContent = planner && metrics.planner_ms > 0
    ? `${total}（规划 ${planner}）`
    : total;

  const usage = metrics.usage;
  if (!usage || !usage.total_tokens) {
    // Rule-based planning calls no model, so there is nothing to bill.
    elements.usage.textContent = metrics.model ? "0 tok" : "无模型调用";
    return;
  }
  const parts = [`${usage.total_tokens} tok`];
  if (typeof metrics.cost === "number") {
    parts.push(`${metrics.currency || ""} ${metrics.cost.toFixed(4)}`.trim());
  }
  elements.usage.textContent = parts.join(" · ");
  elements.usage.title = [
    `模型 ${metrics.model || "-"}`,
    `输入 ${usage.input_tokens} tok`,
    `输出 ${usage.output_tokens} tok`,
    typeof metrics.cost === "number" ? "" : "未配置单价，不计费用",
  ].filter(Boolean).join(" / ");
}

function handleRun(run) {
  updateInspector(run);
  updatePortsFromResult(run.result);

  if (run.status === "confirmation_required") {
    showConfirmation(run);
    appendMessage(
      "assistant",
      run.confirmation?.question || "该控制操作需要确认。",
    );
    return;
  }

  hideConfirmation();
  const summary = run.summary || (
    run.result?.ok ? "设备操作已完成。" : "设备操作未完成。"
  );
  appendMessage(
    "assistant",
    summary,
    run.result?.ok === false
      && !run.result?.cancelled
      && !run.result?.needs_clarification,
    run,
  );
}

function showConfirmation(run) {
  state.pendingThreadId = run.thread_id;
  elements.confirmation.hidden = false;
  elements.confirmationQuestion.textContent =
    run.confirmation?.question || "确认执行该设备控制命令？";
  const before = run.confirmation?.before;
  elements.confirmationBefore.textContent = before
    ? formatPortSummary(before)
    : "操作前状态不可用";
}

function hideConfirmation() {
  state.pendingThreadId = null;
  elements.confirmation.hidden = true;
}

function renderFailure(error) {
  elements.runStatus.textContent = "ERROR";
  appendMessage(
    "assistant",
    error instanceof Error ? error.message : "请求执行失败。",
    true,
  );
}

function renderCancelledRun() {
  elements.runStatus.textContent = "STOPPED";
  appendMessage("assistant", "已停止本次请求，未继续等待后续结果。");
}

function updateInspector(run) {
  const command = run.command || {};
  const result = run.result || {};
  const verification = result.verification || {};
  elements.runStatus.textContent = runStatusLabel(run, result);
  elements.commandAction.textContent =
    ACTION_LABELS[command.action] || command.action || "-";
  elements.commandPort.textContent = command.port ?? "-";
  elements.commandTarget.textContent = command.enabled === true
    ? "开启"
    : command.enabled === false
      ? "关闭"
      : command.action === "answer_knowledge"
        ? result.response_kind === "general_knowledge"
          ? "通用知识"
          : "知识库"
        : command.action === "respond_chat"
          ? "对话"
          : command.action === "explain_previous"
            ? "上下文"
            : command.action === "unsupported_request"
              ? "能力边界"
              : command.action === "ask_clarification"
                ? "待补充"
                : "查询";
  elements.verification.textContent = verification.status || "-";
  elements.verification.className = verification.status
    ? `verification-${verification.status}`
    : "";

  elements.trace.replaceChildren();
  const trace = Array.isArray(run.trace) ? run.trace : [];
  const steps = run.metrics?.steps || [];
  elements.inspectorToggleLabel.textContent = trace.length
    ? `执行证据 · ${trace.length} 步`
    : "执行证据";
  elements.inspectorToggle.classList.toggle("has-evidence", trace.length > 0);
  if (trace.length === 0) {
    const item = document.createElement("li");
    item.className = "trace-empty";
    item.textContent = "暂无 Trace";
    elements.trace.append(item);
  } else {
    // On resume, `trace` carries the whole thread while `steps` only times
    // the segment just executed, so align the timings to the tail.
    const offset = Math.max(0, trace.length - steps.length);
    trace.forEach((step, index) => {
      elements.trace.append(
        traceItem(step, steps[index - offset]?.duration_ms),
      );
    });
  }

  renderMetrics(run.metrics);
  renderEvidence(run);
  renderKnowledge(run);
}

function runStatusLabel(run, result) {
  if (run.status === "confirmation_required") {
    return "WAITING";
  }
  if (result.cancelled) {
    return "CANCELLED";
  }
  if (result.needs_clarification) {
    return "CLARIFY";
  }
  if (result.response_kind === "unsupported") {
    return "OUT OF SCOPE";
  }
  if (
    ["conversation", "knowledge", "general_knowledge", "explanation"].includes(
      result.response_kind,
    )
  ) {
    return "ANSWERED";
  }
  return result.ok === false ? "FAILED" : "COMPLETED";
}

function renderEvidence(run) {
  const result = run?.result || {};
  elements.evidence.replaceChildren();
  const port = result?.port || result?.after?.data;
  const diagnosis = result?.diagnosis;
  const rows = [];

  if (diagnosis) {
    rows.push(
      ["范围", diagnosis.port_id ? `${diagnosis.port_id} 号端口` : "整机"],
      ["健康度", diagnosis.health || "unknown"],
      ["证据组", Object.keys(diagnosis.evidence || {}).length],
      ["可疑端口", (diagnosis.suspect_ports || []).join("、") || "无"],
    );
  } else if (port) {
    rows.push(
      ["端口", port.port_id ?? "-"],
      ["模式", port.mode ?? "unknown"],
      ["连接", port.connected ? "connected" : "disconnected"],
      ["功率", formatNumber(port.power_w, "W")],
      ["协议", port.protocol || "none"],
    );
  } else if (result?.device) {
    rows.push(
      ["在线", result.device.online ? "yes" : "no"],
      ["端口数", Object.keys(result.device.ports || {}).length],
      ["型号", result.device.machine?.product_family || result.device.model || "-"],
      ["预算", formatNumber(result.device.machine?.max_power_budget, "W")],
    );
  } else if (result?.response_kind) {
    const kindLabels = {
      conversation: "业务对话",
      knowledge: "知识问答",
      general_knowledge: "通用领域知识",
      explanation: "上下文解释",
      clarification: "需要澄清",
      unsupported: "超出范围",
    };
    rows.push(
      ["类型", kindLabels[result.response_kind] || result.response_kind],
      [
        "工具",
        result.response_kind === "knowledge"
          ? "RAG"
          : result.response_kind === "general_knowledge"
            ? "Ollama（无知识库证据）"
            : "未调用设备",
      ],
    );
  } else {
    rows.push(["状态", result?.ok === false ? "failed" : "暂无"]);
  }

  for (const [label, value] of rows) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = String(label);
    detail.textContent = String(value);
    row.append(term, detail);
    elements.evidence.append(row);
  }
}

function renderKnowledge(run) {
  const citations = Array.isArray(run?.knowledge) ? run.knowledge : [];
  const metadata = run?.knowledge_metadata || {};
  const warnings = Array.isArray(run?.warnings) ? run.warnings : [];
  elements.knowledgeList.replaceChildren();
  elements.warningList.replaceChildren();
  elements.warningList.hidden = warnings.length === 0;

  const status = metadata.status || (citations.length ? "answered" : "not-used");
  elements.knowledgeStatus.textContent = status.replaceAll("_", " ");
  elements.knowledgeStatus.className = `knowledge-badge knowledge-${status}`;

  if (citations.length === 0) {
    const item = document.createElement("li");
    item.className = "knowledge-empty";
    item.textContent = metadata.origin === "general_model"
      ? "来自 Ollama 通用知识，未经过当前知识库验证"
      : status === "no_evidence"
      ? "知识库未找到足够证据"
      : status === "unavailable"
        ? "知识服务当前不可用"
        : "本次请求未调用知识库";
    elements.knowledgeList.append(item);
  } else {
    citations.forEach((citation, index) => {
      const item = document.createElement("li");
      const marker = document.createElement("span");
      marker.textContent = String(index + 1).padStart(2, "0");
      const copy = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = citation.title || citation.id || "未命名来源";
      const section = document.createElement("small");
      section.textContent = citation.section || "文档片段";
      copy.append(title, section);
      item.append(marker, copy);
      elements.knowledgeList.append(item);
    });
  }

  for (const warning of warnings) {
    const item = document.createElement("p");
    item.textContent = warning;
    elements.warningList.append(item);
  }
}

function updatePortsFromResult(result) {
  if (!result) {
    return;
  }
  state.recentPorts.clear();
  const devicePorts = result.device?.ports
    || result.device?.data?.ports
    || result.data?.device?.ports;
  storePorts(devicePorts, {preferActive: true});
  if (result.port) {
    storePort(result.port, {recent: true});
  }
  if (result.after?.status === "ok" && result.after.data) {
    storePort(result.after.data, {recent: true});
  }
  const evidence = result.diagnosis?.evidence || {};
  if (evidence.port_status?.status === "ok") {
    storePort(evidence.port_status.data, {recent: true});
  }
  if (evidence.device_status?.status === "ok") {
    storePorts(evidence.device_status.data?.ports, {preferActive: true});
  }
  renderPorts();
  if (
    devicePorts
    || result.port
    || result.after?.data
    || evidence.port_status?.data
    || evidence.device_status?.data
  ) {
    elements.deviceLastUpdated.textContent = formatSnapshotTime(
      new Date().toISOString(),
    );
  }
}

function storePorts(ports, {preferActive = false} = {}) {
  if (!ports) {
    return;
  }
  const values = Array.isArray(ports) ? ports : Object.values(ports);
  for (const port of values) {
    const recent = preferActive && Boolean(
      port?.mode === "charging"
      || port?.connected
      || Number(port?.power_w) > 0.5,
    );
    storePort(port, {recent});
  }
}

function storePort(port, {recent = false} = {}) {
  if (port && Number.isInteger(Number(port.port_id))) {
    const portId = Number(port.port_id);
    state.ports.set(portId, port);
    if (recent) {
      state.recentPorts.add(portId);
    }
  }
}

function renderPorts() {
  renderDeviceVisualPorts();
  elements.portList.replaceChildren();
  for (const portId of state.allowedPorts) {
    const port = state.ports.get(portId);
    const row = document.createElement("div");
    row.className = "port-row";
    row.classList.toggle("recent", state.recentPorts.has(portId));

    const index = document.createElement("span");
    index.className = "port-index";
    index.textContent = String(portId).padStart(2, "0");

    const primary = document.createElement("div");
    primary.className = "port-primary";
    const title = document.createElement("strong");
    title.textContent = port
      ? PORT_MODE_LABELS[port.mode] || port.mode || "状态未知"
      : state.statusLoading
        ? "正在读取"
        : "暂无数据";
    const subtitle = document.createElement("span");
    subtitle.textContent = port
      ? `${port.connected ? "已连接" : "未连接"} · ${port.protocol || "未协商协议"}`
      : state.statusLoading
        ? "等待设备遥测"
        : "设备未返回该端口";
    primary.append(title, subtitle);

    const power = document.createElement("span");
    power.className = "port-power";
    power.textContent = port ? formatNumber(port.power_w, "W") : "-";
    row.append(index, primary, power);
    elements.portList.append(row);

    const visual = elements.deviceVisualPorts.querySelector(
      `[data-visual-port="${portId}"]`,
    );
    visual?.classList.toggle("active", Boolean(
      port && (port.connected || Number(port.power_w) > 0.5),
    ));
    visual?.classList.toggle("attention", Boolean(
      port && port.connected && Number(port.power_w) <= 0.5,
    ));
  }
}

function renderDeviceVisualPorts() {
  elements.deviceVisualPorts.replaceChildren();
  for (const portId of state.allowedPorts) {
    const port = document.createElement("span");
    port.dataset.visualPort = String(portId);
    port.textContent = `P${String(portId).padStart(2, "0")}`;
    elements.deviceVisualPorts.append(port);
  }
}

function formatPortSummary(port) {
  const mode = port.mode || "unknown";
  const connection = port.connected ? "connected" : "disconnected";
  return `端口 ${port.port_id ?? "-"} · ${mode} · ${connection} · ${formatNumber(port.power_w, "W")}`;
}

function formatNumber(value, unit) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(2)} ${unit}` : "-";
}

async function loadHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) {
      throw new Error("health check failed");
    }
    const health = await response.json();
    elements.healthDot.className = "status-dot online";
    elements.healthLabel.textContent = "ONLINE";
    elements.backendLabel.textContent = String(health.backend).toUpperCase();
    elements.plannerLabel.textContent = String(health.planner).toUpperCase();
    state.plannerMode = String(health.planner || "").toLowerCase();
    elements.knowledgeLabel.textContent = String(health.knowledge).toUpperCase();
    elements.controlLabel.textContent = String(health.control_mode).toUpperCase();
    if (Array.isArray(health.allowed_ports) && health.allowed_ports.length) {
      state.allowedPorts = health.allowed_ports.map(Number);
      renderPorts();
    }
  } catch {
    elements.healthDot.className = "status-dot offline";
    elements.healthLabel.textContent = "OFFLINE";
  }
}

async function loadDeviceInventory() {
  const response = await fetch("/v1/devices");
  if (!response.ok) {
    throw new Error("无法读取设备配置。");
  }
  const inventory = await response.json();
  state.activeProfileId = inventory.active_profile_id || "";
  state.switchingEnabled = Boolean(inventory.switching_enabled);
  state.devices = new Map(
    (inventory.devices || []).map((device) => [device.id, device]),
  );
  const active = state.devices.get(state.activeProfileId);
  if (Array.isArray(active?.allowed_ports) && active.allowed_ports.length) {
    state.allowedPorts = active.allowed_ports.map(Number);
  }
  renderDeviceInventory();
  renderPorts();
}

function renderDeviceInventory() {
  elements.deviceProfileSelect.replaceChildren();
  for (const device of state.devices.values()) {
    const option = document.createElement("option");
    option.value = device.id;
    option.textContent = device.label;
    option.selected = device.id === state.activeProfileId;
    elements.deviceProfileSelect.append(option);
  }
  const active = state.devices.get(state.activeProfileId);
  const label = active?.label || "当前设备";
  elements.devicePanelLabel.textContent = label;
  elements.deviceProfileSelect.disabled = (
    state.busy || !state.switchingEnabled
  );
  elements.deviceProfileSelect.title = state.switchingEnabled
    ? `当前设备：${label}`
    : "只配置了一台授权设备";
}

async function loadDeviceStatus() {
  state.statusLoading = true;
  elements.deviceLastUpdated.textContent = "正在读取";
  elements.deviceModelLabel.textContent = "正在读取设备";
  elements.deviceContext.textContent = "正在同步设备状态";
  elements.deviceLed.className = "device-led loading";
  renderPorts();
  try {
    const response = await fetch("/v1/devices/current/status");
    if (!response.ok) {
      throw new Error("无法读取当前设备状态。");
    }
    const snapshot = await response.json();
    renderDeviceSnapshot(snapshot);
    return snapshot;
  } catch (error) {
    state.statusLoading = false;
    state.deviceSnapshot = null;
    state.ports.clear();
    state.recentPorts.clear();
    elements.devicePowerBudget.textContent = "-- W";
    elements.deviceModelLabel.textContent = "状态不可用";
    elements.deviceContext.textContent = "设备状态读取失败";
    elements.deviceLastUpdated.textContent = "读取失败";
    elements.deviceLed.className = "device-led offline";
    renderPorts();
    throw error;
  }
}

function renderDeviceSnapshot(snapshot) {
  state.statusLoading = false;
  state.deviceSnapshot = snapshot;
  state.ports.clear();
  state.recentPorts.clear();
  storePorts(snapshot?.ports, {preferActive: true});

  const model = snapshot?.model || snapshot?.product_family || "型号未知";
  const budget = Number(snapshot?.power_budget_w);
  elements.devicePowerBudget.textContent = Number.isFinite(budget)
    ? `${Math.round(budget)}W`
    : "-- W";
  elements.deviceModelLabel.textContent = snapshot?.firmware_version
    ? `${model} · ${snapshot.firmware_version}`
    : model;
  elements.deviceContext.textContent = `${model} · ${
    snapshot?.online ? "在线" : "离线"
  }`;
  elements.deviceLastUpdated.textContent = formatSnapshotTime(
    snapshot?.captured_at,
  );
  elements.deviceLed.className = snapshot?.online
    ? "device-led online"
    : "device-led offline";
  renderPorts();

  if (elements.welcomeMessage?.isConnected) {
    elements.welcomeMessage.textContent = deviceSnapshotSummary(snapshot);
  }
}

function deviceSnapshotSummary(snapshot, {includeLabel = true} = {}) {
  const chargingPorts = (snapshot?.ports || [])
    .filter((port) => port.mode === "charging" || Number(port.power_w) > 0.5)
    .map((port) => port.port_id);
  const subject = includeLabel ? snapshot?.label || "当前设备" : "设备";
  if (!snapshot?.online) {
    return `${subject}当前离线，暂时无法执行设备操作。`;
  }
  if (chargingPorts.length === 0) {
    return `${subject}已连接，当前没有端口充电。`;
  }
  return `${subject}已连接，${chargingPorts.join("、")} 号端口正在充电，总输出 ${
    formatCompactPower(snapshot.total_power_w)
  }。`;
}

function formatCompactPower(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}W` : "-- W";
}

function formatSnapshotTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "刚刚";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

async function activateDevice(profileId) {
  if (
    state.busy
    || !state.switchingEnabled
    || profileId === state.activeProfileId
  ) {
    elements.deviceProfileSelect.value = state.activeProfileId;
    return;
  }
  const previousProfileId = state.activeProfileId;
  setBusy(true);
  elements.session.textContent = "切换中";
  elements.healthDot.className = "status-dot";
  elements.healthLabel.textContent = "SWITCHING";
  try {
    const response = await fetch(
      `/v1/devices/${encodeURIComponent(profileId)}/activate`,
      {method: "POST"},
    );
    if (!response.ok) {
      let detail = "设备切换失败，已保留当前设备。";
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string") {
          detail = payload.detail;
        }
      } catch {
        // Keep the generic, credential-free error.
      }
      throw new Error(detail);
    }
    const result = await response.json();
    state.activeProfileId = result.active_device.id;
    state.allowedPorts = result.active_device.allowed_ports.map(Number);
    elements.devicePanelLabel.textContent = result.active_device.label;
    renderDeviceSnapshot(result.snapshot);
    resetConversation(
      `${result.message} ${deviceSnapshotSummary(
        result.snapshot,
        {includeLabel: false},
      )}`,
    );
    await loadDeviceInventory();
    await loadHealth();
  } catch (error) {
    state.activeProfileId = previousProfileId;
    renderDeviceInventory();
    appendMessage(
      "assistant",
      error instanceof Error
        ? error.message
        : "设备切换失败，已保留当前设备。",
      true,
    );
    await loadHealth();
  } finally {
    setBusy(false);
  }
}

function setInspectorOpen(open, {restoreFocus = false} = {}) {
  elements.inspector.classList.toggle("open", open);
  elements.inspector.setAttribute("aria-hidden", String(!open));
  elements.inspectorBackdrop.hidden = !open;
  elements.inspectorToggle.setAttribute("aria-expanded", String(open));
  if (open) {
    elements.inspectorClose.focus();
  } else if (restoreFocus) {
    elements.inspectorToggle.focus();
  }
}

function resetInspector() {
  elements.runStatus.textContent = "IDLE";
  elements.commandAction.textContent = "等待请求";
  elements.commandPort.textContent = "-";
  elements.commandTarget.textContent = "-";
  elements.verification.textContent = "-";
  elements.verification.className = "";
  elements.duration.textContent = "-";
  elements.usage.textContent = "-";
  elements.trace.innerHTML = '<li class="trace-empty">等待执行</li>';
  elements.evidence.innerHTML = "<div><dt>状态</dt><dd>暂无</dd></div>";
  elements.knowledgeStatus.textContent = "未调用";
  elements.knowledgeStatus.className = "knowledge-badge";
  elements.knowledgeList.innerHTML =
    '<li class="knowledge-empty">等待知识证据</li>';
  elements.warningList.replaceChildren();
  elements.warningList.hidden = true;
  elements.inspectorToggleLabel.textContent = "执行证据";
  elements.inspectorToggle.classList.remove("has-evidence");
  setInspectorOpen(false);
}

function resetConversation(message) {
  const nextId = createConversationId();
  state.conversationId = nextId;
  try {
    globalThis.localStorage?.setItem(CONVERSATION_STORAGE_KEY, nextId);
  } catch {
    // The in-memory id is still enough for this tab.
  }
  hideConfirmation();
  resetInspector();
  elements.messages.replaceChildren();
  elements.starterActions.hidden = false;
  appendMessage(
    "assistant",
    message,
  );
  elements.input.focus();
}

function startNewSession() {
  if (state.busy) {
    return;
  }
  resetConversation(
    "新会话已开始。你可以查询、诊断或控制设备，也可以询问固件知识。",
  );
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = elements.input.value;
  elements.input.value = "";
  elements.input.style.height = "";
  void sendRequest(text);
});

elements.input.addEventListener("input", () => {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 112)}px`;
});

elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

elements.newSession.addEventListener("click", startNewSession);

elements.deviceProfileSelect.addEventListener("change", () => {
  void activateDevice(elements.deviceProfileSelect.value);
});

elements.inspectorToggle.addEventListener("click", () => {
  const opening = !elements.inspector.classList.contains("open");
  setInspectorOpen(opening, {restoreFocus: !opening});
});

elements.inspectorClose.addEventListener("click", () => {
  setInspectorOpen(false, {restoreFocus: true});
});

elements.inspectorBackdrop.addEventListener("click", () => {
  setInspectorOpen(false, {restoreFocus: true});
});

globalThis.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.inspector.classList.contains("open")) {
    setInspectorOpen(false, {restoreFocus: true});
  }
});

globalThis.addEventListener("resize", () => {
  setInspectorOpen(false);
});

document.querySelectorAll(".quick-actions button").forEach((button) => {
  button.addEventListener("click", () => {
    void sendRequest(button.dataset.request || "");
  });
});

elements.approve.addEventListener("click", () => {
  void resumeControl(true);
});

elements.reject.addEventListener("click", () => {
  void resumeControl(false);
});

elements.refresh.addEventListener("click", () => {
  if (state.busy) {
    return;
  }
  elements.refresh.classList.add("loading");
  setBusy(true);
  elements.session.textContent = "刷新中";
  void loadDeviceStatus()
    .catch(() => {})
    .finally(() => {
      elements.refresh.classList.remove("loading");
      setBusy(false);
    });
});

async function initializeConsole() {
  renderPorts();
  setInspectorOpen(false);
  try {
    await loadDeviceInventory();
  } catch {
    elements.deviceProfileSelect.replaceChildren();
    const option = document.createElement("option");
    option.textContent = "配置不可用";
    elements.deviceProfileSelect.append(option);
    elements.deviceProfileSelect.disabled = true;
    state.statusLoading = false;
    renderPorts();
    return;
  }
  await Promise.allSettled([loadHealth(), loadDeviceStatus()]);
}

void initializeConsole();
