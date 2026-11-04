/* 接口层：所有动态数据都来自后端 API，前端不产生、不缓存任何"假实时数据"。
   接口异常统一抛错，由上层显示「数据连接中断」而不是继续显示上一次的值。 */
const API = {
  async request(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const body = await resp.json();
        detail = body.detail || detail;
      } catch (_) { /* 非 JSON 响应时保留状态码信息 */ }
      throw new Error(detail);
    }
    return resp.json();
  },
  get(path) { return this.request(path); },
  post(path, body) { return this.request(path, { method: "POST", body: JSON.stringify(body || {}) }); },

  line()        { return this.get("/api/line"); },
  device(id)    { return this.get(`/api/devices/${encodeURIComponent(id)}`); },
  trend(id, n = 24) { return this.get(`/api/trend/${encodeURIComponent(id)}?n=${n}`); },
  health()      { return this.get("/api/health"); },
  diagnose(q, deviceId, operator) {
    return this.post("/api/diagnose", { query: q, device_id: deviceId || null, operator: operator || "" });
  },
  kb(kbId)      { return this.get(`/api/kb/${encodeURIComponent(kbId)}`); },
  checklist(recordId) { return this.get(`/api/records/${recordId}/checklist`); },
  saveChecklist(recordId, checks) { return this.post(`/api/records/${recordId}/checklist`, { checks }); },
  /* L3 安全确认：服务端闸门 + 人工确认留痕（不是设备断电检测） */
  l3Acks(recordId) { return this.get(`/api/records/${recordId}/l3-acks`); },
  ackL3(recordId, stepKey, ackBy) {
    return this.post(`/api/records/${recordId}/l3-ack`,
      { step_key: stepKey, ack_by: ackBy, mode: "manual" });
  },
  feedback(recordId, resolved, comment, extra = {}) {
    return this.post(`/api/records/${recordId}/feedback`, {
      resolved, comment,
      operator: extra.operator || "",
      handling_min: extra.handlingMin == null ? null : extra.handlingMin,
      downtime_min: extra.downtimeMin == null ? null : extra.downtimeMin,
    });
  },
  history(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return this.get(`/api/history${qs ? "?" + qs : ""}`);
  },
  historyDetail(id) { return this.get(`/api/history/${id}`); },
  factories()   { return this.get("/api/factories"); },
  insights(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return this.get(`/api/insights${qs ? "?" + qs : ""}`);
  },
  /* 导出：浏览器直接打开下载地址（CSV=utf-8-sig / XLSX 两张表） */
  exportUrl(fmt, params = {}) {
    const qs = new URLSearchParams({ fmt, ...params }).toString();
    return `/api/records/export?${qs}`;
  },
  demoStatus()  { return this.get("/api/demo/status"); },
  demoCatalog() { return this.get("/api/demo/catalog"); },
  demoInject(deviceId, faultId) { return this.post("/api/demo/inject", { device_id: deviceId, fault_id: faultId }); },
  demoClear()   { return this.post("/api/demo/clear", {}); },
};

/* 不做任何「操作人 / 步骤勾选」的浏览器本地记忆。
   原因：绿色包每次带一份全新数据库，诊断 id 从 1 重新开始；若把勾选或署名存在
   localStorage，换一份库后 id 重号会把上一轮的数据错位套到新诊断上（表现为
   「一打开就全部勾好、操作人自动填了上一位的名字」）。
   现在以服务端为唯一真相：勾选走 /api/records/{id}/checklist，操作人每次由本人
   在反馈区填写并随反馈落库；无登录体系的限制不变，将来可直接替换为账号体系。 */
