/* 历史视图：时间线列表 + 筛选/搜索 + 展开完整报告（含步骤勾选结果） */
const HistoryView = {
  items: [],
  openId: null,
  detailCache: {},

  async refresh() {
    const device = UI.el("historyDevice").value;
    const result = UI.el("historyResult").value;
    const q = UI.el("historyKeyword").value.trim();
    try {
      const data = await API.history({ device, result, q, limit: 200 });
      this.items = data.items;
      UI.el("historyCount").textContent =
        `共 ${data.count} 条${q ? `（关键字「${q}」）` : ""}${device ? `｜设备 ${device}` : ""}`;
      this.render();
    } catch (err) {
      UI.el("historyCount").textContent = `加载失败：${err.message}`;
      UI.el("historyList").innerHTML =
        `<div class="empty-state"><div class="empty-text">读不到历史记录：${UI.escape(err.message)}</div></div>`;
    }
  },

  render() {
    const host = UI.el("historyList");
    if (!this.items.length) {
      host.innerHTML = `<div class="empty-state"><div class="empty-text">暂时没有运维记录。先在监控页做一次诊断。</div></div>`;
      return;
    }
    host.innerHTML = this.items.map(it => {
      const cls = it.result === "已解决" ? "res-ok" : it.result === "未解决" ? "res-ng" : "res-none";
      const icon = it.result === "已解决" ? "✅" : it.result === "未解决" ? "❌" : "⏳";
      const factory = it.factory_name ? `<span class="hist-factory">${UI.escape(it.factory_name)}</span>` : "";
      return `<div class="hist-item ${cls}" data-id="${it.id}">
        <div class="hist-summary" data-toggle="${it.id}">
          <div class="hist-time">${UI.escape(UI.fmtTime(it.created_at))}</div>
          <div class="hist-device">${factory}${UI.escape(it.device_id)}${it.fault_code ? " · " + UI.escape(it.fault_code) : ""}</div>
          <div class="hist-concl" title="${UI.escape(it.conclusion)}">${UI.escape(it.fault_title || it.conclusion || "—")}</div>
          <div class="hist-level">${UI.escape(it.max_level || "—")}</div>
          <div class="hist-result">${icon} ${UI.escape(it.result)}</div>
        </div>
        <div class="hist-detail" id="histDetail_${it.id}" hidden></div>
      </div>`;
    }).join("");
  },

  async toggle(id) {
    const box = UI.el(`histDetail_${id}`);
    if (!box) return;
    if (!box.hidden) { box.hidden = true; return; }
    // 收起其它行，保证一次只展开一条，手机上更好读
    this.items.forEach(i => { const b = UI.el(`histDetail_${i.id}`); if (b && b.id !== box.id) b.hidden = true; });
    if (this.detailCache[id]) { box.innerHTML = this.detailCache[id]; box.hidden = false; return; }
    box.innerHTML = `<div class="empty-text">加载中…</div>`;
    box.hidden = false;
    try {
      const d = await API.historyDetail(id);
      this.detailCache[id] = this.detailHtml(d);
      box.innerHTML = this.detailCache[id];
    } catch (err) {
      box.innerHTML = `<div class="empty-text">加载失败：${UI.escape(err.message)}</div>`;
    }
  },

  detailHtml(d) {
    const ev = (d.evidence || []).map(e =>
      `<li>${UI.escape(e.text || `${e.label} ${e.value}${e.unit}`)}</li>`).join("");
    const snap = Object.entries(d.snapshot || {}).map(([k, v]) =>
      `<div class="ap-row"><span>${UI.escape(k)}</span><span>${UI.escape(v)}</span></div>`).join("");
    const steps = (d.steps || []).map(s => {
      const done = !!(d.checks || {})[s.key];
      return `<li class="${done ? "done" : ""}">
        <b>[${s.level}]</b> ${done ? "✅" : "⬜"} ${UI.escape(s.text)}
        <div class="step-meta"><span>🧰 ${(s.tools || []).join("、") || "无"}</span>
        <span>📦 ${(s.materials || []).join("、") || "无"}</span><span>⏱ ${s.eta_min} 分钟</span></div></li>`;
    }).join("");
    const handling = d.handling_min == null ? "未记录" : `${d.handling_min} 分钟`;
    const downtime = d.downtime_min == null ? "未记录" : `${d.downtime_min} 分钟`;
    const acks = Object.values(d.l3_acks || {});
    const ackHtml = acks.length ? `
      <div style="margin-top:12px">
        <div style="font-size:16px"><b>L3 安全确认留痕（人工确认，非系统断电检测）</b></div>
        <ul class="kv-list">${acks.map(a =>
          `<li>步骤 ${UI.escape(a.step_key)}：确认人 ${UI.escape(a.ack_by)}｜时间 ${UI.escape(a.ack_at)}｜模式 ${UI.escape(a.mode)}${a.signal_source ? "｜信号来源 " + UI.escape(a.signal_source) : ""}</li>`
        ).join("")}</ul>
      </div>` : "";
    return `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px">
        <div>
          <div style="font-size:16px"><b>当时描述：</b>${UI.escape(d.query || "—")}</div>
          <div style="font-size:16px;margin-top:6px"><b>结论：</b>${UI.escape(d.conclusion || "—")}</div>
          <div style="font-size:15px;color:var(--text-dim);margin-top:6px">
            处置等级 ${UI.escape(d.max_level || "—")} ｜ 诊断模式 ${UI.escape(d.mode || "—")} ｜ ${UI.escape(d.result)}
            ｜ 工厂 ${UI.escape(d.factory_name || d.factory_id || "—")}
          </div>
          <div style="font-size:15px;margin-top:6px"><b>处理备注：</b>${UI.escape(d.comment || "（无）")}</div>
        </div>
        <div>
          <div style="font-size:15px;color:var(--text-dim)">处理台账（经验沉淀数据来源）</div>
          <div class="allpoints-grid">
            <div class="ap-row"><span>报修/提问人</span><span>${UI.escape(d.operator || "匿名")}</span></div>
            <div class="ap-row"><span>实际处理人</span><span>${UI.escape(d.fb_operator || "未反馈")}</span></div>
            <div class="ap-row"><span>处理时长</span><span>${UI.escape(handling)}</span></div>
            <div class="ap-row"><span>停机时长</span><span>${UI.escape(downtime)}</span></div>
          </div>
        </div>
      </div>
      <div style="margin-top:12px">
        <div style="font-size:16px"><b>超标证据</b></div>
        <ul class="kv-list">${ev || "<li>无异常证据</li>"}</ul>
      </div>
      <div style="margin-top:12px">
        <div style="font-size:16px"><b>步骤勾选结果（${d.checked_count || 0}/${d.step_count || 0} 已完成）</b></div>
        <ol class="kv-list">${steps || "<li>无步骤记录</li>"}</ol>
      </div>
      ${ackHtml}`;
  },

  fillDevices(devices) {
    const sel = UI.el("historyDevice");
    if (sel.options.length > 1) return;
    devices.forEach(id => {
      const opt = document.createElement("option");
      opt.value = id; opt.textContent = id;
      sel.appendChild(opt);
    });
  },
};
