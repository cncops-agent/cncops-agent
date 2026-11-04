/* 界面渲染：设备摘要行、报告卡、证据迷你趋势、步骤清单、排查模式。
   约定：任何状态都必须同时具备"颜色 + 图标 + 文字"，不单独依赖颜色传达信息。 */

const UI = {
  el(id) { return document.getElementById(id); },

  escape(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  toast(msg, isError = false) {
    const box = this.el("toast");
    box.textContent = msg;
    box.className = isError ? "toast error" : "toast";
    box.hidden = false;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { box.hidden = true; }, 3600);
  },

  fmtTime(iso) {
    if (!iso) return "未知";
    const t = String(iso).replace("T", " ");
    return t.length > 19 ? t.slice(0, 19) : t;
  },

  severityClass(sev) {
    return { normal: "sev-normal", warn: "sev-warn", alarm: "sev-alarm", offline: "sev-offline" }[sev] || "sev-idle";
  },
  severityIcon(sev) {
    return { normal: "🟢", warn: "🟡", alarm: "🔴", offline: "⚫" }[sev] || "⚪";
  },

  /* ---------------- 顶部状态条 ---------------- */
  renderTopbar(line, connected) {
    const bar = this.el("topbar");
    const summary = this.el("lineSummary");
    if (!connected || !line) {
      bar.dataset.worst = "offline";
      summary.textContent = "数据连接中断：无法获取产线状态";
      return;
    }
    bar.dataset.worst = line.summary.worst;
    summary.textContent = `产线状态：${line.summary.sentence}`;
  },

  /* ---------------- 左侧紧凑摘要行列表 ----------------
     一行一台设备：状态点+状态文字 / 编号+名称 / 唯一关键监测值（超标红色+上限标注）/
     迷你趋势线 / 未处理报警数角标。行高 60px、字号 ≥14px，设备再多也不缩放不分页。 */
  renderDeviceRows(line, sparks, selectedId, filter, keyword) {
    const host = this.el("deviceList");
    if (!line) {
      // 连接中断：只保留设备骨架，数值一律显示"--"，绝不显示过期数值
      const skel = this._deviceIds || [];
      host.innerHTML = skel.length
        ? skel.map(d => `
          <div class="dev-row sev-offline" data-device="${this.escape(d.id)}">
            <span class="dr-dot sev-offline-dot"></span>
            <span class="dr-status st-idle">中断</span>
            <span class="dr-name"><b>${this.escape(d.id)}</b>${this.escape(d.name)}</span>
            <span class="dr-key num-idle">--</span>
            <span class="dr-spark"></span>
          </div>`).join("")
        : `<div class="dw-empty">正在连接数据…</div>`;
      return;
    }
    this._deviceIds = line.devices.map(d => ({ id: d.id, name: d.name }));

    // 排序：报警 > 预警 > 离线 > 正常，同级按设备编号；每次刷新实时重排
    const rank = { alarm: 0, warn: 1, offline: 2, normal: 3 };
    let rows = [...line.devices].sort((a, b) =>
      (rank[a.severity] == null ? 3 : rank[a.severity]) - (rank[b.severity] == null ? 3 : rank[b.severity])
      || String(a.id).localeCompare(String(b.id)));
    if (filter && filter !== "all") rows = rows.filter(r => r.severity === filter);
    const kw = (keyword || "").trim().toLowerCase();
    if (kw) {
      rows = rows.filter(r => String(r.id).toLowerCase().includes(kw)
        || String(r.name || "").toLowerCase().includes(kw));
    }
    host.innerHTML = rows.length
      ? rows.map(c => this._rowHtml(c, (sparks || {})[c.id], selectedId)).join("")
      : `<div class="dw-empty">没有符合筛选条件的设备</div>`;
  },

  _rowHtml(card, seriesMap, selectedId) {
    const sev = card.severity || "normal";
    const dotCls = { normal: "sev-normal-dot", warn: "sev-warn-dot", alarm: "sev-alarm-dot", offline: "sev-offline-dot" }[sev] || "sev-offline-dot";
    const stCls = { normal: "st-ok", warn: "st-warn", alarm: "st-alarm", offline: "st-idle" }[sev] || "st-idle";
    const stText = { normal: "正常", warn: "预警", alarm: "报警", offline: "中断" }[sev] || "中断";
    const m = (card.key_metrics || [])[0] || null;

    let keyHtml = `<span class="dr-key num-idle">--</span>`;
    let sparkHtml = `<span class="dr-spark"></span>`;
    if (m) {
      const numCls = m.severity === "alarm" ? "num-alarm" : m.severity === "warn" ? "num-warn" : "";
      const note = this.thresholdNote(m);
      keyHtml = `<span class="dr-key ${numCls}">${m.value}<span class="dr-unit">${this.escape(m.unit || "")}</span>`
        + (note ? `<span class="dr-limit">${this.escape(note)}</span>` : "") + `</span>`;
      // 迷你趋势线与抽屉同源（/api/trend 的 TREND 缓冲），阈值线用报警档
      const series = seriesMap ? seriesMap[m.key] : null;
      const thr = m.direction === "deviation" ? null : m.alarm;
      const lineColor = sev === "alarm" ? "#e5372c" : sev === "warn" ? "#e3b505" : "#4aa3ff";
      sparkHtml = `<span class="dr-spark">${this.sparkline(series, thr, { w: 76, h: 40, color: lineColor })}</span>`;
    }
    const alarmN = (card.alarm_codes || []).length;
    const badge = alarmN > 0
      ? `<span class="dr-badge" title="当前未处理报警 ${alarmN} 起">${alarmN}</span>` : "";
    return `
    <div class="dev-row sev-${sev}${card.id === selectedId ? " selected" : ""}" data-device="${this.escape(card.id)}"
         role="option" tabindex="0" aria-selected="${card.id === selectedId ? "true" : "false"}">
      <span class="dr-dot ${dotCls}"></span>
      <span class="dr-status ${stCls}">${stText}</span>
      <span class="dr-name"><b>${this.escape(card.id)}</b>${this.escape(card.name)}</span>
      ${keyHtml}
      ${sparkHtml}
      ${badge}
    </div>`;
  },

  /* 关键值旁的阈值标注：超标给上限/下限，预警给注意值，正常不标注 */
  thresholdNote(m) {
    if (m.direction === "deviation") {
      if (m.severity === "normal" || m.alarm == null) return "";
      return `偏差 ${(Number(m.alarm) * 100).toFixed(0)}%`;
    }
    const limit = m.severity === "alarm" ? m.alarm : m.severity === "warn" ? m.warn : null;
    if (limit == null) return "";
    const bound = m.direction === "below" ? "下限" : "上限";
    return m.severity === "alarm" ? `${bound} ${limit}${m.unit || ""}` : `注意 ${limit}${m.unit || ""}`;
  },

  /* 列表头摘要 + 筛选条数量角标 */
  renderListSummary(line, connected) {
    const box = this.el("listSummary");
    if (!connected || !line) { box.textContent = "数据连接中断：无法获取设备状态"; return; }
    const s = line.summary;
    box.textContent = `共 ${s.total} 台 · 报警 ${s.alarm} · 预警 ${s.warn} · 正常 ${s.normal}`;
    const c = { all: s.total, alarm: s.alarm, warn: s.warn, normal: s.normal };
    this.el("cntAll").textContent = c.all;
    this.el("cntAlarm").textContent = c.alarm;
    this.el("cntWarn").textContent = c.warn;
    this.el("cntNormal").textContent = c.normal;
  },

  /* ---------------- 设备详情抽屉 ---------------- */
  showDrawer() {
    const mask = this.el("drawerMask"), drawer = this.el("deviceDrawer");
    mask.hidden = false; drawer.hidden = false;
    void drawer.offsetWidth;                    // 强制回流，保证滑入过渡生效
    mask.classList.add("open"); drawer.classList.add("open");
    document.body.classList.add("drawer-open"); // 锁定底层滚动（手机端尤其重要）
    drawer.setAttribute("aria-hidden", "false");
    this.el("drawerBody").innerHTML = `<div class="dw-empty">正在读取设备全部监测点…</div>`;
    this.el("drawerClose").focus();
  },

  hideDrawer() {
    const mask = this.el("drawerMask"), drawer = this.el("deviceDrawer");
    mask.classList.remove("open"); drawer.classList.remove("open");
    document.body.classList.remove("drawer-open");
    drawer.setAttribute("aria-hidden", "true");
    clearTimeout(this._drawerHide);
    this._drawerHide = setTimeout(() => { mask.hidden = true; drawer.hidden = true; }, 320);
  },

  renderDrawer(detail, history) {
    const dev = detail.device || {};
    const statusMeta = { RUN: ["正常", "st-ok", "sev-normal-dot"], WARN: ["预警", "st-warn", "sev-warn-dot"], ALARM: ["报警", "st-alarm", "sev-alarm-dot"] };
    const [stText, stCls, dotCls] = statusMeta[dev.status] || ["中断", "st-idle", "sev-offline-dot"];
    this.el("drawerDevId").textContent = dev.id || "";
    this.el("drawerDevName").textContent = dev.name || "";
    const st = this.el("drawerStatus");
    st.textContent = stText; st.className = `drawer-status ${stCls}`;
    this.el("drawerDot").className = `dr-dot ${dotCls}`;
    this.el("drawerSub").textContent =
      `${dev.location || ""} ｜ 更新于 ${this.fmtTime(dev.update_time)} ｜ 数据来源：${dev.data_source || ""}`;

    const body = this.el("drawerBody");
    const keepScroll = body.scrollTop;          // 刷新不打断用户滚动位置

    const alarms = (detail.alarm_codes || []).length
      ? `<div class="dw-sec"><div class="dw-sec-title">当前报警（${detail.alarm_codes.length}）</div>
         ${(detail.alarm_messages || []).map(x => `<div class="alarm-line">🔴 ${this.escape(x)}</div>`).join("")}</div>` : "";

    const points = detail.points.map(p => {
      const cls = p.severity === "alarm" ? "p-alarm" : p.severity === "warn" ? "p-warn" : "";
      return `<div class="dw-point ${cls}">
        <span class="dw-point-label">${this.escape(p.label)}</span>
        <span class="dw-point-value">${p.value}${this.escape(p.unit || "")} ${this.severityIcon(p.severity)}</span>
        <span class="dw-point-thr">${this.thresholdText(p)}</span>
      </div>`;
    }).join("");

    const trends = detail.points.map(p => {
      const series = (detail.trend || {})[p.key] || [];
      const thr = p.direction === "deviation" ? null : p.alarm;
      const color = p.severity === "alarm" ? "#e5372c" : p.severity === "warn" ? "#e3b505" : "#4aa3ff";
      return `<div class="dw-trend">
        <div class="dw-trend-label"><span>${this.escape(p.label)}</span><b>${p.value}${this.escape(p.unit || "")}</b></div>
        ${this.sparkline(series, thr, { w: 300, h: 52, color })}
      </div>`;
    }).join("");

    const hist = (history || []).length
      ? history.map(h => `
        <div class="dw-hist-item">
          <div class="dw-hist-title">${this.escape(h.fault_title || h.conclusion || "诊断记录")}<span class="dw-hist-meta"> #${h.id}</span></div>
          <div class="dw-hist-meta">${this.escape(this.fmtTime(h.created_at))} ｜ 结果：${this.escape(h.result)} ｜ 操作人：${this.escape(h.operator || "未填")}${h.comment ? " ｜ 备注：" + this.escape(h.comment) : ""}</div>
        </div>`).join("")
      : `<div class="dw-empty">该设备暂无运维历史</div>`;

    body.innerHTML = `
      ${alarms}
      <div class="dw-sec">
        <div class="dw-sec-title">全部监测点<span class="hint">实时值 vs 阈值，共 ${detail.points.length} 项</span></div>
        ${points}
      </div>
      <div class="dw-sec">
        <div class="dw-sec-title">趋势<span class="hint">各测点最近一段时间的采样曲线（虚线为报警阈值）</span></div>
        <div class="dw-trends">${trends}</div>
      </div>
      <div class="dw-sec">
        <div class="dw-sec-title">最近运维历史<span class="hint">最多 5 条</span></div>
        ${hist}
      </div>`;
    body.scrollTop = keepScroll;
  },

  renderDrawerInterrupted(message) {
    this.el("drawerBody").innerHTML =
      `<div class="dw-empty">⚠ 数据连接中断：${this.escape(message || "读取失败")}。已停止显示该设备的过期数值，正在重试…</div>`;
  },

  thresholdText(m) {
    if (m.warn == null && m.alarm == null) return "无阈值";
    const unit = m.unit || "";
    const arrow = m.direction === "below" ? "越低越危险" : "越高越危险";
    return `注意 ${m.warn}${unit} / 超标 ${m.alarm}${unit}（${arrow}）`;
  },

  /* ---------------- 迷你趋势图（火花线）---------------
     opts: { w, h, color } 控制尺寸与线色（摘要行用小尺寸，报告证据用默认尺寸） */
  sparkline(series, threshold, opts = {}) {
    const w = opts.w || 180, h = opts.h || 46, pad = 4;
    const color = opts.color || "#4aa3ff";
    if (!series || series.length < 2) {
      return `<svg class="spark" viewBox="0 0 ${w} ${h}">
        <text x="${w / 2}" y="${h / 2 + 4}" fill="#6d7d8c" font-size="11" text-anchor="middle">趋势采集中…</text></svg>`;
    }
    const vals = series.map(p => p.v);
    let min = Math.min(...vals), max = Math.max(...vals);
    if (threshold != null) { min = Math.min(min, threshold); max = Math.max(max, threshold); }
    const span = (max - min) || 1;
    const x = i => pad + (i * (w - pad * 2)) / (series.length - 1);
    const y = v => h - pad - ((v - min) / span) * (h - pad * 2);
    const pts = series.map((p, i) => `${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    const thrY = threshold == null ? null : y(threshold);
    const last = series[series.length - 1];
    return `<svg class="spark" viewBox="0 0 ${w} ${h}">
      ${thrY != null ? `<line x1="0" y1="${thrY.toFixed(1)}" x2="${w}" y2="${thrY.toFixed(1)}" stroke="#e5372c" stroke-width="1" stroke-dasharray="4 3"/>` : ""}
      <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"/>
      <circle cx="${x(series.length - 1).toFixed(1)}" cy="${y(last.v).toFixed(1)}" r="3" fill="#e8eef4"/>
    </svg>`;
  },

  /* ---------------- 报告卡 ---------------- */
  renderReport(report) {
    const host = this.el("reportArea");
    const head = report.headline;
    const uncovered = report.uncovered;
    const cls = uncovered ? "warn" : (report.evidence.length ? "" : "calm");

    const sinceText = ev => {
      if (!ev.cross_time) return "";
      const sec = ev.over_seconds;
      const when = sec >= 60 ? `${Math.round(sec / 60)} 分钟` : `${sec} 秒`;
      return `<div class="ev-since">从 ${this.escape(this.fmtTime(ev.cross_time))} 起越限，已持续约 ${when}${ev.approx ? "（采样起点近似）" : ""}</div>`;
    };
    const evidence = report.evidence.length ? report.evidence.map(ev => `
      <div class="evidence-row ${this.severityClass(ev.severity)}">
        <div>
          <div class="ev-main"><span class="ev-name">${this.escape(ev.label)}</span>
            <span class="ev-num"> ${ev.value}${this.escape(ev.unit)}</span>
            ${this.severityIcon(ev.severity)} ${this.escape(ev.severity_text || "")}</div>
          <div class="ev-compare">阈值上限/下限：<b>${ev.alarm != null ? ev.alarm : "-"}${this.escape(ev.unit)}</b>
            ｜ 实测 ${ev.value}${this.escape(ev.unit)} <span class="ev-dev">${this.escape(ev.deviation || ev.text || "")}</span></div>
          ${sinceText(ev)}
        </div>
        ${this.sparkline(ev.spark, ev.threshold_line)}
      </div>`).join("")
      : `<div class="empty-text">实时数据没有越过任何阈值。</div>`;

    const stepsHtml = ["L1", "L2", "L3"].map(level => {
      const items = report.steps_by_level[level] || [];
      if (!items.length) return "";
      const safetyBar = level === "L3" ? `
        <div class="safety-bar"><span>⚠</span><div>
          以下步骤涉及高压/带电/拆机/主轴等危险作业，必须由专业工程师执行并做断电挂牌上锁（LOTO）。
          系统未接入断电/挂锁信号，不检测设备是否真的断电；确认由现场持证人员作出并留痕。
          <ul><li>执行前：停机、断电、挂牌上锁（LOTO）、泄压、等待高温部件冷却</li>
              <li>禁止：无资质人员操作、带电拆装、未冷却触碰高温部件</li></ul></div></div>` : "";
      const rows = items.map(s => {
        const tools = s.tools.length ? s.tools.map(t => this.escape(t)).join("、") : "不需要工具";
        const mats = s.materials.length ? s.materials.map(t => this.escape(t)).join("、") : "不需要物料";
        const danger = s.danger_badge ? `<div class="step-danger">${this.escape(s.danger_badge)}</div>` : "";
        const safety = (s.safety || []).map(x => `<div class="step-danger">⚠ ${this.escape(x)}</div>`).join("");
        const ackTag = level === "L3"
          ? (s.ack
              ? `<div class="step-ack ok">🔒 安全确认已留痕：${this.escape(s.ack.ack_by)} · ${this.escape(s.ack.ack_at)}（人工确认，非系统断电检测）</div>`
              : `<div class="step-ack pending">🔒 未完成安全确认：需在排查模式中由持证人员署名后方可标记完成</div>`)
          : "";
        return `<label class="step-item" data-key="${this.escape(s.key)}">
          <input type="checkbox" data-step="${this.escape(s.key)}" />
          <div>
            <div class="step-text">${this.escape(s.text)}</div>
            <div class="step-meta">
              <span>🧰 <b>工具：</b>${tools}</span>
              <span>📦 <b>物料：</b>${mats}</span>
              <span>⏱ <b>预计耗时：</b>${s.eta_min} 分钟</span>
            </div>
            ${danger}${safety}${ackTag}
            <div class="step-src">来源：${this.escape(s.kb_code || "")} ${this.escape(s.kb_title || s.kb_id || "")}</div>
          </div>
        </label>`;
      }).join("");
      const meta = { L1: { t: "L1 · 操作工可执行", i: "🟢" }, L2: { t: "L2 · 维修工执行", i: "🟡" }, L3: { t: "L3 · 必须工程师执行", i: "🔴" } }[level];
      return `<div class="step-group ${level.toLowerCase()}">
          <div class="step-group-head">${meta.i} ${meta.t}（${items.length} 条）</div>
          ${safetyBar}${rows}</div>`;
    }).join("");

    const histRef = (report.history || []).map(h => `
      <div class="history-ref" data-history="${h.id}">
        <div class="hr-title">📌 相似历史：${this.escape(h.fault_title || "同类故障")}</div>
        <div class="hr-meta">${this.escape(this.fmtTime(h.created_at))} ｜ ${this.escape(h.device_id)} ｜ 处理结果：${this.escape(h.result)}${h.comment ? " ｜ 备注：" + this.escape(h.comment) : ""}</div>
        <div class="hr-meta">👉 点击查看当时的完整记录</div>
      </div>`).join("");

    const totalEta = report.total_eta_min;
    const modeLine = report.degraded
      ? `降级模式（规则+知识库）：${this.escape((report.degraded_reasons || [])[0] || "")}`
      : "大模型增强模式";

    host.innerHTML = `
      <div class="report-head ${cls}">
        <h1 class="conclusion">
          ${this.escape(head.device)} · ${this.escape(head.fault)}
          <span class="confidence">（置信度：${this.escape(head.confidence.text)}）</span>
          <span class="kb-badge" id="kbBadge" data-kb="${this.escape(head.kb_id)}" title="点击查看知识库条目">${this.escape(head.code || "无编号")}</span>
        </h1>
        <div class="report-sub">记录号 #${report.record_id} ｜ ${this.escape(this.fmtTime(report.created_at))} ｜ ${modeLine}</div>
        <div class="report-conclusion-text">${this.escape(head.conclusion)}</div>
      </div>

      ${uncovered ? `<div class="uncovered-box">
        <div class="u-title">⚠ 知识库未覆盖</div>
        <div style="margin-top:6px">当前现象在知识库中没有对应条目，下面只给出通用安全处置原则，请不要凭经验盲目拆解。</div>
        <ul><li>先停机断电，出现冒烟/焦糊味/火花时严禁继续送电试机</li>
            <li>保持现场并拍照记录，形成工单上报设备工程师</li>
            <li>在原因未确认前，不得反复复位报警继续生产</li></ul>
      </div>` : ""}

      <div class="section">
        <h3 class="section-title">数据证据 <span class="hint">实测值 vs 阈值，右侧为最近趋势</span></h3>
        ${evidence}
      </div>

      ${(report.causes || []).length ? `<div class="section">
        <h3 class="section-title">可能原因排序 <span class="hint">按可能性排序，每条带依据</span></h3>
        ${report.causes.map((c, i) => `
          <div class="cause-row">
            <div class="cause-rank">${i + 1}</div>
            <div class="cause-body">
              <div class="cause-title">${this.escape(c.title || c.kb_id || "")}
                <span class="cause-prob">可能性 ${Math.round((c.probability || 0) * 100)}%</span></div>
              <div class="cause-text">${this.escape(c.text || "")}</div>
              <div class="cause-basis">依据：${this.escape(c.basis || "")}</div>
            </div>
          </div>`).join("")}
      </div>` : ""}

      ${report.warnings.length ? `<div class="section"><h3 class="section-title">安全警告</h3>
        <ul class="warn-list">${report.warnings.map(w => `<li>${this.escape(w)}</li>`).join("")}</ul></div>` : ""}

      <div class="section">
        <h3 class="section-title">排查步骤 <span class="hint">预计总耗时 ${totalEta} 分钟</span></h3>
        ${stepsHtml}
      </div>

      ${histRef ? `<div class="section"><h3 class="section-title">相似历史</h3>${histRef}</div>` : ""}

      <div class="feedback" id="feedbackBox">
        <div class="feedback-actions">
          <button class="btn btn-primary" id="fbOk" type="button">✅ 已解决</button>
          <button class="btn btn-danger" id="fbNg" type="button">❌ 没解决</button>
          <button class="btn btn-warn" id="startStepMode" type="button">▶ 开始排查（单步模式）</button>
        </div>
        <div class="feedback-extra">
          <label><span class="fb-field-label">操作人<span class="req" title="必填">*</span></span>
            <input id="fbOperator" type="text" placeholder="姓名/工号（必填，记入运维记录）" required />
          </label>
          <label><span class="fb-field-label">处理时长（分钟）</span>
            <input id="fbHandling" type="number" min="0" step="1" placeholder="选填，缺省按反馈时间推算" />
          </label>
          <label><span class="fb-field-label">停机时长（分钟）</span>
            <input id="fbDowntime" type="number" min="0" step="1" placeholder="留空则按报警起点自动计时" />
          </label>
        </div>
        <div class="fb-downtime-auto" id="fbDowntimeAuto">⏱ 未检测到本次报警起点（设备报警后自动计时，也可手动填写）</div>
        <input class="feedback-note" id="fbNote" type="text" placeholder="备注（可选）：怎么处理的、换了什么件" />
        <div class="feedback-done" id="fbDone" hidden></div>
      </div>`;
  },

  /* ---------------- 知识库条目详情 ---------------- */
  async openKb(kbId) {
    if (!kbId) return;
    try {
      const kb = await API.kb(kbId);
      this.el("kbModalCode").textContent = kb.code || kb.kb_id;
      this.el("kbModalTitle").textContent = `${kb.title}（${kb.kb_id}）`;
      const steps = kb.steps.map(s => `
        <li><b>[${s.level}]</b> ${this.escape(s.text)}
          <div class="step-meta"><span>🧰 工具：${s.tools.length ? s.tools.map(t => this.escape(t)).join("、") : "无"}</span>
          <span>📦 物料：${s.materials.length ? s.materials.map(t => this.escape(t)).join("、") : "无"}</span>
          <span>⏱ ${s.eta_min} 分钟</span></div></li>`).join("");
      this.el("kbModalBody").innerHTML = `
        <div class="mono">来源文件：${this.escape(kb.source_file)} ｜ 适用设备类型：${kb.device_types.map(t => this.escape(t)).join("、")}</div>
        <p><b>结论：</b>${this.escape(kb.conclusion)}</p>
        <h4>可能原因</h4><ul class="kv-list">${kb.causes.map(c => `<li>${this.escape(c.text)}<div class="step-meta">依据：${this.escape(c.basis || "")}</div></li>`).join("")}</ul>
        <h4>标准排查步骤</h4><ol class="kv-list">${steps}</ol>
        <h4>安全要求</h4><ul class="kv-list">${kb.safety.map(s => `<li>${this.escape(s)}</li>`).join("")}</ul>`;
      this.el("kbModal").hidden = false;
    } catch (err) {
      this.toast(`知识库条目加载失败：${err.message}`, true);
    }
  },
};

/* ---------------- 排查模式：全屏单步 ---------------- */
const StepMode = {
  report: null,
  idx: 0,
  currentKey: null,

  start(report) {
    if (!report || !report.steps.length) { UI.toast("本次诊断没有可执行的排查步骤", true); return; }
    this.report = report;
    this.idx = 0;
    this.el = UI.el("stepMode");
    UI.el("stepDevice").textContent = `${report.headline.device} · ${report.headline.fault}`;
    this.el.hidden = false;
    this.render();
  },
  stop() {
    UI.el("stepMode").hidden = true;
  },
  step() { return this.report.steps[this.idx]; },

  render() {
    const s = this.step();
    UI.el("stepProgress").textContent = `第 ${this.idx + 1} 步 / 共 ${this.report.steps.length} 步`;
    const isL3 = s.level === "L3";
    UI.el("stepSafetyTop").hidden = !isL3;
    const done = (App.checks || {})[s.key];
    const levelMeta = { L1: "🟢 L1 · 操作工可执行", L2: "🟡 L2 · 维修工执行", L3: "🔴 L3 · 必须工程师执行" }[s.level];
    const tools = s.tools.length ? s.tools.map(t => UI.escape(t)).join("、") : "不需要工具";
    const mats = s.materials.length ? s.materials.map(t => UI.escape(t)).join("、") : "不需要物料";
    const safety = isL3
      ? `<div class="step-mode-safety">⚠ ${s.danger_badge || "该步骤涉及高压/拆机，需工程师执行并确认安全措施"}<br>
         执行前：停机、断电、挂牌上锁（LOTO）、泄压、等待高温部件冷却；无资质人员禁止操作。<br>
         系统不检测设备是否断电，物理安全由设备侧硬回路与现场 LOTO 规程承担。</div>` : "";
    const ackLine = isL3
      ? (s.ack
          ? `<div class="step-mode-ack ok">🔒 安全确认已留痕：${UI.escape(s.ack.ack_by)} 于 ${UI.escape(s.ack.ack_at)} 人工确认（非系统断电检测）</div>`
          : `<div class="step-mode-ack pending">🔒 未完成安全确认：进入本步前需持证人员勾选声明并署名</div>`)
      : "";
    UI.el("stepBody").innerHTML = `
      <div class="step-mode-content">
        <div class="step-mode-level">${levelMeta}</div>
        <div class="step-mode-text">${UI.escape(s.text)}</div>
        <div class="step-mode-meta">
          <div>🧰 <b>所需工具：</b>${tools}</div>
          <div>📦 <b>所需物料：</b>${mats}</div>
          <div>⏱ <b>预计耗时：</b>${s.eta_min} 分钟</div>
          ${s.safety && s.safety.length ? `<div>⚠ ${s.safety.map(x => UI.escape(x)).join("；")}</div>` : ""}
          <div>来源：${UI.escape(s.kb_code)} ${UI.escape(s.kb_title)}</div>
        </div>
        ${safety}${ackLine}
        <div style="margin-top:18px">当前勾选状态：<b>${done ? "已完成 ✅" : "未完成 ⬜"}</b></div>
      </div>`;
    UI.el("stepPrev").disabled = this.idx === 0;
    UI.el("stepNext").textContent = this.idx === this.report.steps.length - 1 ? "✅ 完成，收工" : "✅ 完成，下一步";
  },

  /* 进入 L3 步骤前必须过服务端安全确认（已留痕的可直接进入），不允许跳过 */
  enter(forward = true) {
    const s = this.step();
    if (forward && s.level === "L3" && !s.ack) { this.openSafeConfirm(); return; }
    this.render();
  },
  openSafeConfirm() {
    const s = this.step();
    UI.el("safeLead").textContent = s.danger_badge
      ? `${s.danger_badge}：该步骤涉及危险作业，需工程师执行并确认安全措施。`
      : "该步骤涉及高压/拆机，需工程师执行并确认安全措施。";
    UI.el("safeList").innerHTML = [
      ...(s.safety || []),
      ...(this.report.warnings || []),
      "必须由持证/专业人员执行，并执行断电挂牌上锁（LOTO）",
    ].map(x => `<li>${UI.escape(x)}</li>`).join("");
    UI.el("safeAck").checked = false;
    UI.el("safeAckBy").value = "";
    UI.el("safeConfirm").disabled = true;
    UI.el("safeModal").hidden = false;
  },
  async confirmSafe() {
    const s = this.step();
    const ackBy = (UI.el("safeAckBy").value || "").trim();
    if (!UI.el("safeAck").checked || !ackBy) {
      UI.toast("请勾选安全声明并填写确认人姓名/工号", true);
      return;
    }
    const btn = UI.el("safeConfirm");
    btn.disabled = true;
    try {
      // 服务端强制闸门：确认人必填、step_key 必须是本次诊断的 L3 步骤，落库留痕
      const ack = await API.ackL3(this.report.record_id, s.key, ackBy);
      s.ack = ack;
      s.locked = false;
      (this.report.steps_by_level?.L3 || []).forEach(x => {
        if (x.key === s.key) { x.ack = ack; x.locked = false; }
      });
      if (this.report.l3_acks) this.report.l3_acks[s.key] = ack;
      UI.el("safeModal").hidden = true;
      this.render();
      UI.toast(`安全确认已留痕：${ackBy}（人工确认，非系统断电检测）`);
    } catch (err) {
      btn.disabled = false;
      UI.toast(`安全确认未通过：${err.message}`, true);
    }
  },
  async markDone() {
    // 与报告卡共用 App.checks（服务端为唯一真相），不再读写浏览器本地缓存
    const key = this.step().key;
    const before = { ...(App.checks || {}) };
    const map = { ...before, [key]: true };
    App.checks = map;
    App.applyChecks(this.report);
    try {
      await API.saveChecklist(this.report.record_id, map);
      // 成功即保留本次 map：避免服务端快照覆盖并发勾选
    } catch (err) {
      App.checks = before;   // L3 未确认等被服务端拒绝时回滚，不留下假勾选
      UI.toast(`勾选保存失败：${err.message}`, true);
    }
    App.applyChecks(this.report);
  },
  async next() {
    await this.markDone();
    if (this.idx >= this.report.steps.length - 1) {
      this.stop();
      App.guideFeedback();
      return;
    }
    this.idx += 1;
    this.enter(true);
  },
  prev() {
    if (this.idx === 0) return;
    this.idx -= 1;
    this.enter(false);      // 回看上一步不需要再次确认安全
  },
};
