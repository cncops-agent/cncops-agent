# cncops-agent · 中小加工厂设备运维 AI 智能体（只读诊断）

> **一句话介绍**：对着一句话描述"设备哪里不对劲"，系统自动取实时数据、比对阈值、检索故障知识库、结合该设备历史，输出**结论先行 + L1/L2/L3 分级排查步骤**的诊断报告，并把每次运维沉淀成可导出的经验台账——全程**只读**，代码里不存在任何下发控制指令的能力。

面向车间场景设计：深色工业风、正文 ≥16px、主按钮 ≥48px（戴手套、强光下可读），平板横屏 / 桌面 / 手机竖屏自适应。

---

## 1. 架构图

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  车间 HMI（web/static，原生 HTML/CSS/JS，无构建步骤）                                    │
│  ┌──────────────┐  ┌────────────────────────────┐  ┌──────────────────────────────┐  │
│  │ 顶部状态条    │  │ 左侧紧凑摘要行（56px 一行）  │  │ 右侧输入区 + 诊断报告卡        │  │
│  │ 一句话总览    │  │ 状态灯+文字/编号/关键值/    │  │ 结论先行·证据火花线·分级清单   │  │
│  │ 报警整条变红  │  │ 趋势线/报警角标 · 报警置顶   │  │ 全屏单步排查 · L3 安全确认     │  │
│  │              │  │ 点击行 → 右侧滑出详情抽屉    │  │                              │  │
│  └──────────────┘  └────────────────────────────┘  └──────────────────────────────┘  │
│  视图：监控 ｜ 历史时间线 ｜ 经验沉淀（高频故障/复发提醒/停机统计） ＋ CSV/Excel 导出      │
└──────────────────────────────────────────────────────────────────────────────────────┘
                    │ fetch（JSON；接口异常 → 顶部显示「数据连接中断」并停刷）
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  Web API 层（src/cncops/api.py · FastAPI）                                              │
│  /api/line  /api/diagnose  /api/kb/{id}  /api/records/{id}/checklist                    │
│  /api/records/{id}/feedback  /api/history  /api/factories  /api/insights                │
│  /api/records/export?fmt=csv|xlsx  /api/demo/*（演示注入，服务端总开关）                 │
└──────────────────────────────────────────────────────────────────────────────────────┘
                    │
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  诊断智能体（src/cncops/agent.py · 固定六步工作流）                                      │
│  ① 口语解析（设备别名+同义词，确定性）→ ② 取实时数据 → ③ 阈值比对生成证据                 │
│  → ④ 知识库三路检索（字面+TF-IDF+结构化）→ ⑤ 历史复发引用 → ⑥ 固定七段格式渲染            │
│  安全闸：safety.py 关键词强制升级 L3（渲染链最后一步，LLM 无法降级）                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
        │                    │                          │                    │
┌───────────────┐  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│ 数据源门面     │  │ 知识库           │  │ 记忆库            │  │ 大模型（可选）        │
│ datasource.py │  │ config/kb/*.yaml │  │ memory.py SQLite │  │ llm_client.py        │
│ simulator ────┼──│ 12 条故障条目     │  │ diagnoses+feedback│  │ OpenAI 兼容协议      │
│ （内置仿真）   │  │ 每步带工具/物料/ │  │ 多工厂/多租户预留 │  │ 未配置/超时/校验失败  │
│ opcua ────────┼──│ 耗时 · L1/L2/L3  │  │ 工厂/操作人/停机  │  │ → 自动降级规则版     │
│ （asyncua 只读）│  │ F001 编号角标    │  │ 复发判定/经验统计 │  │ （步骤恒取自 KB 原文）│
└───────────────┘  └─────────────────┘  └──────────────────┘  └──────────────────────┘
```

**数据流**：仿真器 / OPC-UA（只读）→ 阈值规则（rules.py）→ 报警码派生（阈值+回差+去抖，不写死报警码）
→ 知识库检索 → 报告渲染 → 记忆库落库 → 经验沉淀 / 导出。

**只读边界**：`opcua_client.py` 无任何 `write/set` 方法（`tests/test_safety_readonly.py` 回归该约束）；
故障注入只改仿真器内部状态，是测试夹具，不下发控制指令。

**L3 安全闸门（服务端强制）**：危险步骤（拆机/带电/高压/主轴/液压）由 `safety.py` 确定性升 L3；
进入前必须由持证人员勾选 LOTO 声明并填写姓名/工号，`POST /api/records/{id}/l3-ack` 落库留痕
（确认人+时间，历史详情可查）。未经确认时 `POST /api/records/{id}/checklist` 对该步骤返回 400
且不落库——**绕开前端直接调接口也拦得住**，由自测 M 段 6 项守着。

**诚实边界**：这是**人工确认，不是设备断电检测**。只读数据源拿不到主电源与挂锁状态，系统不判断
设备是否真的断电；物理安全由设备侧硬回路与现场 LOTO 规程承担，系统输出是诊断建议、不是作业许可。

---

## 2. 环境要求

| 项 | 免安装体验（CLI 全链路） | 完整体验（Web HMI + 导出） |
|---|---|---|
| Python | ≥ 3.9（开发验证用 3.11 / 3.13） | 同左 |
| 硬依赖 | **仅 PyYAML** | PyYAML + fastapi + uvicorn + httpx |
| 可选 | asyncua（真实 OPC-UA 通道）、numpy（向量检索加速） | 同左 |
| LLM Key | **不需要**（无 Key 自动进入降级模式） | 同左 |
| 数据库 | 不需要（SQLite 文件自动创建，零配置） | 同左 |
| 网络 | **完全离线可跑** | 仅配 LLM / 嵌入服务时才需要 |
| 浏览器 | — | Chrome / Edge / Safari 近年版本（语音输入需浏览器支持 Web Speech API；2026-10-25 实测：无外网环境录音指示正常但识别服务不可达，详见 FAQ） |

> 无需 GPU、无需向量数据库、无需任何外部服务。Excel 导出为零依赖实现（标准库 zipfile + XML）。

---

## 3. 安装步骤

### 3.1 免安装体验（3 分钟，只有 PyYAML 一个依赖）

适合先看诊断能力：内置仿真数据源 + 规则引擎，跑通"注入 → 报警 → 口语诊断 → 分级步骤 → 反馈入历史"全链路。

```bash
# ① 进入项目
cd cncops-agent

# ② 建虚拟环境（任选 Python 3.9+）
python -m venv .venv && .venv/Scripts/activate        # Windows
# python -m venv .venv && source .venv/bin/activate   # Linux / macOS

# ③ 装唯一硬依赖
pip install -r requirements.txt

# ④ 设置源码路径（或 pip install -e . 二选一）
export PYTHONPATH=src            # Windows: $env:PYTHONPATH="src"

# ⑤ 跑通 6 个口语用例演示（含模糊描述、未覆盖、复发引用、降级模式）
python scripts/demo_cases.py
```

### 3.2 完整体验（Web HMI 界面 + 经验沉淀 + 导出）

```bash
# 接 3.1 的 ①~④，再装 Web 依赖：
pip install fastapi uvicorn httpx

# 可选：真实 OPC-UA 通道（不装则自动用内置仿真，行为一致）
pip install asyncua

# 复制环境变量模板（全部留空即可运行；LLM 三项填上才启用大模型增强）
cp .env.example .env              # Windows: copy .env.example .env

# 启动 Web 服务（默认 127.0.0.1:8000）
python scripts/run_web.py
# 浏览器打开 http://127.0.0.1:8000
```

### 3.3 一键自检（可选，建议首次运行）

```bash
python scripts/check_thresholds.py     # 阈值可达性守门
python tests/selftest.py               # 88 项自动化自测（固定种子/临时库，不污染正式数据）
```

---

## 4. 运行命令

### 4.1 命令行（CLI）

```bash
export PYTHONPATH=src                      # Windows: $env:PYTHONPATH="src"

python -m cncops devices                   # 查看设备与点位目录
python -m cncops faults                    # 查看可注入的故障剧本（11 个）
python -m cncops diagnose "一号机主轴发烫，温度高得厉害"
python -m cncops diagnose "二号机主轴又响又烫，是不是轴承坏了"
python -m cncops diagnose "空压机没劲儿，压力表上不去"
python -m cncops history --device CNC-02   # 该设备历史诊断
python -m cncops feedback 1 --resolved     # 反馈"解决了"（未解决则不加大写参数）
python -m cncops diagnose "切削液颜色发绿还有泡沫"   # 知识库未覆盖 → 明确说查不到
```

启用真实 OPC-UA 通道（可选，进程内拉起 asyncua 仿真服务器）：

```bash
export CNCOPS_DATA_SOURCE=opcua
python -m cncops diagnose "空压机没劲儿，压力表上不去"
```

### 4.2 Web 服务

```bash
python scripts/run_web.py                  # 默认 127.0.0.1:8000
CNCOPS_API_PORT=8020 python scripts/run_web.py    # 换端口
```

多设备预览（10 台：7 加工中心 + 3 空压机，两个工厂；正式 3 台线不受影响）：

```bash
CNCOPS_DEVICES_YAML=config/devices_10.yaml CNCOPS_API_PORT=8020 python scripts/run_web.py
# 打开 http://127.0.0.1:8020
```

30 台压力预览（20 台加工中心 + 10 台空压机；紧凑摘要行 + 详情抽屉在设备量放大后
行高/字号/按钮完全不缩放、不分页，列表独立滚动）：

```bash
CNCOPS_DEVICES_YAML=config/devices_30.yaml CNCOPS_API_PORT=8020 python scripts/run_web.py
```

### 4.3 演示与验收脚本

```bash
python scripts/demo_cases.py               # 6 个口语用例（CLI 版演示）
python scripts/browser_check.py            # 无头 Chrome 真实浏览器验收（断言+截图；建议指向临时库实例，见 5.5）
python scripts/layout_check.py             # 紧凑摘要行+详情抽屉布局验收（3/10/30 台+手机端，66 项）
python scripts/feedback_check.py           # 反馈规则验收：操作人必填 + 停机时长自动计时（无头 Chrome）
python scripts/shot_diagnosis.py           # 诊断报告卡七块全览 + 二次故障相似历史引用截图（自起服务/临时库）
python scripts/shot_insights.py --url http://127.0.0.1:8000/   # 经验页+导出验收（需服务已启动）
python scripts/llm_mode_check.py           # 增强模式 vs 降级模式对比（本地模拟大模型服务，无需 Key）
python -m pytest tests -q                  # 27 项回归测试（含 API 契约：llm_used/原因排序数据契约）
```

### 4.4 运维反馈规则（报告卡底部）

| 字段 | 规则 |
|---|---|
| 操作人（姓名/工号） | **必填**：空姓名点「已解决/没解决」会被拦截（后端接口同样返回 400，不落库） |
| 处理时长（分钟） | 选填；缺省按「反馈时间 − 诊断时间」推算 |
| 停机时长（分钟） | **自动计时**：设备一报警即起表（起点来自后端报警事件，刷新页面可续计时），报警消失继续走表，点「已解决/没解决」确认时停表并计入运维记录；留空即用自动值，也可手动填报覆盖 |

停机时长随后进入历史详情、经验页「累计停机」统计与 Excel 导出的停机汇总表。

---

## 5. 故障注入方法

三种方式，按场景选用。**注入只改仿真器内部数据，不下发任何控制指令。**

### 5.1 Web 演示模式（演示/汇报推荐）

1. 打开页面，点顶栏「演示模式」开关（**默认关闭**；关闭时界面完全没有注入入口）；
2. 面板出现后选设备 + 故障类型 → 点「注入」；
3. 屏幕表现：顶部状态条整条变红（「3 台设备中 1 台报警」）、报警设备摘要行自动置顶并红色高亮（带未处理报警数角标）、读数按物理斜坡变化；
4. 点「全部恢复」一键复位。

### 5.2 命令行注入

```bash
python -m cncops inject --device CNC-02 --fault spindle_bearing_wear
python -m cncops clear  --device CNC-02     # 单台恢复
python -m cncops clear  --all               # 全部恢复
```

### 5.3 API 注入（脚本/自动化验收用）

```bash
curl -X POST http://127.0.0.1:8000/api/demo/inject \
     -H "Content-Type: application/json" \
     -d '{"device_id":"CNC-01","fault_id":"spindle_overheat"}'
# clear 必须带 JSON 请求体（device_id 缺省 = 全部恢复）；不带 body 会返回 422
curl -X POST http://127.0.0.1:8000/api/demo/clear \
     -H "Content-Type: application/json" \
     -d '{}'
```

> 服务端总开关 `CNCOPS_DEMO_ALLOW=0` 可整体关闭演示模式（返回 403），生产部署时使用。

### 5.5 演示 / 验收数据隔离（重要，防止污染正式库）

诊断与反馈会写入正式记忆库 `data/cncops.db`。演示和验收请用下列任一种方式，**不要把测试记录写进正式库**：

1. **自隔离脚本（推荐）**：`scripts/layout_check.py`、`scripts/feedback_check.py`、`scripts/shot_diagnosis.py`
   自带「独立端口 + 临时记忆库 + 强制降级」，直接跑即可，不碰正式库；
2. **临时库实例**：需要对着页面跑的验收（`browser_check.py` / `shot_insights.py` / `shot_10.py`）另起一个
   指向临时库的服务，再把脚本指向它：

```bash
# 另起临时库实例（8021 端口 + 独立库文件）
CNCOPS_API_PORT=8021 CNCOPS_DB_PATH="data/_demo_tmp.db" python scripts/run_web.py &
python scripts/browser_check.py  --url http://127.0.0.1:8021/
python scripts/shot_insights.py --url http://127.0.0.1:8021/
python scripts/shot_10.py http://127.0.0.1:8021
```

3. **恢复出厂数据**：演示数据已经混进正式库时，按下面三步重置（仿真数据自动重建，记忆库回到空台账）：

```bash
# ① 停掉正在跑的服务（Ctrl+C 或结束进程）→ ② 删正式库 → ③ 重启
rm data/cncops.db
python scripts/run_web.py
```

> `tests/selftest.py` 固定使用临时库并在结束时清理，任何时候都不会碰正式库（E-01 断言正式库行数不变）。

### 5.4 可注入故障清单（11 个）

| 设备 | 故障 | 口语问法示例 |
|---|---|---|
| CNC | `spindle_overheat` 主轴过热 | "一号机主轴发烫，温度高得厉害" |
| CNC | `spindle_vibration` 主轴振动 | "二号机主轴声音不对，震动很大" |
| CNC | `spindle_bearing_wear` 轴承磨损 | "二号机主轴又响又烫，是不是轴承坏了" |
| CNC | `spindle_overload` 主轴过载/闷车 | "主轴闷车了，进给跟不上" |
| CNC | `coolant_low` 冷却液不足 | "冷却液好像不够了" |
| CNC | `cnc_air_low` 本机进气压力低 | "机床进气压力不够" |
| 空压机 | `air_pressure_low` 气压不足 | "空压机没劲儿，压力表上不去" |
| 空压机 | `air_overheat` 排气高温 | "空压机排气温度高，机器发烫" |
| 空压机 | `air_oil_overheat` 润滑油高温 | "空压机油温高得吓人" |
| 空压机 | `air_motor_overload` 电机过载 | "空压机电机电流大，一直加载" |
| 空压机 | `air_maintenance_due` 保养到期 | "这台空压机是不是该保养了" |

> **首期范围与扩充计划**：当前为首期 **11 类核心故障**（CNC 6 + 空压机 5，编号 F001–F006 / F101–F104）。
> 后续按现场高频故障优先级扩充，方向包括：伺服过载/丢步、限位开关失效、丝杠反向间隙、刀库换刀故障、
> 编码器异常、油路泄漏/堵塞、冷却系统故障、气动阀门失效等。每类新故障必须同步新增故障剧本、知识库条目、
> 阈值与报警码，并通过「Top-1 命中 + 误报回归 + 阈值可达性」三项自测闸门后才能计入总数。

---

## 6. 常见报错排查

| 现象 / 报错 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'cncops'` | 未设置源码路径 | `export PYTHONPATH=src`（Windows: `$env:PYTHONPATH="src"`），或 `pip install -e .` |
| `ImportError: 缺少依赖 PyYAML` | 硬依赖未装 | `pip install -r requirements.txt` |
| `ImportError: 缺少后端依赖，请执行 pip install fastapi uvicorn` | 起 Web 服务但没装 Web 依赖 | `pip install fastapi uvicorn httpx` |
| 页面打开后顶部显示「数据连接中断」 | 后端服务没起 / 端口不对 / 后端崩了 | 确认 `python scripts/run_web.py` 在跑；浏览器访问 `http://127.0.0.1:8000/api/health` 应返回 `{"status":"ok"}`；看服务端终端堆栈 |
| 8080/8000 端口被占用 | 端口冲突 | 换端口：`CNCOPS_API_PORT=8020 python scripts/run_web.py` |
| 语音按钮点了没反应 | 浏览器不支持 Web Speech API（目前主要是 Chrome/Edge）或未授权麦克风 | 换 Chrome/Edge；地址栏允许麦克风权限；不支持时直接打字，功能不受影响。**2026-10-25 返工轮实测（无头 Chrome + 模拟麦克风）**：API 识别正常、按钮显示、点击后录音指示出现，但 15 秒无识别结果——识别服务需外网，内网/离线环境请直接打字 |
| 报告头部标注"当前为降级模式" | 未配置 LLM Key / 超时 / 返回未通过校验 | 正常现象：规则引擎独立完成诊断，格式不变。需要大模型增强时在 `.env` 填 `CNCOPS_LLM_API_BASE/API_KEY/MODEL` |
| 诊断结论是"知识库未覆盖" | 该现象不在 12 条知识条目内 | 设计行为（查不到就说查不到，只给通用安全建议），不是 bug；可在 `config/kb/*.yaml` 增补条目 |
| 历史页/经验页数据为空 | 用的是全新临时库或从未诊断过 | 先跑一次诊断+反馈；正式库在 `data/cncops.db` |
| 导出 Excel 双击打开乱码 | 用了 CSV 却以错误编码打开 | CSV 自带 utf-8-sig BOM，Excel/WPS 双击直接打开即可；或直接导出 xlsx 格式 |
| `python` 命令不存在 | 系统未装 Python 或不在 PATH | 装 Python 3.9+ 并勾选 "Add to PATH"；或用 `py -3` |
| Windows 下 `./.venv/Scripts/python.exe` 报"不是内部或外部命令" | 用了 Git Bash 之外的 shell 或路径分隔符问题 | Git Bash 用 `./.venv/Scripts/python.exe`；CMD/PowerShell 用 `.venv\Scripts\python.exe` |
| 想恢复出厂数据 | 测试数据混在正式库 | 删 `data/cncops.db` 后重启服务即可（仿真数据自动重建；自测永远用临时库，不碰它） |

---

## 7. 能力边界（交付铁律）

| 铁律 | 落地方式 |
|---|---|
| 只诊断，不下发控制指令 | `opcua_client.py` 无写方法；`tests/test_safety_readonly.py` 回归 |
| 危险操作必须 L3 + 安全警告 | `safety.py` 关键词强制升级，渲染链最后一步，LLM 无法降级 |
| 结论必须可溯源 | 每个步骤带 `来源：KB-XXX` 角标；步骤文本必须存在于知识库原文 |
| 查不到就说查不到 | 检索未命中明确输出"知识库未覆盖"，只给通用安全建议 |
| 断网可用 | 无 LLM Key / 超时 / 校验失败 → 自动降级规则版，格式不变 |

---

## 8. 测试与一键自测

```bash
python -m pytest tests -q                # 27 passed（含 API 契约测试）
python scripts/check_thresholds.py       # 阈值可达性守门
python tests/selftest.py                 # 88 项一键自测（A/B/C/D/E/F/G/H/I/J/K/L/M 十三段）
python scripts/browser_check.py          # 无头 Chrome 真实浏览器验收
python scripts/layout_check.py           # 紧凑列表+抽屉布局验收（3/10/30 台 + 手机端，66 项）
python scripts/feedback_check.py         # 反馈规则验收（操作人必填 + 停机自动计时）
```

自测固定随机种子（20261018）、强制降级模式、记忆库指向临时目录并自动清理，**不污染正式库**；
覆盖：11 个故障逐个注入断言 Top-1、6 条误报回归、37 个阈值/报警码可达性（触发不了逐条列根因）、
Web API 端到端、7 步验收剧本、经验沉淀与导出、KB↔用例双向覆盖、10 台配置、紧凑列表/抽屉数据契约与断线语义。

---

## 9. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/技术说明文档.md](docs/技术说明文档.md) | **比赛提交用**技术说明：AI 辅助生成代码占比口径、开发工具清单、核心功能实现逻辑、验证证据与能力边界（2026-11-04） |
| [docs/SELFTEST.md](docs/SELFTEST.md) | 自测说明、历轮问题与修复记录、完整输出存档 |
| [docs/LAYOUT_ACCEPTANCE.md](docs/LAYOUT_ACCEPTANCE.md) | 设备列表「紧凑摘要行+详情抽屉」重构：改动清单/新结构/验收逐条结果（66/66） |
| [docs/selftest_output.txt](docs/selftest_output.txt) | 最近一次自测完整输出（88/88，含 M 段 L3 闸门） |
| [docs/layout_check_output.txt](docs/layout_check_output.txt) | 最近一次布局验收完整输出（66/66） |
| [docs/coverage_report.txt](docs/coverage_report.txt) | 覆盖率实测存档（pytest+selftest 合并 85%，api.py 92%；含测量命令与口径说明） |
| [docs/browser_check_output.txt](docs/browser_check_output.txt) | 无头 Chrome 界面全流程验收输出（注入→诊断→分级步骤→反馈→断线恢复，0 JS 报错） |
| [docs/shot_diagnosis_output.txt](docs/shot_diagnosis_output.txt) | 诊断报告卡七块结构 + 二次故障历史引用验收（15/15） |
| [docs/llm_mode_check_output.txt](docs/llm_mode_check_output.txt) | 增强/降级模式对比验收（本地模拟大模型服务，17/17） |
| [docs/真实模型增强验证报告.md](docs/真实模型增强验证报告.md) | 真实 DeepSeek 模型接入验证（20/20 链路断言 + 11/11 类故障增强开/关对比，2026-10-27） |
| [docs/真实模型增强验证_raw.txt](docs/真实模型增强验证_raw.txt) | 真实模型验证逐条原始记录（不含密钥） |
| [docs/SAAS_PLAN.md](docs/SAAS_PLAN.md) | 多工厂/多租户迁移方案、指标口径、3 档订阅功能差异表 |
| [docs/DEMO_3MIN.md](docs/DEMO_3MIN.md) | 3 分钟演示脚本（按分钟：做什么/说什么/屏幕结果/备用话术） |
| [docs/PROJECT_PITCH.md](docs/PROJECT_PITCH.md) | 项目说明（路演/参赛/汇报用） |
| [docs/DELIVERY_CHECKLIST.md](docs/DELIVERY_CHECKLIST.md) | 交付清单自查结果表 |
| docs/screenshots/ | 26 张验收截图（01–08 诊断全链路含历史页、08_diagnosis_report 报告卡七块全览、09 二次故障相似历史引用、10 十台预览、11/12 经验页、20–28 布局三档+手机端、29–31 反馈规则；2026-10-25 返工轮重新生成，与各文档引用逐一核对无死链） |

---

## 10. 离线演示包与安装向导（2026-11-04 重编译）

源码启动见第 3 节；给非技术用户分发用下面两个产物（**完全离线、免装依赖、无需 API Key**）：

| 产物 | 路径 | 用途 |
|---|---|---|
| 绿色目录包 | `dist/cncops-demo/`（双击 `cncops-demo.exe`） | 解压即用，数据写在同目录 `data/`，U 盘可带 |
| 标准安装向导 | `installer/cncops-demo-setup.exe` | 装到 `Program Files`，自动建桌面/开始菜单快捷方式，可经控制面板卸载 |

- 构建：`.venv/Scripts/python.exe -m PyInstaller --noconfirm cncops-demo.spec`；编译安装包：`installer/build_installer.bat`（需 NSIS）。图标源 `installer/cncops-demo.ico`。
- **2026-11-04 已按最新源码全量重编译**：包含 L3 服务端强制闸门（`l3_acks` 表 / `l3-ack` 接口 / 确认人署名弹层）；`dist/` 内前端与配置共 17 个文件已逐字节核对与源码一致。
- **自动打开浏览器修复（2026-11-04）**：旧版 `freeze_entry.py` 在子线程用 `webbrowser.open()` 且 `except Exception: pass` 静默吞异常，服务其实已启动但浏览器不弹、控制台也无提示。现改为「线程内初始化 COM → `os.startfile` → `webbrowser` → 独立 `cmd /c start`」三重兜底，成功/失败原因都打印到控制台，并常驻显示可复制的访问地址。
- **新一轮演示从空态开始（2026-11-04）**：取消「操作人 / 步骤勾选」的浏览器本地记忆，勾选状态以服务端 `/api/records/{id}/checklist` 为唯一真相；注入新故障会清空上一份报告与勾选，操作人每轮由本人填写。原因是绿色包自带全新数据库、诊断 id 从 1 重新计数，旧版 localStorage 缓存会把上一轮的勾选与署名错位套到新诊断上。
- 详见 `installer/安装包编译说明.md` 与 `dist/cncops-demo/使用说明.txt`。演示数据来自内置仿真器，系统只读；L3 确认为**人工署名 + 服务端强制校验 + 落库留痕**，**不是设备断电检测**。
