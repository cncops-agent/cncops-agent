/* 应用主控：轮询实时数据、绑定交互、诊断、演示模式、视图切换。
   铁律：写死的只有"界面骨架"，数值一律来自后端；接口异常时显示连接中断而非旧数据。 */

const POLL_MS = 2000;      // 设备区刷新周期（2~5 秒要求）

const App = {
  line: null,
  report: null,
  selected: null,
  connected: true,
  demoTimer: null,
  filter: "all",          // 列表筛选：all / alarm / warn / normal（刷新后保持）
  keyword: "",            // 搜索关键字（刷新后保持）
  sparks: {},             // 每设备的趋势序列（摘要行火花线与抽屉同源）
  drawer: null,           // 已打开的抽屉设备 id（刷新后保持打开并同步数据）
  downtimeTimers: {},     // 停机自动计时：device_id -> {startMs, stopped, stopMs}
  checks: {},             // 本次诊断的步骤勾选：服务端 checklist 为唯一真相，不读浏览器本地缓存

  /* ---------------- 启动 ---------------- */
  init() {
    // 旧版本曾把操作人/勾选存进 localStorage，换演示库后 id 重号会串数据；
    // 启动时清掉这些遗留键（仅本项目自己的键，之后不再写入）。
    try {
      const stale = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k === "cncops:operator" || (k && k.startsWith("cncops:checks:"))) stale.push(k);
      }
      stale.forEach(k => localStorage.removeItem(k));
    } catch (_) { /* 隐私模式下忽略 */ }
    this.bind();
    this.tickClock();
    setInterval(() => this.tickClock(), 1000);
    this.poll();
    setInterval(() => this.poll(), POLL_MS);
    this.initVoice();
    // 演示模式开关：默认关闭，且面板在 DOM 中不渲染
    UI.el("demoPanel").hidden = true;
    // 工厂筛选器数据（经验沉淀视图用）；失败不阻塞主流程
    API.factories().then(f => InsightsView.fillFactories(f.items || []))
      .catch(() => { /* 工厂列表不可用时筛选器只有"全部"，不影响其它视图 */ });
  },

  tickClock() {
    UI.el("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    this.updateDowntimeReadout();   // 停机计时读数每秒刷新
  },

  /* ---------------- 实时数据轮询 ---------------- */
  async poll() {
    try {
      const line = await API.line();
      this.line = line;
      // 摘要行火花线与抽屉趋势同源同周期：并行拉各设备最近 24 个采样点
      const ids = line.devices.map(d => d.id);
      const series = await Promise.all(ids.map(id => API.trend(id, 24).catch(() => null)));
      this.sparks = {};
      ids.forEach((id, i) => { if (series[i]) this.sparks[id] = series[i].series || {}; });

      if (!this.connected) { this.connected = true; UI.el("connBanner").hidden = true; }
      UI.renderTopbar(line, true);
      UI.renderListSummary(line, true);
      UI.renderDeviceRows(line, this.sparks, this.selected, this.filter, this.keyword);
      this.bindDeviceRows();
      this.syncDowntimeTimers(line);
      if (this.drawer) this.refreshDrawer();     // 抽屉打开期间同步刷新，不显示静态旧数据
      this.afterRenderChecks();
      HistoryView.fillDevices(ids);
      this.refreshDemoState(line);
    } catch (err) {
      this.connected = false;
      UI.el("connBanner").hidden = false;
      UI.el("connBanner").textContent =
        `⚠ 数据连接中断：${err.message}。已停止显示实时数据（不显示过期数值），正在重试…`;
      UI.renderTopbar(null, false);
      UI.renderListSummary(null, false);
      UI.renderDeviceRows(null, null, null, null, null);   // 数值清空，不假装正常
      if (this.drawer) UI.renderDrawerInterrupted(err.message);
    }
  },

  bindDeviceRows() {
    document.querySelectorAll(".dev-row").forEach(row => {
      if (row.dataset.bound) return;
      row.dataset.bound = "1";
      row.addEventListener("click", () => {
        this.selected = row.dataset.device;
        if (this.line) UI.renderDeviceRows(this.line, this.sparks, this.selected, this.filter, this.keyword);
        this.openDrawer(row.dataset.device);
      });
      row.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); row.click(); }
      });
    });
  },

  /* ---------------- 详情抽屉：滑出/关闭/刷新 ---------------- */
  async openDrawer(deviceId) {
    this.drawer = deviceId;
    UI.showDrawer();
    await this.refreshDrawer();
  },

  async refreshDrawer() {
    if (!this.drawer) return;
    const id = this.drawer;
    try {
      const [detail, hist] = await Promise.all([
        API.device(id),
        API.history({ device: id, limit: 5 }),
      ]);
      if (this.drawer !== id) return;            // 刷新期间已切换/关闭：丢弃过期响应
      UI.renderDrawer(detail, hist.items || []);
    } catch (err) {
      if (this.drawer !== id) return;
      UI.renderDrawerInterrupted(err.message);
    }
  },

  closeDrawer() {
    this.drawer = null;
    UI.hideDrawer();
  },

  /* ---------------- 停机自动计时 ----------------
     规则：设备一报警就开始计时（起点取后端报警事件 alarm_since，刷新页面可续计时），
     报警消失后继续走表，直到提交反馈（确认解决）才停止；时长计入运维记录。
     注意：计时中的表不因"报警消失又出现"而重启——设备从报警起停线，到确认解决复产；
     只有已停表的设备再次报警（新故障）才起新表。 */
  syncDowntimeTimers(line) {
    if (!line || !line.devices) return;
    for (const card of line.devices) {
      if (!card.alarm_since) continue;
      const startMs = Date.parse(card.alarm_since);
      if (Number.isNaN(startMs)) continue;
      const cur = this.downtimeTimers[card.id];
      if (!cur || (cur.stopped && startMs > cur.startMs)) {
        this.downtimeTimers[card.id] = { startMs, stopped: false, stopMs: 0 };
      }
    }
    this.updateDowntimeReadout();
  },

  /* 报告证据里的越限起点（cross_time）作为兜底计时起点：后端事件缺失时也能自动计时 */
  startDowntimeTimerFromReport(report) {
    const devId = report && report.device && report.device.id;
    if (!devId || this.downtimeTimers[devId]) return;
    const times = (report.evidence || [])
      .filter(ev => ev.severity === "alarm" && ev.cross_time)
      .map(ev => Date.parse(ev.cross_time))
      .filter(t => !Number.isNaN(t));
    if (!times.length) return;
    this.downtimeTimers[devId] = { startMs: Math.min(...times), stopped: false, stopMs: 0 };
  },

  downtimeMinutes(deviceId) {
    const t = this.downtimeTimers[deviceId];
    if (!t) return null;
    const end = t.stopped ? t.stopMs : Date.now();
    return Math.max(0, Math.round((end - t.startMs) / 60000));
  },

  stopDowntimeTimer(deviceId) {
    const t = this.downtimeTimers[deviceId];
    if (t && !t.stopped) { t.stopped = true; t.stopMs = Date.now(); }
  },

  updateDowntimeReadout() {
    const box = UI.el("fbDowntimeAuto");
    if (!box) return;
    const devId = this.report && this.report.device && this.report.device.id;
    const t = devId ? this.downtimeTimers[devId] : null;
    if (!t) {
      box.textContent = "⏱ 未检测到本次报警起点（设备报警后自动计时，也可手动填写）";
      box.classList.remove("running");
      return;
    }
    const min = this.downtimeMinutes(devId);
    const start = new Date(t.startMs).toLocaleTimeString("zh-CN", { hour12: false });
    box.textContent = t.stopped
      ? `⏱ 停机计时已停止：${min} 分钟（自 ${start} 报警起，已计入记录）`
      : `⏱ 停机自动计时中：${min} 分钟（自 ${start} 报警起，确认解决时停止）`;
    box.classList.toggle("running", !t.stopped);
  },

  /* ---------------- 交互绑定 ---------------- */
  bind() {
    UI.el("askBtn").addEventListener("click", () => this.ask());
    UI.el("queryInput").addEventListener("keydown", e => { if (e.key === "Enter") this.ask(); });

    document.querySelectorAll(".chip").forEach(chip => {
      chip.addEventListener("click", () => {
        const input = UI.el("queryInput");
        const word = chip.dataset.word;
        input.value = input.value.trim() ? `${input.value.trim()}，${word}` : this.compose(word);
        input.focus();
      });
    });

    // 视图切换
    UI.el("navDashboard").addEventListener("click", () => this.switchView("dashboard"));
    UI.el("navHistory").addEventListener("click", () => this.switchView("history"));
    UI.el("navInsights").addEventListener("click", () => this.switchView("insights"));

    // 经验沉淀筛选与导出
    UI.el("insightRefresh").addEventListener("click", () => InsightsView.refresh());
    ["insightFactory", "insightDays"].forEach(id =>
      UI.el(id).addEventListener("change", () => InsightsView.refresh()));
    UI.el("exportCsvInsight").addEventListener("click", () => this.exportRecords("csv", "insight"));
    UI.el("exportXlsxInsight").addEventListener("click", () => this.exportRecords("xlsx", "insight"));
    UI.el("exportCsvHistory").addEventListener("click", () => this.exportRecords("csv", "history"));
    UI.el("exportXlsxHistory").addEventListener("click", () => this.exportRecords("xlsx", "history"));

    // 演示模式开关
    UI.el("demoToggle").addEventListener("change", async e => {
      const on = e.target.checked;
      UI.el("demoPanel").hidden = !on;
      if (on) await this.loadDemoCatalog();
    });
    UI.el("demoDevice").addEventListener("change", () => this.fillDemoFaults());
    UI.el("demoInject").addEventListener("click", () => this.inject());
    UI.el("demoRecover").addEventListener("click", async () => {
      try { const r = await API.demoClear(); UI.toast(r.message); this.poll(); }
      catch (err) { UI.toast(`恢复失败：${err.message}`, true); }
    });

    // 弹层关闭
    UI.el("kbModalClose").addEventListener("click", () => { UI.el("kbModal").hidden = true; });
    UI.el("kbModal").addEventListener("click", e => { if (e.target.id === "kbModal") e.currentTarget.hidden = true; });

    // 设备列表筛选条（数量角标实时随数据变化；筛选状态刷新后保持）
    UI.el("filterChips").addEventListener("click", e => {
      const btn = e.target.closest(".fchip");
      if (!btn) return;
      this.filter = btn.dataset.filter;
      document.querySelectorAll(".fchip").forEach(b => b.classList.toggle("active", b === btn));
      if (this.line) UI.renderDeviceRows(this.line, this.sparks, this.selected, this.filter, this.keyword);
      this.bindDeviceRows();
    });
    // 搜索框：按设备编号或名称过滤
    UI.el("deviceSearch").addEventListener("input", e => {
      this.keyword = e.target.value.trim();
      if (this.line) UI.renderDeviceRows(this.line, this.sparks, this.selected, this.filter, this.keyword);
      this.bindDeviceRows();
    });

    // 详情抽屉：关闭按钮 / 点遮罩 / Esc 三种方式
    UI.el("drawerClose").addEventListener("click", () => this.closeDrawer());
    UI.el("drawerMask").addEventListener("click", () => this.closeDrawer());
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && this.drawer) this.closeDrawer();
    });
    // 「对该设备提问诊断」：把设备名自动带入诊断输入框
    UI.el("drawerAsk").addEventListener("click", () => {
      if (!this.drawer) return;
      const dev = this.line && this.line.devices.find(d => d.id === this.drawer);
      const input = UI.el("queryInput");
      input.value = dev ? `${dev.name} ` : `${this.drawer} `;
      input.focus();
      this.closeDrawer();
    });

    // 排查模式按钮
    UI.el("stepPrev").addEventListener("click", () => StepMode.prev());
    UI.el("stepNext").addEventListener("click", () => StepMode.next());
    UI.el("stepExit").addEventListener("click", () => { StepMode.stop(); this.guideFeedback(); });
    UI.el("safeCancel").addEventListener("click", () => { UI.el("safeModal").hidden = true; StepMode.prev(); });
    // L3 确认：勾选声明 + 确认人署名，两者齐备才允许提交（服务端同样强制校验）
    const syncSafeBtn = () => {
      UI.el("safeConfirm").disabled = !(UI.el("safeAck").checked && UI.el("safeAckBy").value.trim());
    };
    UI.el("safeAck").addEventListener("change", syncSafeBtn);
    UI.el("safeAckBy").addEventListener("input", syncSafeBtn);
    UI.el("safeConfirm").addEventListener("click", () => StepMode.confirmSafe());

    // 历史筛选
    UI.el("historyRefresh").addEventListener("click", () => HistoryView.refresh());
    ["historyDevice", "historyResult"].forEach(id =>
      UI.el(id).addEventListener("change", () => HistoryView.refresh()));
    UI.el("historyKeyword").addEventListener("keydown", e => {
      if (e.key === "Enter") HistoryView.refresh();
    });
    UI.el("historyList").addEventListener("click", e => {
      const t = e.target.closest("[data-toggle]");
      if (t) HistoryView.toggle(t.dataset.toggle);
    });
  },

  compose(word) {
    const dev = this.selected || this.defaultDevice();
    const name = dev ? `${dev} ` : "";
    return `${name}${word}`;
  },

  defaultDevice() {
    if (!this.line) return "";
    const alarm = this.line.devices.find(d => d.severity === "alarm");
    return alarm ? alarm.id : "";
  },

  switchView(view) {
    const isDash = view === "dashboard";
    UI.el("viewDashboard").hidden = !isDash;
    UI.el("viewHistory").hidden = view !== "history";
    UI.el("viewInsights").hidden = view !== "insights";
    UI.el("navDashboard").classList.toggle("active", isDash);
    UI.el("navHistory").classList.toggle("active", view === "history");
    UI.el("navInsights").classList.toggle("active", view === "insights");
    if (view === "history") HistoryView.refresh();
    if (view === "insights") InsightsView.refresh();
  },

  /* 导出：带上当前视图的筛选条件，浏览器直接下载（CSV=utf-8-sig / XLSX 两张表） */
  exportRecords(fmt, scope) {
    let params = {};
    if (scope === "insight") {
      params = { ...InsightsView.filters() };
    } else {
      params = {
        device: UI.el("historyDevice").value,
        result: UI.el("historyResult").value,
        q: UI.el("historyKeyword").value.trim(),
      };
    }
    window.open(API.exportUrl(fmt, params), "_blank");
  },

  /* ---------------- 诊断 ---------------- */
  async ask() {
    const input = UI.el("queryInput");
    const query = input.value.trim();
    if (!query) { UI.toast("先说说哪台机器怎么了", true); input.focus(); return; }
    UI.el("askBtn").disabled = true;
    UI.el("reportArea").innerHTML =
      `<div class="empty-state"><div class="empty-text">正在取数据、查资料库…</div></div>`;
    try {
      const report = await API.diagnose(query, this.selected);
      this.report = report;
      this.checks = {};             // 新一轮诊断一律从「零勾选」开始
      this.selected = report.device.id;
      UI.renderReport(report);
      // 报告证据含越限起点：作为停机自动计时的兜底起点（后端事件缺失时也能计时）
      this.startDowntimeTimerFromReport(report);
      this.updateDowntimeReadout();
      // 操作人必须由本人填写：不回填、不记忆，避免换演示环境后带出上一位署名
      await this.loadChecks(report);
      this.afterRenderChecks();
      this.bindReport();
      await this.poll();
    } catch (err) {
      UI.el("reportArea").innerHTML =
        `<div class="empty-state"><div class="empty-text">诊断失败：${UI.escape(err.message)}</div></div>`;
      UI.toast(`诊断失败：${err.message}`, true);
    } finally {
      UI.el("askBtn").disabled = false;
    }
  },

  bindReport() {
    const kb = UI.el("kbBadge");
    if (kb) kb.addEventListener("click", () => UI.openKb(kb.dataset.kb));

    document.querySelectorAll("[data-step]").forEach(box => {
      box.addEventListener("change", async () => {
        // L3 危险步骤：报告卡里不能直接勾选完成，必须先走排查模式的安全确认并署名
        const step = (this.report.steps || []).find(s => s.key === box.dataset.step);
        if (box.checked && step && step.level === "L3" && !step.ack) {
          box.checked = false;
          UI.toast("L3 危险步骤需先在「开始排查」中由持证人员确认 LOTO 并署名", true);
          return;
        }
        const before = { ...(this.checks || {}) };
        const map = { ...before, [box.dataset.step]: box.checked };
        this.checks = map;
        this.applyChecks(this.report);
        try {
          await API.saveChecklist(this.report.record_id, map);
          // 成功即保留本次 map：并发勾选时不用服务端快照覆盖后一次操作
        } catch (err) {
          this.checks = before;   // 保存失败就回滚，不假装已完成
          UI.toast(`勾选保存失败：${err.message}`, true);
        }
        this.applyChecks(this.report);
      });
    });

    UI.el("fbOk").addEventListener("click", () => this.sendFeedback(true));
    UI.el("fbNg").addEventListener("click", () => this.sendFeedback(false));
    UI.el("startStepMode").addEventListener("click", () => StepMode.start(this.report));

    document.querySelectorAll("[data-history]").forEach(el => {
      el.addEventListener("click", async () => {
        this.switchView("history");
        const id = el.dataset.history;
        await HistoryView.refresh();
        await HistoryView.toggle(id);
        const row = document.querySelector(`.hist-item[data-id="${id}"]`);
        if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
  },

  applyChecks(report) {
    if (!report) return;
    const map = this.checks || {};
    // 未经服务端确认的 L3 步骤，即使本地有旧勾选缓存也不显示为已完成
    const l3Locked = new Set((report.steps || [])
      .filter(s => s.level === "L3" && !s.ack).map(s => s.key));
    document.querySelectorAll("[data-step]").forEach(box => {
      const done = !!map[box.dataset.step] && !l3Locked.has(box.dataset.step);
      box.checked = done;
      const item = box.closest(".step-item");
      if (item) item.classList.toggle("done", done);
    });
  },

  async loadChecks(report) {
    // 服务端是勾选状态的唯一真相：绝不与浏览器本地旧缓存取并集。
    // 绿色包自带新库、诊断 id 会重号，本地缓存会把上一轮的勾选错位带进新报告。
    try {
      const data = await API.checklist(report.record_id);
      this.checks = { ...(data.checks || {}) };
    } catch (_) {
      this.checks = {};   // 接口不可用时不臆造已完成状态
    }
    this.applyChecks(report);
  },

  afterRenderChecks() { if (this.report) this.applyChecks(this.report); },

  async sendFeedback(resolved) {
    if (!this.report) return;
    // 操作人必填：空姓名不允许提交（后端同样强制，双保险）
    const operator = UI.el("fbOperator").value.trim();
    if (!operator) {
      UI.toast("请填写操作人姓名/工号（必填）", true);
      UI.el("fbOperator").focus();
      return;
    }
    // 操作人只随本次反馈落库，不写入浏览器本地存储：下一轮仍须本人重填
    const note = UI.el("fbNote").value.trim();
    const handlingRaw = UI.el("fbHandling").value.trim();
    const downtimeRaw = UI.el("fbDowntime").value.trim();
    const toInt = v => (v === "" ? null : Math.max(0, parseInt(v, 10) || 0));
    // 停机时长：留空则用自动计时值（报警起点 → 现在）；手动填报优先
    let downtimeMin = toInt(downtimeRaw);
    if (downtimeMin == null) {
      const auto = this.downtimeMinutes(this.report.device.id);
      if (auto != null) downtimeMin = auto;
    }
    try {
      const r = await API.feedback(this.report.record_id, resolved, note, {
        operator,
        handlingMin: toInt(handlingRaw),
        downtimeMin,
      });
      // 确认解决 → 停表；停机时长以服务端最终入库值为准回显
      this.stopDowntimeTimer(this.report.device.id);
      const shown = r.downtime_min != null ? r.downtime_min : downtimeMin;
      const autoTag = r.downtime_auto ? "（自动计时）" : "";
      UI.el("fbDone").textContent = shown != null
        ? `${r.message}（记录 #${this.report.record_id}，停机 ${shown} 分钟${autoTag}，历史页可查到）`
        : `${r.message}（记录 #${this.report.record_id}，历史页可查到）`;
      UI.toast(r.message);
      HistoryView.detailCache = {};    // 失效缓存，历史页立刻能看到
      this.updateDowntimeReadout();
    } catch (err) {
      UI.toast(`反馈失败：${err.message}`, true);
    }
  },

  guideFeedback() {
    const box = UI.el("feedbackBox");
    if (box) box.scrollIntoView({ behavior: "smooth", block: "center" });
    UI.toast("排查已结束：请点击「已解决」或「没解决」完成反馈");
  },

  /* ---------------- 演示模式 ---------------- */
  async loadDemoCatalog() {
    try {
      const cat = await API.demoCatalog();
      const devSel = UI.el("demoDevice");
      devSel.innerHTML = cat.devices.map(d =>
        `<option value="${d.device_id}">${d.device_name}（${d.device_id}）</option>`).join("");
      this._demoCatalog = cat;
      this.fillDemoFaults();
      this.el0 = true;
    } catch (err) {
      UI.el("demoPanel").hidden = true;
      UI.toast(`演示模式不可用：${err.message}`, true);
    }
  },

  fillDemoFaults() {
    const cat = this._demoCatalog;
    if (!cat) return;
    const devId = UI.el("demoDevice").value;
    const dev = cat.devices.find(d => d.device_id === devId);
    UI.el("demoFault").innerHTML = (dev ? dev.faults : []).map(f =>
      `<option value="${f.id}">${f.name}（${f.id}）</option>`).join("");
  },

  async inject() {
    const deviceId = UI.el("demoDevice").value;
    const faultId = UI.el("demoFault").value;
    try {
      const r = await API.demoInject(deviceId, faultId);
      // 注入新故障 = 新一轮演示：清掉上一份报告、勾选和排查界面，避免看到上轮残留
      this.report = null;
      this.checks = {};
      StepMode.stop();
      UI.el("safeModal").hidden = true;
      UI.el("reportArea").innerHTML =
        `<div class="empty-state"><div class="empty-icon">🛠</div>
         <div class="empty-text">故障已注入，请描述现象后点「诊断」，开始新一轮排查。</div></div>`;
      UI.toast(r.message);
      await this.poll();
    } catch (err) {
      UI.toast(`注入失败：${err.message}`, true);
    }
  },

  refreshDemoState(line) {
    if (UI.el("demoPanel").hidden) return;
    const act = line.active_faults || {};
    const keys = Object.keys(act);
    UI.el("demoState").textContent = keys.length
      ? `当前注入：${keys.map(k => `${k} → ${act[k]}`).join("；")}`
      : "当前无注入";
  },

  /* ---------------- 语音输入（不支持则隐藏按钮） ---------------- */
  initVoice() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btn = UI.el("micBtn");
    if (!SR) { btn.hidden = true; return; }     // 浏览器不支持：优雅降级，不报错
    const rec = new SR();
    rec.lang = "zh-CN";
    rec.interimResults = false;
    rec.continuous = false;
    btn.hidden = false;
    let recording = false;
    btn.addEventListener("click", () => {
      if (recording) { rec.stop(); return; }
      try { rec.start(); } catch (_) { /* 已在录音时忽略 */ }
    });
    rec.onstart = () => { recording = true; btn.classList.add("recording"); UI.el("micHint").hidden = false; };
    rec.onend = () => { recording = false; btn.classList.remove("recording"); UI.el("micHint").hidden = true; };
    rec.onerror = e => { UI.toast(`语音输入失败：${e.error || "未知错误"}`, true); };
    rec.onresult = e => {
      const text = Array.from(e.results).map(r => r[0].transcript).join("");
      const input = UI.el("queryInput");
      input.value = input.value.trim() ? `${input.value.trim()}${text}` : text;
      input.focus();
    };
  },
};

document.addEventListener("DOMContentLoaded", () => App.init());
