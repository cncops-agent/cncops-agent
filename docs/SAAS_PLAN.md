# 记忆库完善与 SaaS 化预留方案

> **⚠ 当前状态提示（2026-11-04）**：文中「82/82 自测」等数字是**当时轮次**的实测存档，按事实保留不改。当前版本已新增 M 段「L3 服务端强制闸门」6 项断言，`tests/selftest.py` 为 **88/88 全过**、`pytest` **27 passed**，最新输出见 `docs/selftest_output.txt`（说明见 `docs/SELFTEST.md` 第十节）。L3 安全确认是**人工署名 + 服务端强制校验 + 落库留痕**，**不是设备断电检测**；系统不判断设备是否真的断电，物理安全由设备侧硬回路与现场 LOTO 规程承担。

> 本文档回答四件事：
> 1. 运维记录表现在支持哪些维度（多工厂 / 多设备 / 操作人 / 反馈结果），字段什么含义；
> 2. 单租户 → 多租户的迁移方案（本阶段只做结构预留，不开发支付与注册）；
> 3. "经验沉淀"视图与数据导出的指标口径；
> 4. 后续 SaaS 化的订阅模型建议（按设备数 / 账号数分档的 3 档功能差异表）。
>
> 相关代码：`src/cncops/memory.py`（表结构与统计）、`src/cncops/export.py` + `src/cncops/xlsx.py`（导出）、
> `src/cncops/api.py`（`/api/factories`、`/api/insights`、`/api/records/export`）、
> `web/static/js/insights.js`（经验沉淀视图）。验收：`tests/selftest.py` I 段 7 项 + `scripts/shot_insights.py` 无头浏览器截图。

---

## 一、运维记录表：字段与维度

### 1.1 表结构（v2）

```
factories                      工厂目录（多工厂维度）
  id / name / tenant_id / created_at

diagnoses                      一次诊断 = 一条运维记录
  id / created_at              记录号 / 时间（ISO 秒级）
  device_id / device_name / device_type      多设备：设备编号/名称/类型
  query / parsed_json / snapshot_json        当时的口语描述与完整数据快照
  evidence_json / steps_json / conclusion    超标证据 / 排查步骤原文 / 结论
  fault_id / fault_title / kb_ids            命中的故障条目（知识库编号，如 KB-CNC-001）
  max_level / signature / mode / llm_used    处置等级 / 故障指纹 / 诊断模式
  factory_id                   ★ 所属工厂（多工厂隔离维度）
  tenant_id                    ★ 租户（多租户隔离维度，预留）
  operator                     ★ 操作人（报修/提问人）
  alarm_codes                  ★ 当时派生的报警码（JSON 数组，导出与统计用）

feedback                       一次处理反馈（一条诊断可多次反馈，统计取最新一条）
  diagnosis_id / resolved / comment / created_at
  operator                     ★ 操作人（实际处理人）
  handling_min                 ★ 处理时长（分钟；操作人填报，缺省按"反馈时间-诊断时间"推算）
  downtime_min                 ★ 本次停机时长（分钟；老板视角的停机统计）
  tenant_id / factory_id       ★ 与所属诊断一致（反规范化，便于按工厂直接统计）

step_checks                    排查步骤勾选状态（原有，不变）
```

★ = 本次新增。所有新列都通过 `_migrate()` 以 `ALTER TABLE ADD COLUMN ... DEFAULT` 方式无感加到老库上，
**不重建表、不丢数据**；`factory_id` 按"设备当前所属工厂"回填，剩余归默认工厂 `F001`，`tenant_id` 一律回填 `default`。

### 1.2 维度语义

| 维度 | 字段 | 本阶段取值 | SaaS 化后 |
|---|---|---|---|
| 多工厂 | `factory_id` | `config/devices.yaml` 的 `factories:` 目录 + 每台设备的 `factory_id`（正式线 F001；10 台演示配置 F001/F002 各 5 台） | 一个租户下可有多个工厂；工厂归属账号（主账号/子账号） |
| 多设备 | `device_id` | 设备目录即插即用，任意台数 | 按订阅档位限制设备接入数 |
| 操作人 | `operator`（提问）/ `feedback.operator`（处理） | 前端本地记住的姓名/工号（无登录体系前的过渡），可配 `CNCOPS_DEFAULT_OPERATOR` | 替换为账号体系的当前用户，**接口字段不变** |
| 反馈结果 | `feedback.resolved`（已解决/未解决/未反馈）+ `comment` | 原有能力，未变 | 同上 |
| 多租户 | `tenant_id` | 恒为 `default`（单租户） | 部署环境注入（`CNCOPS_TENANT_ID` 或网关头），见下 |

---

## 二、单租户 → 多租户迁移方案（只做结构预留）

当前所有查询都带 `tenant_id` 条件（`MemoryStore` 实例记住自己的租户，`_joined_rows` /
`device_history` / `recent` / `get` 等全部附加 `AND d.tenant_id=?`），即**行级隔离的最小实现已经就位**。
上云时按下面三步走，不需要改表结构：

### 第 1 步：租户上下文注入（应用层，1 人日）

- 部署形态 A（每租户独立库/独立实例）：启动时注入 `CNCOPS_TENANT_ID=<租户>`，
  数据文件按租户分目录（`data/tenants/<tenant>/cncops.db`）。隔离最强，适合大客户/私有化。
- 部署形态 B（共享库）：保持单库，由 API 网关/JWT 解析出租户 id，注入请求上下文；
  `MemoryStore` 增加 `for_tenant(tenant_id)` 工厂方法（按租户取/建连接）。
  本阶段 `MemoryStore(db_path, tenant_id)` 的签名已为此留好。

### 第 2 步：存量数据归属（一次性脚本）

```sql
-- 形态 B：把 default 租户的存量数据划给第一个租户
UPDATE diagnoses SET tenant_id='<首个租户>' WHERE tenant_id='default';
UPDATE feedback  SET tenant_id='<首个租户>' WHERE tenant_id='default';
UPDATE factories SET tenant_id='<首个租户>' WHERE tenant_id='default';
```

### 第 3 步：账号与权限（注册/登录/支付之前只到"账号"为止）

- `users` 表预留字段建议：`id / tenant_id / factory_ids(JSON) / role(owner|manager|operator) / created_at`；
- 角色与界面：owner 看全部工厂+导出；manager 看本厂；operator 只能诊断与反馈（即当前界面）；
- 知识库、设备目录同步加 `tenant_id`（配置即代码，按租户加载不同 config 目录）。

### 明确不做的事（本阶段边界）

不做：支付、订阅计费、注册登录、邮件通知、组织架构。结构上不为它们留"半成品"，
但 `tenant_id / factory_id / operator` 三个字段保证上述功能上线时**无需改表**。

---

## 三、"经验沉淀"视图：指标口径

接口：`GET /api/insights?factory_id=&device_id=&days=90&recur_days=30`

| 指标 | 口径 | 界面呈现 |
|---|---|---|
| 高频故障 | 按"设备 × 故障"计数（`fault_id` 非空且非 `KB-GEN` 兜底条目），取 Top5 | 经验页按设备条形图 + 次数 |
| 平均处理时长 | 该设备有反馈记录的处理时长均值；**优先操作人填报值**，缺省按"反馈时间 − 诊断时间"推算（分钟，≥0） | 统计卡 + 按设备条形图 |
| 复发故障提醒 | 同设备、同故障在 `recur_days`（默认 30 天）内出现 ≥2 次即列入；≥3 次升级 `critical` | 红色/黄色提醒卡 + 处置建议 |
| 解决率 | 已解决 ÷ 有反馈记录数 | 统计卡 |
| 累计停机 | 反馈填报的 `downtime_min` 求和（未填报不计入，不编造） | 统计卡 + 按设备条形图 |

复发提醒文案会自动带上下次处置建议（如"建议复核上次处置是否彻底（换件/对中/清滤网）"），
有未解决记录时追加"禁止反复复位报警继续生产"——与 L3 安全警告同一套安全口径。

---

## 四、数据导出设计

接口：`GET /api/records/export?fmt=csv|xlsx&device=&factory_id=&result=&q=&days=`
（过滤条件与历史页一致，"看到的就能导出"；按钮在历史页与经验页均有入口）

**CSV**（`text/csv; charset=utf-8-sig`，带 BOM）：Excel/WPS 双击直接打开，中文不乱码。
18 列：记录号 / 时间 / 工厂 / 设备编号 / 设备名称 / 故障编号 / 故障名称 / 报警码 / 处置等级 /
处理结果 / 处理时长(分钟) / 停机时长(分钟) / 操作人 / 反馈人 / 诊断模式 / 当时描述 / 结论 / 反馈备注。

**Excel**（`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，自研零依赖写入器，见 `src/cncops/xlsx.py`）：
- 工作表 1「运维记录」：同上 18 列明细，首行加粗冻结；
- 工作表 2「故障与停机统计」：按"设备 × 故障"汇总 故障次数/已解决/未解决/未反馈/
  平均处理时长/累计停机/最近发生，末行合计——**这张表就是给工厂老板看的**。

下载文件名带日期（`运维记录_YYYYMMDD.csv` / `运维记录与统计_YYYYMMDD.xlsx`），
`Content-Disposition` 同时给 ASCII 兜底与 `filename*=UTF-8''` 编码值，各浏览器不乱码。

---

## 五、SaaS 订阅模型建议（只写方案，不开发支付）

定价锚点：**接入设备数**（主要成本驱动：数据采集点位数、存储与趋势采样量）+
**账号数**（人的成本：谁看报表、谁收报警）。诊断次数不设限（边际成本近零，作为卖点）。

### 5.1 三档功能差异表

| 能力 | 免费版（试点） | 专业版（主力） | 企业版（私有化/多工厂） |
|---|---|---|---|
| **接入设备数** | ≤ 5 台 | ≤ 50 台 | 不限（多工厂） |
| **账号数** | 2 个（1 主 1 子） | 10 个，含角色权限 | 不限，支持组织架构/SSO |
| **实时监控台** | ✅ | ✅ | ✅ |
| **口语诊断 + 知识库** | ✅ 通用库 | ✅ + 自定义知识库条目 | ✅ + 多知识库版本管理 |
| **排查步骤分级（L1/L2/L3）与安全确认** | ✅ | ✅ | ✅ |
| **历史记录** | 近 30 天 | 近 3 年 | 不限 + 归档导出 |
| **经验沉淀视图**（高频故障/平均处理时长） | 近 30 天 | 全周期 + 复发提醒推送 | 全周期 + 多工厂横向对比 |
| **复发故障提醒** | 界面内提醒 | 界面 + 邮件/ webhook | 界面 + 邮件/ webhook + 企业微信/钉钉 |
| **数据导出 CSV/Excel** | 仅 CSV，最近 100 条 | CSV + Excel 全量 | CSV + Excel 全量 + 定时报表 |
| **多工厂管理** | ❌ | 单工厂 | ✅ 多工厂 + 工厂间横向对标 |
| **趋势数据保留** | 7 天 | 1 年 | 3 年（可配置） |
| **LLM 增强模式** | ❌（规则模式） | ✅ 按量计费/包月 | ✅ 私有模型端点 |
| **部署方式** | 公有云共享 | 公有云独占实例 | 公有云 / 私有化 / 混合 |
| **建议年费（人民币，参考）** | 免费（90 天试点） | ¥6,800/年（50 设备档）；加购 ¥120/台/年 | ¥30,000/年起，按工厂数与账号数报价 |
| **目标客户** | 个体维修铺、学校实训 | 中小工厂设备科（本项目原型的主要客户） | 集团制造企业、代工厂 |

### 5.2 计费与迁移建议

1. **按"设备数"阶梯计价，年付折扣 85 折**；设备数超档时按"加购包"补，不强制升档（避免丢单）。
2. **免费版 → 专业版的数据无损迁移**：同一套库结构 + `tenant_id` 隔离，升级只是改租户配置，
   历史记录/知识库/勾选状态全部保留（这正是本阶段把字段一次做对的原因）。
3. **私有化（企业版）走授权码**：同一份代码，`CNCOPS_TENANT_ID` + license 文件控制功能开关，
   不在代码里分叉（避免"开源版/商业版"两份代码维护爆炸）。
4. **支付接入点预留**：账单实体建议 `subscriptions(tenant_id, plan, device_quota, seat_quota,
   status, period_end)`，支付只写这一张表；本阶段不建表、不接支付 SDK。

### 5.3 与现有代码的对应关系（升级时要改什么）

| SaaS 功能 | 需要新增 | 已有可复用 |
|---|---|---|
| 注册/登录 | `users` 表 + 登录页 + JWT | `tenant_id/factory_id/operator` 字段、`MemoryStore(db, tenant)` |
| 订阅计费 | `subscriptions` 表 + 支付 SDK | 设备数已有（`catalog.devices()`）、账号数待建 |
| 多工厂账号权限 | `users.factory_ids` + 接口层过滤 | `/api/factories` 已按工厂返回记录数 |
| 定时报表 | 定时任务 + 邮件/ webhook | `/api/records/export` 直接产出文件 |
| 私有化 | license 校验 + 部署脚本 | `CNCOPS_*` 环境变量体系、单文件 SQLite |

---

## 六、本次改动的验收证据

- `tests/selftest.py` **I 段 7/7**：多工厂隔离、复发提醒（≥2 warn / ≥3 critical）、
  平均处理时长（填报优先/时间差兜底）、操作人端到端落库、CSV 导出（BOM/18 列/行数一致）、
  Excel 导出（zip 结构/双工作表/停机统计）、导出参数校验（非法格式 400）；
- `scripts/shot_insights.py` 无头 Chrome：经验页三块渲染 + 复发分级 + 导出按钮真实点击，0 JS 报错，
  截图 `docs/screenshots/11_insights_recurrence.png`、`12_insights_stats.png`；
- 完整自测输出：`docs/selftest_output.txt`（当前为 82/82；本段文字成稿于 I 段轮，该存档此后又新增 J/K/L 段，逐轮历史见 `docs/SELFTEST.md` 第七~九节）。
