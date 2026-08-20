const state = {
  workspace: null,
  sessionId: null,
  eventSource: null,
  files: [],
  currentFile: null,
  originalEditor: "",
  busy: false,
  traceFilter: "all",
  pendingConfirmation: null,
  assistantMessage: null,
  recovering: false,
  selectionMode: false,
  selectedFiles: new Set(),
  deleteAction: null,
  canUndo: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const PROTECTED_IMPORT_COMPONENTS = new Set([
  ".git", ".ssh", ".gnupg", ".aws", ".kube", ".venv", "node_modules",
  "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
]);
const PROTECTED_IMPORT_NAMES = new Set([
  ".env", "id_rsa", "id_ed25519", "credentials.json",
]);
const PROTECTED_IMPORT_SUFFIXES = [".pem", ".key", ".p12", ".pfx"];

document.addEventListener("DOMContentLoaded", async () => {
  bindActions();
  setMobileView("work");
  await loadConfig();
  await restoreLastWorkspace();
  window.lucide?.createIcons();
});

function bindActions() {
  $("#openButton").addEventListener("click", () => $("#pathDialog").showModal());
  $("#pickSourceButton").addEventListener("click", pickSourceDirectory);
  $("#closePathDialog").addEventListener("click", () => $("#pathDialog").close());
  $("#cancelPathButton").addEventListener("click", () => $("#pathDialog").close());
  $("#importFilesButton").addEventListener("click", () => $("#filesInput").click());
  $("#filesInput").addEventListener("change", importFiles);
  $("#importButton").addEventListener("click", () => $("#folderInput").click());
  $("#folderInput").addEventListener("change", importFolder);
  $("#pathForm").addEventListener("submit", openLocalPath);
  $("#refreshButton").addEventListener("click", refreshWorkspace);
  $("#fileSearch").addEventListener("input", renderFiles);
  $("#selectFilesButton").addEventListener("click", toggleSelectionMode);
  $("#deleteSelectedButton").addEventListener("click", requestDeleteSelected);
  $("#clearAllButton").addEventListener("click", requestClearAll);
  $("#undoButton").addEventListener("click", undoLastChange);
  $("#sendButton").addEventListener("click", sendMessage);
  $("#messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) sendMessage();
  });
  $("#stopButton").addEventListener("click", stopTask);
  $("#saveButton").addEventListener("click", saveFile);
  $("#codeEditor").addEventListener("input", updateEditorDirty);
  $("#clearTraceButton").addEventListener("click", () => {
    $("#traceList").innerHTML = "";
    $("#traceList").classList.add("empty");
    $("#traceList").innerHTML = `<div class="trace-placeholder"><i data-lucide="activity"></i><p>轨迹已清空，后续事件仍会继续显示。</p></div>`;
    window.lucide?.createIcons();
  });
  $("#shellToggle").addEventListener("change", async () => {
    if (state.workspace) {
      await createSession();
      toast("终端权限已更新，并创建了新的 Agent 会话");
    }
  });
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$("[data-mobile-view]").forEach((button) => button.addEventListener("click", () => setMobileView(button.dataset.mobileView)));
  $$("[data-filter]").forEach((button) => button.addEventListener("click", () => setTraceFilter(button.dataset.filter)));
  $("#approveButton").addEventListener("click", () => resolveConfirmation(true));
  $("#denyButton").addEventListener("click", () => resolveConfirmation(false));
  $("#closeDeleteDialog").addEventListener("click", closeDeleteDialog);
  $("#cancelDeleteButton").addEventListener("click", closeDeleteDialog);
  $("#confirmDeleteButton").addEventListener("click", executeDeleteAction);
  $("#clearConfirmationInput").addEventListener("input", updateDeleteConfirmation);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    $("#modelLabel").textContent = config.configured ? `${config.model} · ${config.provider}` : "模型未配置";
    if (!config.configured) toast("Web 工作台已启动，但发送任务前需要配置模型 API Key");
  } catch (error) {
    $("#modelLabel").textContent = "服务不可用";
  }
}

async function openLocalPath(event) {
  event.preventDefault();
  const path = $("#pathInput").value.trim();
  if (!path) return;
  try {
    const workspace = await api("/api/workspaces/open", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    $("#pathDialog").close();
    await activateWorkspace(workspace);
  } catch (error) {
    toast(error.message);
  }
}

async function pickSourceDirectory() {
  try {
    const workspace = await api("/api/workspaces/pick", {
      method: "POST",
      body: "{}",
    });
    if (workspace.cancelled) return;
    await activateWorkspace(workspace);
  } catch (error) {
    toast(error.message);
  }
}

async function importFolder(event) {
  const files = [...event.target.files];
  if (!files.length) return;
  const selected = files.filter((file) => !isProtectedImportPath(file.webkitRelativePath));
  const skipped = files.length - selected.length;
  if (!selected.length) {
    toast("所选目录中没有可导入的源码文件；凭据、缓存和依赖目录已被安全策略跳过");
    event.target.value = "";
    return;
  }
  toast(`正在读取 ${selected.length} 个文件...`);
  try {
    const encoded = await Promise.all(selected.map(async (file) => ({
      path: file.webkitRelativePath.split("/").slice(1).join("/") || file.name,
      content_base64: arrayBufferToBase64(await file.arrayBuffer()),
    })));
    const name = files[0].webkitRelativePath.split("/")[0] || "workspace";
    const workspace = await api("/api/workspaces/import", {
      method: "POST",
      body: JSON.stringify({ name, files: encoded }),
    });
    await activateWorkspace(workspace);
    if (skipped) toast(`已导入 ${selected.length} 个文件，自动跳过 ${skipped} 个受保护文件`);
  } catch (error) {
    toast(error.message);
  } finally {
    event.target.value = "";
  }
}

async function importFiles(event) {
  const files = [...event.target.files];
  if (!files.length) return;
  const selected = files.filter((file) => !isProtectedImportPath(file.name));
  const skipped = files.length - selected.length;
  if (!selected.length) {
    toast("所选文件属于凭据或受保护内容，未授予 Agent 访问权限");
    event.target.value = "";
    return;
  }
  toast(`正在读取 ${selected.length} 个文件...`);
  try {
    const encoded = await Promise.all(selected.map(async (file) => ({
      path: file.name,
      content_base64: arrayBufferToBase64(await file.arrayBuffer()),
    })));
    const workspace = await api("/api/workspaces/import", {
      method: "POST",
      body: JSON.stringify({ name: "uploaded-files", files: encoded }),
    });
    await activateWorkspace(workspace);
    if (skipped) toast(`已导入 ${selected.length} 个文件，自动跳过 ${skipped} 个受保护文件`);
  } catch (error) {
    toast(error.message);
  } finally {
    event.target.value = "";
  }
}

function isProtectedImportPath(rawPath) {
  const parts = rawPath.replaceAll("\\", "/").split("/").filter(Boolean);
  const lowerParts = parts.map((part) => part.toLowerCase());
  if (lowerParts.some((part) => PROTECTED_IMPORT_COMPONENTS.has(part) || part.endsWith(".egg-info"))) {
    return true;
  }
  const name = lowerParts.at(-1) || "";
  return PROTECTED_IMPORT_NAMES.has(name)
    || (name.startsWith(".env.") && name !== ".env.example")
    || PROTECTED_IMPORT_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

async function activateWorkspace(workspace) {
  state.workspace = workspace;
  state.files = workspace.files || [];
  state.currentFile = null;
  state.selectionMode = false;
  state.selectedFiles.clear();
  state.canUndo = false;
  const sourceWorkspace = workspace.workspace_mode === "source";
  if (sourceWorkspace) localStorage.setItem("zhixu.lastWorkspacePath", workspace.root);
  else localStorage.removeItem("zhixu.lastWorkspacePath");
  $("#workspaceLabel").textContent = workspace.name;
  $("#workspaceMode").textContent = sourceWorkspace ? "源代码直连" : "临时副本";
  $("#workspaceMode").classList.toggle("copy", !sourceWorkspace);
  $("#workspacePath").textContent = sourceWorkspace
    ? `${workspace.root} · Agent 修改会直接写回该目录`
    : `${workspace.root} · 临时副本，修改不会回写原始文件`;
  $("#statusDot").className = "status-dot ready";
  $("#fileSearch").disabled = false;
  $("#refreshButton").disabled = false;
  $("#selectFilesButton").disabled = false;
  $("#selectFilesButton").title = "选择要从 Agent 权限列表移除的文件";
  $("#clearAllButton").title = "清空 Agent 文件权限列表（不删除本机文件）";
  $("#undoButton").disabled = true;
  $("#codeEditor").value = "";
  $("#editorPath").textContent = "未选择文件";
  renderFiles();
  setWelcome(false);
  await refreshDiff();
  await createSession();
  setMobileView("work");
  toast(sourceWorkspace
    ? "已连接源代码目录，发送任务会直接修改本地文件"
    : "已打开临时副本，修改不会回写原始文件");
}

async function restoreLastWorkspace() {
  const path = localStorage.getItem("zhixu.lastWorkspacePath");
  if (!path) return;
  try {
    const workspace = await api("/api/workspaces/open", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    await activateWorkspace(workspace);
  } catch (_error) {
    localStorage.removeItem("zhixu.lastWorkspacePath");
  }
}

async function createSession() {
  if (state.eventSource) {
    state.eventSource.onerror = null;
    state.eventSource.close();
  }
  state.eventSource = null;
  state.sessionId = null;
  try {
    const requestedShell = $("#shellToggle").checked;
    const response = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: state.workspace.id,
        allow_shell: requestedShell,
      }),
    });
    if (requestedShell && !response.allow_shell) {
      $("#shellToggle").checked = false;
      toast("文件权限已收紧，为防止终端绕过限制，本会话已关闭终端工具");
    }
    state.sessionId = response.session_id;
    connectEvents();
    $("#messageInput").disabled = false;
    $("#sendButton").disabled = false;
    $("#composerHint").textContent = "Ctrl + Enter 发送 · 文件修改受工作区策略约束";
  } catch (error) {
    $("#messageInput").disabled = true;
    $("#sendButton").disabled = true;
    $("#composerHint").textContent = error.message;
    toast(error.message);
  }
}

function connectEvents() {
  const source = new EventSource(`/api/sessions/${state.sessionId}/events`);
  state.eventSource = source;
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    handleEvent(event);
  };
  source.onerror = () => {
    if (source !== state.eventSource) return;
    source.close();
    state.eventSource = null;
    recoverSession();
  };
}

async function recoverSession() {
  if (state.recovering) return;
  state.recovering = true;
  setBusy(false);
  $("#messageInput").disabled = true;
  $("#sendButton").disabled = true;
  $("#composerHint").textContent = "服务已重启，正在恢复工作区和会话...";
  await new Promise((resolve) => setTimeout(resolve, 700));
  const path = localStorage.getItem("zhixu.lastWorkspacePath");
  try {
    if (!path) throw new Error("没有可恢复的工作区路径");
    await api("/api/health");
    const workspace = await api("/api/workspaces/open", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    await activateWorkspace(workspace);
    toast("服务连接已恢复，已创建新的 Agent 会话");
  } catch (_error) {
    $("#composerHint").textContent = "等待本地服务恢复...";
    setTimeout(() => {
      state.recovering = false;
      recoverSession();
    }, 1800);
    return;
  }
  state.recovering = false;
}

function handleEvent(event) {
  const { type, data } = event;
  if (type === "user_message") appendMessage("user", data.text);
  if (type === "assistant_delta") appendAssistantDelta(data.text);
  if (type === "task_completed" || type === "task_failed" || type === "task_aborted") {
    setBusy(false);
    state.assistantMessage = null;
    refreshWorkspace();
  }
  if (type === "workspace_diff_updated") refreshWorkspace();
  if (type === "confirmation_required") showConfirmation(data);
  if (type !== "assistant_delta" && type !== "user_message") appendTrace(event);
}

async function sendMessage() {
  const input = $("#messageInput");
  const message = input.value.trim();
  if (!message || !state.sessionId || state.busy) return;
  input.value = "";
  state.assistantMessage = null;
  setBusy(true);
  try {
    await api(`/api/sessions/${state.sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
  } catch (error) {
    setBusy(false);
    toast(error.message);
  }
}

async function stopTask() {
  if (!state.sessionId) return;
  await api(`/api/sessions/${state.sessionId}/abort`, { method: "POST", body: "{}" }).catch((error) => toast(error.message));
}

function setBusy(busy) {
  state.busy = busy;
  $("#statusDot").className = `status-dot ${busy ? "busy" : "ready"}`;
  $("#sendButton").disabled = busy || !state.sessionId;
  $("#stopButton").hidden = !busy;
  $("#selectFilesButton").disabled = busy || !state.workspace;
  $("#clearAllButton").disabled = busy || !state.workspace || !state.files.length;
  $("#undoButton").disabled = busy || !state.canUndo;
  updateSelectionControls();
  $("#composerHint").textContent = busy ? "Agent 正在读取上下文并执行任务" : "Ctrl + Enter 发送 · 文件修改受工作区策略约束";
}

function appendMessage(role, text) {
  setWelcome(false);
  const message = document.createElement("article");
  message.className = `message ${role}`;
  message.innerHTML = `<div class="message-meta"><i data-lucide="${role === "user" ? "user" : "sparkles"}"></i><span>${role === "user" ? "你" : "智修 Agent"}</span></div><div class="message-content"></div>`;
  message.querySelector(".message-content").textContent = text;
  $("#messages").append(message);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  window.lucide?.createIcons();
  return message;
}

function appendAssistantDelta(text) {
  if (!state.assistantMessage) state.assistantMessage = appendMessage("assistant", "");
  state.assistantMessage.querySelector(".message-content").textContent += text;
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

function setWelcome(show) {
  const welcome = $(".welcome-state");
  if (show) return;
  welcome?.remove();
}

function renderFiles() {
  const tree = $("#fileTree");
  const query = $("#fileSearch").value.trim().toLowerCase();
  const files = state.files.filter((file) => file.path.toLowerCase().includes(query));
  $("#fileCount").textContent = state.files.length;
  $("#clearAllButton").disabled = !state.workspace || !state.files.length || state.busy;
  tree.classList.toggle("empty", !files.length);
  if (!files.length) {
    tree.textContent = state.files.length ? "没有匹配文件" : "暂无文件";
    updateSelectionControls();
    return;
  }
  tree.innerHTML = "";
  for (const file of files) {
    const button = document.createElement("button");
    button.className = `file-item ${state.currentFile === file.path ? "active" : ""} ${state.selectedFiles.has(file.path) ? "selected" : ""}`;
    button.title = file.path;
    button.innerHTML = `${state.selectionMode ? `<input class="selection-box" type="checkbox" ${state.selectedFiles.has(file.path) ? "checked" : ""} tabindex="-1">` : ""}<i data-lucide="file-code-2"></i><span>${escapeHtml(file.path)}</span>`;
    button.addEventListener("click", () => state.selectionMode ? toggleFileSelection(file.path) : openFile(file.path));
    tree.append(button);
  }
  updateSelectionControls();
  window.lucide?.createIcons();
}

function toggleSelectionMode() {
  state.selectionMode = !state.selectionMode;
  if (!state.selectionMode) state.selectedFiles.clear();
  renderFiles();
}

function toggleFileSelection(path) {
  if (state.selectedFiles.has(path)) state.selectedFiles.delete(path);
  else state.selectedFiles.add(path);
  renderFiles();
}

function updateSelectionControls() {
  const selectButton = $("#selectFilesButton");
  if (!selectButton) return;
  selectButton.querySelector("span").textContent = state.selectionMode ? "取消选择" : "选择";
  const deleteButton = $("#deleteSelectedButton");
  deleteButton.hidden = !state.selectionMode;
  deleteButton.disabled = !state.selectedFiles.size || state.busy;
  deleteButton.querySelector("span").textContent = state.selectedFiles.size
    ? `移除所选 (${state.selectedFiles.size})`
    : "移除所选";
}

function requestDeleteSelected() {
  if (!state.selectedFiles.size || state.busy) return;
  state.deleteAction = { type: "selected", paths: [...state.selectedFiles] };
  $("#deleteTitle").textContent = "移除所选文件";
  $("#deleteMessage").textContent = `将从 Agent 工作区移除 ${state.selectedFiles.size} 个文件。Agent 随后无法读取或修改这些文件，本机来源文件不会被删除。`;
  $("#clearConfirmationGroup").hidden = true;
  $("#confirmDeleteButton").disabled = false;
  $("#deleteDialog").showModal();
}

function requestClearAll() {
  if (!state.files.length || state.busy) return;
  state.deleteAction = { type: "all" };
  $("#deleteTitle").textContent = "清空文件列表";
  $("#deleteMessage").textContent = `将从 Agent 工作区移除全部 ${state.files.length} 个文件。Agent 将无法继续读取或修改它们，本机来源文件不会被删除。`;
  $("#clearConfirmationGroup").hidden = true;
  $("#confirmDeleteButton").disabled = false;
  $("#deleteDialog").showModal();
}

function updateDeleteConfirmation() {
  if (state.deleteAction?.type !== "all") return;
  $("#confirmDeleteButton").disabled = $("#clearConfirmationInput").value.trim() !== state.workspace.name;
}

function closeDeleteDialog() {
  state.deleteAction = null;
  $("#deleteDialog").close();
}

async function executeDeleteAction() {
  const action = state.deleteAction;
  if (!action || !state.workspace) return;
  $("#confirmDeleteButton").disabled = true;
  try {
    let result;
    if (action.type === "selected") {
      result = await api(`/api/workspaces/${state.workspace.id}/access/remove`, {
        method: "POST",
        body: JSON.stringify({ paths: action.paths }),
      });
    } else {
      result = await api(`/api/workspaces/${state.workspace.id}/access/clear`, {
        method: "POST",
        body: JSON.stringify({ confirmation: state.workspace.name }),
      });
    }
    const removed = new Set(result.paths || []);
    if (state.currentFile && removed.has(state.currentFile)) clearEditor();
    state.selectedFiles.clear();
    state.selectionMode = false;
    closeDeleteDialog();
    await refreshWorkspace();
    await createSession();
    toast(`已撤销 ${result.removed} 个文件的 Agent 读写权限；本机文件未删除`);
  } catch (error) {
    $("#confirmDeleteButton").disabled = false;
    toast(error.message);
  }
}

function clearEditor() {
  state.currentFile = null;
  state.originalEditor = "";
  $("#codeEditor").value = "";
  $("#codeEditor").disabled = true;
  $("#editorPath").textContent = "未选择文件";
  $("#saveButton").disabled = true;
  $("#editorDirty").textContent = "";
}

async function openFile(path) {
  if (!state.workspace) return;
  try {
    const file = await api(`/api/workspaces/${state.workspace.id}/file?path=${encodeURIComponent(path)}`);
    state.currentFile = path;
    state.originalEditor = file.content;
    $("#codeEditor").value = file.content;
    $("#codeEditor").disabled = false;
    $("#editorPath").textContent = path;
    $("#saveButton").disabled = true;
    renderFiles();
    setView("editor");
    if (window.innerWidth <= 760) setMobileView("work");
  } catch (error) {
    toast(error.message);
  }
}

function updateEditorDirty() {
  const dirty = $("#codeEditor").value !== state.originalEditor;
  $("#saveButton").disabled = !dirty;
  $("#editorDirty").textContent = dirty ? "●" : "";
}

async function saveFile() {
  if (!state.currentFile) return;
  try {
    await api(`/api/workspaces/${state.workspace.id}/file?path=${encodeURIComponent(state.currentFile)}`, {
      method: "PUT",
      body: JSON.stringify({ content: $("#codeEditor").value }),
    });
    state.originalEditor = $("#codeEditor").value;
    updateEditorDirty();
    await refreshDiff();
    toast(`已保存 ${state.currentFile}`);
  } catch (error) {
    toast(error.message);
  }
}

async function refreshWorkspace() {
  if (!state.workspace) return;
  try {
    const tree = await api(`/api/workspaces/${state.workspace.id}/tree`);
    state.files = tree.files;
    const existingPaths = new Set(state.files.map((file) => file.path));
    state.selectedFiles = new Set([...state.selectedFiles].filter((path) => existingPaths.has(path)));
    if (state.currentFile && !existingPaths.has(state.currentFile)) clearEditor();
    renderFiles();
    await refreshDiff();
    if (state.currentFile && state.files.some((file) => file.path === state.currentFile) && $("#codeEditor").value === state.originalEditor) {
      const file = await api(`/api/workspaces/${state.workspace.id}/file?path=${encodeURIComponent(state.currentFile)}`);
      state.originalEditor = file.content;
      $("#codeEditor").value = file.content;
    }
  } catch (error) {
    toast(error.message);
  }
}

async function refreshDiff() {
  if (!state.workspace) return;
  const diff = await api(`/api/workspaces/${state.workspace.id}/diff`);
  state.canUndo = Boolean(diff.can_undo);
  $("#undoButton").disabled = state.busy || !state.canUndo;
  $("#diffCount").textContent = diff.files.length;
  $("#diffSummary").textContent = diff.files.length
    ? `${diff.files.length} 个文件发生变化：${diff.files.map((file) => file.path).join("、")}`
    : "当前没有修改";
  $("#diffOutput").textContent = diff.patch || "当前工作区与打开时的基线一致。";
}

async function undoLastChange() {
  if (!state.workspace || !state.canUndo || state.busy) return;
  $("#undoButton").disabled = true;
  try {
    const result = await api(`/api/workspaces/${state.workspace.id}/undo`, {
      method: "POST",
      body: "{}",
    });
    state.canUndo = Boolean(result.can_undo);
    await refreshWorkspace();
    toast(`已撤销上次 Agent 修改，恢复 ${result.changed_files} 个文件`);
  } catch (error) {
    toast(error.message);
    await refreshDiff();
  }
}

const TRACE_META = {
  session_created: ["会话已创建", "circle-play", "decision"],
  conversation_loaded: ["加载对话", "messages-square", "decision"],
  context_loaded: ["加载上下文", "layers-3", "decision"],
  task_classified: ["任务分类", "tags", "decision"],
  model_request_started: ["请求模型", "brain-circuit", "decision"],
  model_response_received: ["模型响应", "message-square-more", "decision"],
  agent_decision: ["Agent 决策", "route", "decision"],
  permission_decision: ["权限判定", "shield-check", "decision"],
  tool_call_started: ["调用工具", "wrench", "tool"],
  tool_call_completed: ["工具返回", "check", "tool"],
  confirmation_required: ["等待确认", "triangle-alert", "warning"],
  confirmation_resolved: ["确认完成", "badge-check", "decision"],
  context_compaction_started: ["压缩上下文", "minimize-2", "decision"],
  context_compaction_completed: ["压缩完成", "check", "decision"],
  workspace_diff_updated: ["更新 Diff", "git-compare-arrows", "tool"],
  task_completed: ["任务完成", "circle-check-big", "usage"],
  task_failed: ["任务失败", "circle-x", "error"],
  task_aborted: ["任务已停止", "square", "warning"],
};

function appendTrace(event) {
  const list = $("#traceList");
  if (list.classList.contains("empty")) {
    list.classList.remove("empty");
    list.innerHTML = "";
  }
  const [label, icon, category] = TRACE_META[event.type] || [event.type, "circle", "decision"];
  const item = document.createElement("article");
  item.className = `trace-item ${category}`;
  item.dataset.category = category;
  const details = summarizeEvent(event.type, event.data);
  item.innerHTML = `<span class="trace-icon"><i data-lucide="${icon}"></i></span><div class="trace-head"><strong>${label}</strong><time>${formatTime(event.timestamp)}</time></div><div class="trace-detail"></div>`;
  item.querySelector(".trace-detail").textContent = details;
  item.hidden = state.traceFilter !== "all" && state.traceFilter !== category;
  list.append(item);
  list.scrollTop = list.scrollHeight;
  window.lucide?.createIcons();
}

function summarizeEvent(type, data) {
  if (type === "task_classified") return `${data.category} · 置信度 ${Math.round(data.confidence * 100)}%${data.evidence?.length ? `\n依据：${data.evidence.join("、")}` : ""}`;
  if (type === "context_loaded") return `${data.working_directory}\n${data.tool_count} 个工具 · 上下文窗口 ${number(data.context_window)} tokens`;
  if (type === "conversation_loaded") return `${data.provider} / ${data.model}\n历史消息 ${data.message_count} 条`;
  if (type === "agent_decision") return data.decision === "use_tools" ? `使用工具：${(data.tools || []).join("、")}` : "直接向用户响应";
  if (type === "permission_decision") return `${data.tool} → ${data.action}${data.reason ? `\n${data.reason}` : ""}`;
  if (type === "tool_call_started") return `${data.tool}\n${data.input_preview || ""}`;
  if (type === "tool_call_completed") return `${data.tool} · ${data.success ? "成功" : "失败"} · ${data.duration_ms} ms\n${data.result_preview || ""}`;
  if (type === "task_completed") {
    const usage = data.usage || {};
    return `${usage.turns || 0} 轮 · ${(Number(data.duration_ms || 0) / 1000).toFixed(2)} 秒\n${number(usage.input_tokens || 0)} 输入 / ${number(usage.output_tokens || 0)} 输出\n估算费用 $${Number(usage.estimated_cost_usd || 0).toFixed(6)}`;
  }
  if (type === "model_response_received") return `${data.tool_count || 0} 个工具请求`;
  if (type === "workspace_diff_updated") return `${data.changed_files} 个文件发生变化`;
  if (type === "session_created") return `${data.model} · Shell ${data.allow_shell ? "已开启" : "已关闭"}`;
  return Object.entries(data || {}).map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`).join("\n");
}

function setTraceFilter(filter) {
  state.traceFilter = filter;
  $$("[data-filter]").forEach((button) => button.classList.toggle("active", button.dataset.filter === filter));
  $$(".trace-item").forEach((item) => { item.hidden = filter !== "all" && item.dataset.category !== filter; });
}

function setView(view) {
  $$("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach((panel) => panel.classList.toggle("active", panel.id === `${view}View`));
  if (view === "diff") refreshDiff().catch((error) => toast(error.message));
}

function setMobileView(view) {
  $$("[data-mobile-view]").forEach((button) => button.classList.toggle("active", button.dataset.mobileView === view));
  $$("[data-panel]").forEach((panel) => panel.classList.toggle("mobile-active", panel.dataset.panel === view));
}

function showConfirmation(data) {
  state.pendingConfirmation = data;
  $("#confirmMessage").textContent = data.message;
  $("#confirmDialog").showModal();
}

async function resolveConfirmation(approved) {
  const confirmation = state.pendingConfirmation;
  if (!confirmation) return;
  try {
    await api(`/api/sessions/${state.sessionId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ confirmation_id: confirmation.confirmation_id, approved }),
    });
  } catch (error) {
    toast(error.message);
  } finally {
    state.pendingConfirmation = null;
    $("#confirmDialog").close();
  }
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3200);
}

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatTime(timestamp) {
  return new Date(timestamp).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
