# STORY.md · cncops-agent 路演 PPT

## ① 用户意图对齐
- **目标受众**：比赛路演评委 / 潜在客户（中小工厂主）/ 投资评审。
- **核心目标**：让观众相信"小工厂也能有随时在线的设备医生"——只读、安全、离线、经验可沉淀；记住 ROI 一次停机回本、82/66/27 可验收、真实模型已验证。
- **PPT 长度**：20 页（封面+目录+4 部分 16 内容页+封底）。
- **视觉调性**：深色工业、科技可信、数据驱动、克制有力。
- **内容边界**：必讲——痛点、技术方案、只读安全、AI 应用流程、对比与落地价值、诚实边界；不讲——未落地的真实工厂案例（如实声明仿真）、不夸大覆盖率。

## ② 页面布局骨架
- 总页 20；分 4 部分：需求背景(P3-4)、技术方案(P5-11)、AI 工具应用流程(P12-14)、价值与落地(P15-16)；收尾 P17-20。
- 目录(P2)声明 4 部分，正文以"PART 0X"标签在每部分首页标注，一一对应。
- **Hero 页**（封面 P1、安全 P9、质量 P11、对比 P15、封底 P20）占比 25%，间隔均 ≥1 supporting 页。
- **rhythm**：peak 页 P3/P7/P11/P15/P16；transition 用 PART 标签；其余 valley。无连续 ≥3 valley。
- **非对称版式**占比 >40%（约 12/20）；`N卡片横排` 仅 P8、P17 两页。

## ③ 页面大纲（20 页）
| # | title | type | role | rhythm | layout | visual | visual_role | anti_pattern |
|---|---|---|---|---|---|---|---|---|
| 01 | 车间设备运维 AI 智能体·只读诊断 | cover | hero | peak | 全屏视觉+骑线文字 | grid bg+shield+数据条 | anchor | 禁装饰小图 |
| 02 | 目录 | catalog | supporting | valley | 左标题+右内容 | — | — | 禁四卡预览 |
| 03 | 需求背景·设备运维五大痛点 | content | peak | peak | 非对称双栏(左5痛点+右ROI) | 大数字ROI | anchor | 禁N卡横排 |
| 04 | 目标用户与典型场景 | content | supporting | valley | 左标题+右内容(三行表) | table | evidence | 禁等分双栏 |
| 05 | 产品形态（一屏三区） | content | supporting | valley | 左大图+右文字 | report_card.png | evidence | 禁50:50 |
| 06 | 整体技术架构（分层） | content | supporting | valley | 非对称(架构SVG) | svg arch | evidence | 禁单栏列表 |
| 07 | 核心诊断引擎（固定六步） | content | peak | peak | 巨型流程(6步) | svg flow | anchor | 禁N卡横排 |
| 08 | 技术选型（轻量·离线） | content | supporting | valley | N卡片横排(技术墙) | cards | evidence | 禁相邻同版式 |
| 09 | 安全设计（只读红线+L3/LOTO） | content | hero | peak | 非对称双栏(安全) | l3_confirm.png | evidence | 禁等分双栏 |
| 10 | 大模型增强与降级模式 | content | supporting | valley | 非对称双栏(增强vs降级) | 20/20数字 | anchor | 禁N卡横排 |
| 11 | 验收与质量保障 | content | peak | peak | 巨型数字(82/66/27) | 大数字 | anchor | 禁装进图表卡 |
| 12 | 端到端应用流程（口语→结论） | content | supporting | valley | 上大图+下卡片 | 02/08/09 | evidence | 禁等分双栏 |
| 13 | 现场实操演示脚本（3分钟） | content | supporting | valley | 左大图+右文字(三截图) | 截图组 | evidence | 禁50:50 |
| 14 | 经验沉淀闭环（记忆库） | content | supporting | valley | 左大图+右文字 | feedback_history | evidence | 禁50:50 |
| 15 | 和传统运维的对比 | content | peak | peak | 对比表(左灰右青) | table | anchor | 禁四卡 |
| 16 | 产业场景落地价值 | content | peak | peak | 非对称(场景+路径+价值) | svg 时间轴 | anchor | 禁等分双栏 |
| 17 | 商业模式（SaaS 订阅） | content | supporting | valley | N卡片横排(三档定价) | cards | evidence | 禁相邻同版式 |
| 18 | 后续路线（仿真→真实→多品类） | content | supporting | valley | 时间轴svg | svg timeline | evidence | 禁等分双栏 |
| 19 | 团队与验证/诚实边界 | content | supporting | valley | 左标题+右内容(边界) | — | evidence | 禁等分双栏 |
| 20 | 让每一次停机都变成工厂的经验 | ending | hero | peak | 全屏+金句 | shield | anchor | 禁装饰小图 |

## 数据落点（关键页必含"所以呢"）
- P3：停机 ¥5万+/h vs 年费数千 → ROI 一次停机回本。
- P10：真实 DeepSeek 20/20 + 11/11 → 配了更会说人话，不配结论一样准。
- P11：82/66/27 可复现 → 评委可当场让我重跑。
- P15：传统靠人，我们靠系统+L3闸门 → 不是替代维修工，是给他配老师傅。
- P16：ROI 一次回本 / 覆盖 80% / 离线可交付 → 低风险渐进落地。
