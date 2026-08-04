"use strict";

const state = {
  busy: false,
  pendingThreadId: null,
  ports: new Map(),
  conversationId: globalThis.crypto?.randomUUID?.()
    || `session-${Date.now()}-${Math.random().toString(16).slice(2)}`,
};

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
  refresh: document.querySelector("#refresh-status"),
  portList: document.querySelector("#port-list"),
  runStatus: document.querySelector("#run-status"),
  commandAction: document.querySelector("#command-action"),
  commandPort: document.querySelector("#command-port"),
  commandTarget: document.querySelector("#command-target"),
  verification: document.querySelector("#verification-status"),
  trace: document.querySelector("#trace-list"),
  evidence: document.querySelector("#evidence-list"),
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

function appendMessage(role, text, isError = false) {
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
  article.append(avatar, content);
  elements.messages.append(article);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function setBusy(busy) {
  state.busy = busy;
  elements.send.disabled = busy;
  elements.approve.disabled = busy;
  elements.reject.disabled = busy;
  elements.refresh.disabled = busy;
  document.querySelectorAll(".quick-actions button").forEach((button) => {
    button.disabled = busy;
  });
  elements.session.textContent = busy ? "执行中" : "就绪";
  elements.session.classList.toggle("busy", busy);
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务返回了无法解析的响应（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail
      : `请求失败（HTTP ${response.status}）`;
    throw new Error(detail);
  }
  return payload;
}

async function sendRequest(requestText, options = {}) {
  const text = requestText.trim();
  if (!text || state.busy) {
    return;
  }
  if (!options.silentUser) {
    appendMessage("user", text);
  }
  setBusy(true);
  elements.runStatus.textContent = "RUNNING";
  try {
    const run = await postJson("/v1/requests", {
      request: text,
      conversation_id: state.conversationId,
    });
    handleRun(run);
  } catch (error) {
    renderFailure(error);
  } finally {
    setBusy(false);
  }
}

async function resumeControl(approved) {
  if (!state.pendingThreadId || state.busy) {
    return;
  }
  appendMessage("user", approved ? "确认执行" : "取消操作");
  setBusy(true);
  elements.runStatus.textContent = approved ? "EXECUTING" : "CANCELLING";
  try {
    const run = await postJson(
      `/v1/requests/${encodeURIComponent(state.pendingThreadId)}/resume`,
      {approved},
    );
    hideConfirmation();
    handleRun(run);
  } catch (error) {
    renderFailure(error);
  } finally {
    setBusy(false);
  }
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
    run.result?.ok === false && !run.result?.cancelled,
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

function updateInspector(run) {
  const command = run.command || {};
  const result = run.result || {};
  const verification = result.verification || {};
  elements.runStatus.textContent = run.status === "confirmation_required"
    ? "WAITING"
    : result.cancelled
      ? "CANCELLED"
      : result.ok === false
        ? "FAILED"
        : "COMPLETED";
  elements.commandAction.textContent = command.action || "-";
  elements.commandPort.textContent = command.port ?? "-";
  elements.commandTarget.textContent = command.enabled === true
    ? "开启"
    : command.enabled === false
      ? "关闭"
      : "查询";
  elements.verification.textContent = verification.status || "-";
  elements.verification.className = verification.status
    ? `verification-${verification.status}`
    : "";

  elements.trace.replaceChildren();
  const trace = Array.isArray(run.trace) ? run.trace : [];
  if (trace.length === 0) {
    const item = document.createElement("li");
    item.className = "trace-empty";
    item.textContent = "暂无 Trace";
    elements.trace.append(item);
  } else {
    trace.forEach((step) => {
      const item = document.createElement("li");
      item.textContent = step;
      elements.trace.append(item);
    });
  }

  renderEvidence(result);
}

function renderEvidence(result) {
  elements.evidence.replaceChildren();
  const port = result?.port || result?.after?.data;
  const diagnosis = result?.diagnosis;
  const rows = [];

  if (port) {
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
  } else if (diagnosis) {
    rows.push(
      ["端口", diagnosis.port_id ?? "-"],
      ["健康度", diagnosis.health || "unknown"],
      ["证据组", Object.keys(diagnosis.evidence || {}).length],
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

function updatePortsFromResult(result) {
  if (!result) {
    return;
  }
  if (result.device?.ports) {
    Object.values(result.device.ports).forEach(storePort);
  }
  if (result.port) {
    storePort(result.port);
  }
  if (result.after?.status === "ok" && result.after.data) {
    storePort(result.after.data);
  }
  renderPorts();
}

function storePort(port) {
  if (port && Number.isInteger(Number(port.port_id))) {
    state.ports.set(Number(port.port_id), port);
  }
}

function renderPorts() {
  elements.portList.replaceChildren();
  for (let portId = 1; portId <= 5; portId += 1) {
    const port = state.ports.get(portId);
    const row = document.createElement("div");
    row.className = "port-row";

    const index = document.createElement("span");
    index.className = "port-index";
    index.textContent = String(portId).padStart(2, "0");

    const primary = document.createElement("div");
    primary.className = "port-primary";
    const title = document.createElement("strong");
    title.textContent = port ? port.mode || "unknown" : "未查询";
    const subtitle = document.createElement("span");
    subtitle.textContent = port
      ? `${port.connected ? "connected" : "disconnected"} · ${port.protocol || "no protocol"}`
      : "no telemetry";
    primary.append(title, subtitle);

    const power = document.createElement("span");
    power.className = "port-power";
    power.textContent = port ? formatNumber(port.power_w, "W") : "-";
    row.append(index, primary, power);
    elements.portList.append(row);

    const visual = document.querySelector(`[data-visual-port="${portId}"]`);
    visual?.classList.toggle("active", Boolean(
      port && (port.connected || Number(port.power_w) > 0.5),
    ));
    visual?.classList.toggle("attention", Boolean(
      port && port.connected && Number(port.power_w) <= 0.5,
    ));
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
  } catch {
    elements.healthDot.className = "status-dot offline";
    elements.healthLabel.textContent = "OFFLINE";
  }
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
  elements.refresh.classList.add("loading");
  void sendRequest("查询设备整体状态", {silentUser: true}).finally(() => {
    elements.refresh.classList.remove("loading");
  });
});

renderPorts();
void loadHealth();
