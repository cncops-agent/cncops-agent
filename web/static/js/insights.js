/* 经验沉淀视图：复发故障提醒 + 按设备的高频故障/平均处理时长/停机统计 + 导出入口。
   数据全部来自 /api/insights 与 /api/records/export，前端只做展示，不本地算数。 */

const InsightsView = {
  data: null,
  factoryLoaded: false,

  filters() {
    return {
      factory_id: UI.el("insightFactory").value,
      days: UI.el("insightDays").value,
    };
  },

  async refresh() {
    const f = this.filters();
    try {
      const data = await API.insights(f);
      this.data = data;
      this.renderSummary(data);
      this.renderRecurrence(data);
      this.renderDevices(data);
    } catch (err) {
      UI.el("insightSummary").innerHTML =
        `<div class="empty-state"><div class="empty-text">加载失败：${UI.escape(err.message)}</div></div>`;
      UI.el("insightRecurrence").innerHTML = "";
      UI.el("insightDevices").innerHTML = "";
    }
  },

  renderSummary(data) {
    const s = data.summary || {};
    const rate = s.resolve_rate == null ? "—" : `${Math.round(s.resolve_rate * 100)}%`;
    const avg = s.avg_handling_min == null ? "—" : `${s.avg_handling_min} 分钟`;
    const down = s.total_downtime_min == null ? "0" : `${s.total_downtime_min} 分钟`;
    const chip = (icon, label, value, cls) =>
      `<div class="stat-chip ${cls || ""}"><div class="stat-icon">${icon}</div>
       <div class="stat-body"><div class="stat-value">${UI.escape(value)}</div>
       <div class="stat-label">${UI.escape(label)}</div></div></div>`;
    UI.el("insightSummary").innerHTML = `
      <div class="insight-caption">统计窗口：近 ${data.window_days} 天｜复发判定：同设备 ${data.recurrence_days} 天内同故障 ≥2 次</div>
      <div class="stat-row">
        ${chip("🧾", "运维记录", s.records || 0)}
        ${chip("✅", "已解决", `${s.resolved || 0}（${rate}）`, "ok")}
        ${chip("❌", "未解决", s.unresolved || 0, "alarm")}
        ${chip("⏳", "未反馈", s.no_feedback || 0)}
        ${chip("⏱", "平均处理时长", avg)}
        ${chip("🛑", "累计停机", down, "warn")}
        ${chip("🔁", "复发故障", `${s.recurrence_count || 0} 项`, (s.recurrence_count ? "alarm" : "ok"))}
      </div>`;
  },

  renderRecurrence(data) {
    const host = UI.el("insightRecurrence");
    const list = data.recurrences || [];
    if (!list.length) {
      host.innerHTML = `<div class="recurrence-empty">✅ 近 ${data.recurrence_days} 天内没有复发性故障（同设备同故障 ≥2 次才会提醒）</div>`;
      return;
    }
    host.innerHTML = `
      <h3 class="insight-title">🔁 复发故障提醒（同设备 ${data.recurrence_days} 天内同故障 ≥2 次）</h3>
      <div class="recurrence-list">
        ${list.map(r => {
          const crit = r.severity === "critical";
          const icon = crit ? "🔴" : "🟡";
          const level = crit ? "高频复发" : "需要关注";
          const avg = r.avg_handling_min == null ? "—" : `${r.avg_handling_min} 分钟`;
          return `<div class="recurrence-card ${crit ? "critical" : "warn"}">
            <div class="rec-head">${icon} <b>${UI.escape(r.device_name || r.device_id)}</b><span class="rec-id">${UI.escape(r.device_id)}</span>
              <span class="rec-fault">${UI.escape(r.fault_title || r.fault_id)}${r.fault_code ? `（${UI.escape(r.fault_code)}）` : ""}</span>
              <span class="rec-badge">${level} · ${r.count} 次</span>
            </div>
            <div class="rec-meta">首次 ${UI.escape(UI.fmtTime(r.first_at))} ｜ 最近 ${UI.escape(UI.fmtTime(r.last_at))}
              ｜ 未解决 ${r.unresolved} 次 ｜ 平均处理 ${avg} ｜ 累计停机 ${r.downtime || 0} 分钟</div>
            <div class="rec-hint">👉 ${UI.escape(r.hint)}</div>
          </div>`;
        }).join("")}
      </div>`;
  },

  renderDevices(data) {
    const host = UI.el("insightDevices");
    const list = data.devices || [];
    if (!list.length) {
      host.innerHTML = `<div class="empty-state"><div class="empty-text">统计窗口内暂无运维记录。先做几次诊断并反馈处理结果，这里就会长出经验。</div></div>`;
      return;
    }
    host.innerHTML = `
      <h3 class="insight-title">📊 按设备统计（高频故障 / 平均处理时长 / 停机）</h3>
      <div class="insight-grid">
        ${list.map(d => {
          const avg = d.avg_handling_min == null ? "—" : `${d.avg_handling_min} 分钟`;
          const maxCount = Math.max(1, ...d.top_faults.map(f => f.count));
          const faults = d.top_faults.length ? d.top_faults.map(f => `
            <div class="fault-row">
              <span class="fault-name" title="${UI.escape(f.fault_title)}">${UI.escape(f.fault_title || f.fault_id)}${f.fault_code ? ` <span class="fault-code">${UI.escape(f.fault_code)}</span>` : ""}</span>
              <span class="fault-bar"><i style="width:${Math.round(f.count / maxCount * 100)}%"></i></span>
              <span class="fault-count">${f.count} 次</span>
              <span class="fault-extra">${f.unresolved ? `未解决 ${f.unresolved}` : ""}${f.avg_handling_min != null ? `｜均 ${f.avg_handling_min} 分钟` : ""}</span>
            </div>`).join("") : `<div class="fault-row"><span class="fault-name">暂无故障记录</span></div>`;
          return `<div class="insight-card">
            <div class="ic-head"><b>${UI.escape(d.device_name || d.device_id)}</b>
              <span class="ic-sub">${UI.escape(d.device_id)}${d.factory_id ? ` ｜ ${UI.escape(d.factory_id)}` : ""}</span></div>
            <div class="ic-stats">
              <span>记录 <b>${d.total}</b></span>
              <span>已解决 <b>${d.resolved}</b></span>
              <span>未解决 <b class="${d.unresolved ? "txt-alarm" : ""}">${d.unresolved}</b></span>
              <span>平均处理 <b>${avg}</b></span>
              <span>累计停机 <b>${d.total_downtime_min} 分钟</b></span>
            </div>
            <div class="ic-faults">${faults}</div>
          </div>`;
        }).join("")}
      </div>`;
  },

  fillFactories(factories) {
    const sel = UI.el("insightFactory");
    if (this.factoryLoaded) return;
    (factories || []).forEach(f => {
      const opt = document.createElement("option");
      opt.value = f.id;
      opt.textContent = `${f.name}（${f.id}）`;
      sel.appendChild(opt);
    });
    this.factoryLoaded = true;
  },
};
