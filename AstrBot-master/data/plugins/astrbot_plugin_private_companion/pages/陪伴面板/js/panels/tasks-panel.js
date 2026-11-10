(() => {
function mountTasksPanel(root, { fetchJson, showToast }) {
  const labels = { queued: "待执行", running: "执行中", waiting_retry: "等待重试", waiting_user: "等待补充", paused: "已暂停", unknown_result: "结果待核实", succeeded: "已完成", failed: "失败", cancelled: "已取消" };
  let offset = 0;
  let sequence = 0;
  const element = (tag, text = "") => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  };
  const button = (text, action) => {
    const node = element("button", text);
    node.type = "button";
    node.className = "btn btn-secondary";
    node.addEventListener("click", async () => {
      node.disabled = true;
      try { await action(); } catch (error) { showToast?.(error.message || "操作失败", "error"); }
      finally { node.disabled = false; }
    });
    return node;
  };
  const field = (parent, title, placeholder = "", type = "text") => {
    const label = element("label", title);
    const input = element("input");
    input.type = type;
    input.placeholder = placeholder;
    input.className = "form-input";
    label.append(input);
    parent.append(label);
    return input;
  };
  const choice = (parent, title, options) => {
    const label = element("label", title);
    const select = element("select");
    select.className = "form-input";
    for (const [value, text] of options) {
      const option = element("option", text);
      option.value = value;
      select.append(option);
    }
    label.append(select);
    parent.append(label);
    return select;
  };
  async function call(path, body) {
    const response = await fetchJson(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
    if (response?.success === false) throw new Error(response.error || "操作失败");
    return response?.data ?? response;
  }
  const scope = () => ({ bot_id: bot.value.trim(), session_key: session.value.trim(), sender_id: sender.value.trim() });
  const query = (extra = {}) => new URLSearchParams({ ...scope(), ...extra }).toString();
  const section = element("section");
  section.className = "tasks-panel";
  section.append(element("h2", "任务管理"), element("p", "选择机器人、会话和真实发起者，查看任务及执行情况。"));
  const filters = element("div");
  filters.className = "form-grid";
  const bot = field(filters, "机器人实例", "填写机器人实例标识");
  const session = field(filters, "会话标识", "例如 bot:GroupMessage:群号");
  const sender = field(filters, "真实发起者账号", "填写平台账号，不能填写昵称");
  section.append(filters);
  const list = element("div");
  const pagination = element("p");
  const detail = element("div");
  let detailSequence = 0;
  const recordedTime = value => value == null ? "未记录" : new Date(value * 1000).toLocaleString();
  const facts = task => `${task.engine === "codex" ? "Codex" : "原生"}；模型：${task.codex?.model || task.model_id || "未记录"}；思考：${task.codex?.reasoning_effort || "未记录"}；阶段：${task.execution?.phase_label || "未确认"}；创建：${recordedTime(task.created_at)}；更新：${recordedTime(task.updated_at)}；结束：${recordedTime(task.finished_at)}`;
  async function showDetail(taskId) {
    const ticket = ++detailSequence;
    const selectedScope = scope();
    const detailQuery = extra => new URLSearchParams({ ...selectedScope, task_id: taskId, ...extra }).toString();
    const data = await call(`/tasks/detail?${query({ task_id: taskId })}`);
    if (ticket !== detailSequence) return;
    const task = data.task;
    detail.replaceChildren(element("h3", task.goal), element("p", `状态：${labels[task.status] || task.status}；尝试次数：${task.attempts}`));
    detail.append(element("p", facts(task)));
    if (task.engine === "codex") detail.append(element("p", `对话：${task.codex?.thread_id || "尚未建立"}；连接：${task.codex?.connection_state || "未连接"}。运行状态和宿主心跳不代表持续推进。`));
    if (task.error_kind) detail.append(element("p", `错误：${task.error_kind}`));
    const controls = element("div");
    for (const [action, title] of [["pause", "暂停"], ["resume", "继续"], ["retry", "重试"], ["cancel", "取消任务"]]) {
      if (task.status === "unknown_result" && ["resume", "retry"].includes(action) && task.engine !== "codex") continue;
      const controlTitle = task.engine === "codex" && task.status === "unknown_result" && ["resume", "retry"].includes(action) ? `核实原进度并${title}` : title;
      controls.append(button(controlTitle, async () => {
        const result = await call("/tasks/control", { ...selectedScope, task_id: taskId, action });
        showToast?.(result.task?.status === "running" && ["pause", "cancel"].includes(action) ? "已请求中断，等待真实执行回执" : "任务状态已更新", "success");
        await load();
        await showDetail(taskId);
      }));
    }
    detail.append(controls, element("h4", "步骤与执行结果"), element("pre", JSON.stringify({ steps: task.steps, result: task.result }, null, 2)));
    if (task.engine === "codex") {
      detail.append(element("h4", "Codex实际事件"));
      const timeline = element("div");
      let cursor = data.codex_events?.next_after;
      const appendEvents = page => {
        for (const event of page?.items || []) timeline.append(element("pre", `${recordedTime(event.created_at)} ${event.method}\n${JSON.stringify(event.payload, null, 2)}`));
      };
      appendEvents(data.codex_events);
      const more = button("继续读取事件", async () => {
        const nextPage = await call(`/tasks/detail?${detailQuery({ after: cursor })}`);
        if (ticket !== detailSequence) return;
        appendEvents(nextPage.codex_events);
        cursor = nextPage.codex_events?.next_after;
        more.hidden = cursor == null;
      });
      more.hidden = cursor == null;
      detail.append(timeline, more, element("p", "结果未知不会自动重放；显式继续会让原对话先核实旧回执与实际进度。"));
    }
    if (task.engine !== "codex" && ["queued", "paused"].includes(task.status) && !Object.keys(task.steps || {}).length) {
      const goal = field(detail, "修订目标");
      goal.value = task.goal;
      detail.append(button("保存目标修订", async () => {
        await call("/tasks/control", { ...scope(), task_id: taskId, action: "revise", goal: goal.value });
        showToast?.("任务目标已修订", "success");
        await load();
        await showDetail(taskId);
      }));
    }
    if (task.status === "unknown_result") {
      detail.append(element("p", "请先核对实际文件、平台回执及进程。仅填写已核实证据，不能根据模型描述判断完成。"));
      const outcome = choice(detail, "核实结果", [["", "请选择"], ["completed", "已完成"], ["not_executed", "确认未执行，保持暂停"]]);
      const evidence = field(detail, "核实证据（JSON对象）", '{"receipt":"真实回执"}');
      const workerStopped = field(detail, "已核实执行进程已停止", "", "checkbox");
      detail.append(button("提交核实结果", async () => {
        const verified = JSON.parse(evidence.value);
        if (!verified || Array.isArray(verified) || typeof verified !== "object" || !Object.keys(verified).length) throw new Error("请填写非空JSON证据对象");
        if (!["completed", "not_executed"].includes(outcome.value)) throw new Error("请选择明确核实结果");
        await call("/tasks/control", { ...scope(), task_id: taskId, action: "reconcile", outcome: outcome.value, evidence: verified, worker_stopped: workerStopped.checked });
        showToast?.("核实结果已记录", "success");
        await load();
        await showDetail(taskId);
      }));
    }
  }
  async function load() {
    const ticket = ++sequence;
    const data = await call(`/tasks?${query({ offset })}`);
    if (ticket !== sequence) return;
    list.replaceChildren();
    for (const task of data.tasks || []) {
      const row = element("div");
      row.append(button(task.goal || task.task_id, () => showDetail(task.task_id)), element("span", ` ${labels[task.status] || task.status}；${facts(task)}；对话：${task.codex?.thread_id || "无"}`));
      list.append(row);
    }
    if (!data.tasks?.length) list.append(element("p", "当前范围暂无任务。"));
    pagination.textContent = `共 ${data.total || 0} 项，第 ${Math.floor(offset / 20) + 1} 页`;
    previous.disabled = offset === 0;
    next.disabled = offset + 20 >= (data.total || 0);
  }
  const previous = button("上一页", async () => { offset = Math.max(0, offset - 20); await load(); });
  const next = button("下一页", async () => { offset += 20; await load(); });
  section.append(button("查询任务", async () => { offset = 0; ++detailSequence; detail.replaceChildren(); await load(); }), list, pagination, previous, next, detail);

  const grantsSection = element("details");
  grantsSection.append(element("summary", "群友执行授权"), element("p", "授权仅对所选机器人和群生效；主要用户身份由服务器验证。"));
  const group = field(grantsSection, "群号", "填写真实群号");
  const member = field(grantsSection, "被授权账号", "填写真实平台账号");
  const capabilities = field(grantsSection, "允许能力", "例如 read,write,network");
  const roots = element("textarea");
  roots.placeholder = "可用目录，每行一个完整路径";
  const rootLabel = element("label", "可用目录");
  rootLabel.append(roots);
  grantsSection.append(rootLabel);
  const expiry = field(grantsSection, "到期时间（留空表示长期）", "", "datetime-local");
  const grantsList = element("div");
  const grantScope = () => ({ bot_id: bot.value.trim(), group_id: group.value.trim() });
  async function loadGrants() {
    const data = await call(`/tasks/grants?${new URLSearchParams(grantScope())}`);
    grantsList.replaceChildren(element("p", `可授予能力：${(data.allowed_capabilities || []).join("、")}`));
    for (const grant of data.grants || []) {
      const row = element("div");
      row.append(element("p", `${grant.user_id}；能力：${grant.capabilities.join("、")}；目录：${grant.roots.join("；")}；${grant.revoked_at ? "已撤销" : grant.expires_at ? `到期：${new Date(grant.expires_at * 1000).toLocaleString()}` : "长期有效"}`));
      if (!grant.revoked_at) row.append(button("撤销授权", async () => {
        await call("/tasks/grants/revoke", { ...grantScope(), grant_id: grant.grant_id });
        await loadGrants();
      }));
      grantsList.append(row);
    }
  }
  grantsSection.append(button("查看授权", loadGrants), button("保存授权", async () => {
    const expiresAt = expiry.value ? new Date(expiry.value).getTime() / 1000 : null;
    await call("/tasks/grants/grant", { ...grantScope(), user_id: member.value.trim(), capabilities: capabilities.value.split(/[,，\s]+/).filter(Boolean), roots: roots.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean), expires_at: expiresAt });
    showToast?.("授权已保存", "success");
    await loadGrants();
  }), grantsList);
  section.append(grantsSection);

  const proactive = element("details");
  proactive.append(element("summary", "主动任务推送规则"), element("p", "填写已有定时任务编号，分别设置查询时间与推送条件。"));
  const rule = field(proactive, "定时任务编号");
  const taskKind = choice(proactive, "任务类型", [["", "请选择，旧任务不自动判断"], ["reminder", "提醒：发送既有事项"], ["monitor", "资讯监测：每次重新查询"]]);
  const expression = field(proactive, "查询时间规则（五项，留空保持原值）", "例如 */30 * * * *");
  const timezone = field(proactive, "时区", "Asia/Shanghai");
  timezone.value = "Asia/Shanghai";
  const windows = field(proactive, "允许推送时段", "例如 08:00-12:00,14:00-22:00；留空不限");
  const maximum = field(proactive, "最多推送次数（留空不限，1表示一次）", "", "number");
  maximum.min = "0";
  const interval = field(proactive, "两次推送最短间隔（秒）", "0", "number");
  interval.min = "0";
  const policyExpiry = field(proactive, "停止日期（留空不限）", "", "datetime-local");
  const enabled = field(proactive, "启用规则", "", "checkbox");
  enabled.checked = true;
  const onlyUpdates = field(proactive, "仅有更新时通知", "", "checkbox");
  onlyUpdates.checked = true;
  const stopped = field(proactive, "停止推送", "", "checkbox");
  const policyStatus = element("p");
  proactive.append(button("读取规则", async () => {
    const data = await call(`/tasks/proactive-policy?${query({ rule_id: rule.value.trim() })}`);
    const config = data.policy?.config || data.defaults;
    taskKind.value = data.task_kind || "";
    expression.value = "";
    expression.placeholder = data.cron_expression || "一次性任务";
    expression.disabled = data.run_once === true;
    timezone.value = config.timezone;
    windows.value = (config.windows || []).map(pair => pair.join("-")).join(",");
    maximum.value = config.max_deliveries ?? "";
    interval.value = config.min_interval_seconds;
    policyExpiry.value = "";
    if (config.expires_at != null) {
      const local = new Date(config.expires_at * 1000);
      policyExpiry.value = new Date(local.getTime() - local.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    }
    enabled.checked = config.enabled;
    onlyUpdates.checked = config.only_updates;
    stopped.checked = config.stopped;
    policyStatus.textContent = `已推送 ${data.policy?.sent_count || 0} 次；${data.policy?.state === "unknown_result" ? "上次投递结果待核实" : "规则已读取"}`;
  }), button("保存推送规则", async () => {
    const config = { timezone: timezone.value.trim(), windows: windows.value.split(/[,，]/).map(value => value.trim()).filter(Boolean).map(value => value.split("-").map(time => time.trim())), max_deliveries: maximum.value === "" ? null : Number(maximum.value), min_interval_seconds: Number(interval.value || 0), expires_at: policyExpiry.value ? new Date(policyExpiry.value).getTime() / 1000 : null, enabled: enabled.checked, only_updates: onlyUpdates.checked, stopped: stopped.checked };
    if (!["reminder", "monitor"].includes(taskKind.value)) throw new Error("请明确选择提醒或资讯监测");
    const body = { ...scope(), rule_id: rule.value.trim(), task_kind: taskKind.value, config };
    if (expression.value.trim()) body.cron_expression = expression.value.trim();
    await call("/tasks/proactive-policy/update", body);
    showToast?.("推送规则已保存", "success");
  }), policyStatus);
  section.append(proactive);
  const sharedTools = element("details");
  sharedTools.append(element("summary", "全局共享工具文件夹"), element("p", "所有机器人和获授权用户共用。上传只保存文件；模型读取后仍须按任务权限执行。"));
  const toolDirectory = element("p");
  const toolFiles = element("div");
  const uploadFile = field(sharedTools, "上传脚本或辅助文件（单文件最大8MB）", "", "file");
  const replaceLabel = element("label", "允许覆盖同名文件");
  const replaceFile = element("input");
  replaceFile.type = "checkbox";
  replaceLabel.prepend(replaceFile);
  sharedTools.append(replaceLabel);
  async function loadTools() {
    const data = await call("/tasks/tools");
    toolDirectory.textContent = data.directory || "";
    toolFiles.replaceChildren(...(data.files || []).map(file => element("p", `${file.name}（${file.size} 字节）`)));
  }
  sharedTools.append(button("查看共享工具", loadTools), button("上传工具", async () => {
    const file = uploadFile.files?.[0];
    if (!file) throw new Error("请先选择文件");
    if (file.size > 8 * 1024 * 1024) throw new Error("单个文件不能超过8MB");
    const content = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file);
    });
    await call("/tasks/tools/upload", { name: file.name, content_base64: content, replace: replaceFile.checked });
    showToast?.("工具文件已保存，未执行", "success");
    uploadFile.value = "";
    await loadTools();
  }), toolDirectory, toolFiles);
  section.append(sharedTools);
  root.replaceChildren(section);
  return { refresh: load, destroy() { ++sequence; ++detailSequence; root.replaceChildren(); } };
}
window.PrivateCompanionTasksPanel = { mount: mountTasksPanel };
})();
