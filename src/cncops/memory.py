"""运维记忆库（SQLite）：每次诊断落库，支持反馈回填，并影响下次同类诊断的原因排序。

表结构：
- factories：工厂目录（多工厂维度；tenant_id 为多租户隔离预留）
- diagnoses：一次诊断的完整快照（时间/工厂/设备/操作人/描述/数据快照/证据/结论/命中故障 id/模式）
- feedback：用户对本次诊断的反馈（解决了 / 没解决 + 操作人 + 处理时长 + 停机时长）
- step_checks：排查步骤勾选状态（界面勾选状态要能刷新不丢失，因此落库存到服务端）

多租户 / 多工厂设计（本阶段为单租户单值，结构先行）：
- diagnoses / feedback 均带 tenant_id + factory_id 两列；
- MemoryStore 实例记住自己的 tenant_id，**所有**查询都会附带该条件（行级隔离的最小实现）；
- 老库通过 _migrate() 无感加列：factory_id 按"设备当前所属工厂"回填，剩余归默认工厂；
  tenant_id 一律回填 "default"。迁移方案详见 docs/SAAS_PLAN.md。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS factories (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    device_id      TEXT NOT NULL,
    device_name    TEXT,
    device_type    TEXT,
    query          TEXT,
    parsed_json    TEXT,
    snapshot_json  TEXT,
    evidence_json  TEXT,
    steps_json     TEXT,          -- 当时给出的排查步骤原文（含工具/物料/耗时）
    conclusion     TEXT,
    fault_id       TEXT,
    fault_title    TEXT,
    kb_ids         TEXT,
    max_level      TEXT,
    signature      TEXT,
    mode           TEXT,
    llm_used       INTEGER DEFAULT 0,
    factory_id     TEXT NOT NULL DEFAULT '',      -- 所属工厂（多工厂隔离维度）
    tenant_id      TEXT NOT NULL DEFAULT 'default', -- 租户（多租户隔离维度，预留）
    operator       TEXT NOT NULL DEFAULT '',      -- 操作人（提问/报修人）
    alarm_codes    TEXT NOT NULL DEFAULT ''       -- 当时派生的报警码（JSON 数组，导出/统计用）
);
CREATE INDEX IF NOT EXISTS idx_diag_device ON diagnoses(device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diag_sign ON diagnoses(device_id, signature);
CREATE INDEX IF NOT EXISTS idx_diag_fault ON diagnoses(device_id, fault_id);
-- 注意：idx_diag_tenant 引用 tenant_id/factory_id 两列，老库在 _migrate() 加列之后才创建，
-- 不能放在本 SCHEMA 里（ executescript 先于迁移执行，老库会直接报 no such column）。

CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id  INTEGER NOT NULL,
    resolved      INTEGER NOT NULL,
    comment       TEXT,
    created_at    TEXT NOT NULL,
    operator      TEXT NOT NULL DEFAULT '',    -- 操作人（实际处理人）
    handling_min  INTEGER,                     -- 处理时长（分钟，操作人填报；缺省按反馈时间差推算）
    downtime_min  INTEGER,                     -- 本次停机时长（分钟，操作人填报；老板看停机统计）
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    factory_id    TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(diagnosis_id) REFERENCES diagnoses(id)
);
CREATE INDEX IF NOT EXISTS idx_fb_diag ON feedback(diagnosis_id);

-- 排查步骤勾选：界面勾选状态要能刷新不丢失，因此落库存到服务端
CREATE TABLE IF NOT EXISTS step_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id  INTEGER NOT NULL,
    step_key      TEXT NOT NULL,
    checked       INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(diagnosis_id, step_key)
);
CREATE INDEX IF NOT EXISTS idx_chk_diag ON step_checks(diagnosis_id);

-- L3 危险步骤安全确认留痕（服务端强制闸门）：
--   这是「人工确认」的记录，不是「设备断电检测」的结果。只读数据源拿不到主电源与
--   挂锁状态，确认内容由现场持证人员作出并署名；系统只保证未确认时 L3 步骤不可标记完成，
--   并把确认人/时间落库可追溯。物理安全由设备侧硬回路与现场 LOTO 规程承担。
--   租户隔离通过 diagnosis_id 归属继承（与 step_checks 一致）。
CREATE TABLE IF NOT EXISTS l3_acks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id   INTEGER NOT NULL,
    step_key       TEXT NOT NULL,
    ack_by         TEXT NOT NULL,          -- 确认人（持证人员姓名/工号），必填
    ack_at         TEXT NOT NULL,          -- 确认时间
    mode           TEXT NOT NULL DEFAULT 'manual',  -- manual=人工确认 / signal=隔离信号（二期）
    signal_source  TEXT NOT NULL DEFAULT '',        -- signal 模式必须填真实信号来源，禁止伪造
    UNIQUE(diagnosis_id, step_key)
);
CREATE INDEX IF NOT EXISTS idx_l3ack_diag ON l3_acks(diagnosis_id);
"""

# 通用故障兜底条目前缀：统计"真实故障"时排除（知识库未覆盖/数据正常的观察建议不是故障）
_FALLBACK_PREFIX = "KB-GEN"

# 导出列（CSV 与 Excel 共用；顺序即老板视角的阅读顺序）
EXPORT_HEADERS = [
    "记录号", "时间", "工厂", "设备编号", "设备名称", "故障编号", "故障名称", "报警码",
    "处置等级", "处理结果", "处理时长(分钟)", "停机时长(分钟)", "操作人", "反馈人",
    "诊断模式", "当时描述", "结论", "反馈备注",
]


class MemoryStore:
    def __init__(self, db_path: Optional[Path] = None, tenant_id: Optional[str] = None) -> None:
        self.db_path = Path(db_path or settings().db_path)
        self.tenant_id = tenant_id or settings().tenant_id or "default"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # ---------- 迁移：为早期已存在的库补齐新增列（ALTER TABLE，不重建表、不丢数据） ----------
    def _migrate(self) -> None:
        diag_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(diagnoses)")}
        diag_adds = {
            "steps_json": "ALTER TABLE diagnoses ADD COLUMN steps_json TEXT",
            "factory_id": "ALTER TABLE diagnoses ADD COLUMN factory_id TEXT NOT NULL DEFAULT ''",
            "tenant_id": "ALTER TABLE diagnoses ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
            "operator": "ALTER TABLE diagnoses ADD COLUMN operator TEXT NOT NULL DEFAULT ''",
            "alarm_codes": "ALTER TABLE diagnoses ADD COLUMN alarm_codes TEXT NOT NULL DEFAULT ''",
        }
        for col, ddl in diag_adds.items():
            if col not in diag_cols:
                self.conn.execute(ddl)

        fb_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(feedback)")}
        fb_adds = {
            "operator": "ALTER TABLE feedback ADD COLUMN operator TEXT NOT NULL DEFAULT ''",
            "handling_min": "ALTER TABLE feedback ADD COLUMN handling_min INTEGER",
            "downtime_min": "ALTER TABLE feedback ADD COLUMN downtime_min INTEGER",
            "tenant_id": "ALTER TABLE feedback ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
            "factory_id": "ALTER TABLE feedback ADD COLUMN factory_id TEXT NOT NULL DEFAULT ''",
        }
        for col, ddl in fb_adds.items():
            if col not in fb_cols:
                self.conn.execute(ddl)

        # 多工厂/多租户复合索引：等报表查询按"租户+工厂+时间"过滤（加列之后才可建）
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_diag_tenant ON diagnoses(tenant_id, factory_id, created_at DESC)")

        # 回填 factory_id：先按"设备当前所属工厂"逐台补齐（配置即事实来源），
        # 配置里已不存在的设备归入默认工厂；tenant_id 一律回填 default（本阶段单租户）。
        try:
            from . import catalog as _catalog
            for dev in _catalog.devices():
                self.conn.execute(
                    "UPDATE diagnoses SET factory_id=? WHERE device_id=? AND (factory_id='' OR factory_id IS NULL)",
                    (dev.factory_id, dev.id))
            default_factory = _catalog.DEFAULT_FACTORY_ID
        except Exception:
            default_factory = "F001"
        self.conn.execute(
            "UPDATE diagnoses SET factory_id=? WHERE factory_id='' OR factory_id IS NULL",
            (default_factory,))
        # feedback 的工厂/租户从所属诊断复制（反规范化，便于按工厂直接统计）
        self.conn.execute(
            "UPDATE feedback SET factory_id=(SELECT d.factory_id FROM diagnoses d "
            "WHERE d.id=feedback.diagnosis_id) WHERE factory_id='' OR factory_id IS NULL")
        self.conn.execute(
            "UPDATE feedback SET tenant_id=(SELECT d.tenant_id FROM diagnoses d "
            "WHERE d.id=feedback.diagnosis_id) WHERE tenant_id='' OR tenant_id IS NULL")
        self.conn.execute("UPDATE diagnoses SET tenant_id=? WHERE tenant_id='' OR tenant_id IS NULL",
                          (self.tenant_id,))
        self.conn.execute("UPDATE feedback SET tenant_id=? WHERE tenant_id='' OR tenant_id IS NULL",
                          (self.tenant_id,))

    # ---------- 工厂目录 ----------
    def upsert_factory(self, factory_id: str, name: str = "") -> None:
        """登记工厂（配置驱动的目录入库；已存在则不覆盖，避免多配置互相改写）。"""
        if not factory_id:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO factories (id, name, tenant_id, created_at) VALUES (?,?,?,?)",
            (factory_id, name or factory_id, self.tenant_id,
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def factory_record_counts(self) -> Dict[str, int]:
        """各工厂的运维记录数（供前端工厂筛选器显示规模）。"""
        rows = self.conn.execute(
            "SELECT factory_id, COUNT(*) AS n FROM diagnoses WHERE tenant_id=? GROUP BY factory_id",
            (self.tenant_id,)).fetchall()
        return {r["factory_id"]: int(r["n"]) for r in rows}

    def known_factories(self) -> List[Dict[str, Any]]:
        """记忆库里已登记的工厂（配置目录之外的历史/演示工厂，前端筛选器要能选到）。"""
        rows = self.conn.execute(
            "SELECT id, name, tenant_id FROM factories WHERE tenant_id=? ORDER BY id",
            (self.tenant_id,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "tenant_id": r["tenant_id"]} for r in rows]

    # ---------- 写入 ----------
    def save_diagnosis(self, record: Dict[str, Any]) -> int:
        """写入一次诊断，返回记录 id（供后续反馈回填）。"""
        device_id = record.get("device_id", "")
        factory_id = record.get("factory_id") or self._factory_of(device_id)
        self.upsert_factory(factory_id, self._factory_name(factory_id))
        sql = """
        INSERT INTO diagnoses (created_at, device_id, device_name, device_type, query,
                               parsed_json, snapshot_json, evidence_json, steps_json, conclusion,
                               fault_id, fault_title, kb_ids, max_level, signature, mode, llm_used,
                               factory_id, tenant_id, operator, alarm_codes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        cur = self.conn.execute(sql, (
            record.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            device_id,
            record.get("device_name", ""),
            record.get("device_type", ""),
            record.get("query", ""),
            json.dumps(record.get("parsed", {}), ensure_ascii=False),
            json.dumps(record.get("snapshot", {}), ensure_ascii=False),
            json.dumps(record.get("evidence", []), ensure_ascii=False),
            json.dumps(record.get("steps", []), ensure_ascii=False),
            record.get("conclusion", ""),
            record.get("fault_id", ""),
            record.get("fault_title", ""),
            json.dumps(record.get("kb_ids", []), ensure_ascii=False),
            record.get("max_level", ""),
            record.get("signature", ""),
            record.get("mode", "rule"),
            1 if record.get("llm_used") else 0,
            factory_id,
            record.get("tenant_id") or self.tenant_id,
            record.get("operator") or settings().default_operator or "",
            json.dumps(record.get("alarm_codes", []), ensure_ascii=False),
        ))
        self.conn.commit()
        return int(cur.lastrowid)

    def add_feedback(self, diagnosis_id: int, resolved: bool, comment: str = "",
                     operator: str = "", handling_min: Optional[int] = None,
                     downtime_min: Optional[int] = None,
                     created_at: Optional[str] = None) -> int:
        """反馈回填：处理结果 + 操作人 + 处理/停机时长（后两项为老板视角的停机统计提供数据）。

        created_at 可显式指定（ISO 字符串）：默认取当前时间；测试与历史数据导入时
        需要可复现/可回溯的时间戳，故留此参数（不改变产品默认行为）。
        """
        row = self.conn.execute(
            "SELECT factory_id, tenant_id FROM diagnoses WHERE id=?", (diagnosis_id,)).fetchone()
        factory_id = row["factory_id"] if row else ""
        tenant_id = row["tenant_id"] if row else self.tenant_id
        cur = self.conn.execute(
            "INSERT INTO feedback (diagnosis_id, resolved, comment, created_at, "
            "operator, handling_min, downtime_min, tenant_id, factory_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (diagnosis_id, 1 if resolved else 0, comment,
             created_at or datetime.now().isoformat(timespec="seconds"),
             operator or settings().default_operator or "",
             handling_min, downtime_min, tenant_id, factory_id))
        self.conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _factory_of(device_id: str) -> str:
        try:
            from . import catalog
            return catalog.factory_of(device_id)
        except Exception:
            return "F001"

    @staticmethod
    def _factory_name(factory_id: str) -> str:
        try:
            from . import catalog
            return catalog.factory_name(factory_id)
        except Exception:
            return factory_id

    # ---------- 查询 ----------
    # 注意：diagnoses 与 feedback 的联查必须把 feedback 侧的新列
    # （operator/handling_min/downtime_min/created_at）一并选出，否则历史接口与
    # "处理时长/停机时长"统计会拿到 None（自测 I-04 暴露的问题）。
    # 联查方式：每条诊断只取**最新一条**反馈（MAX(f.id)）。用普通 LEFT JOIN 时，
    # 同一条诊断被重复反馈会产生多行，导致统计计数翻倍。
    _JOIN_COLS = ("f.resolved AS resolved, f.comment AS fb_comment, "
                  "f.operator AS fb_operator, f.handling_min AS handling_min, "
                  "f.downtime_min AS downtime_min, f.created_at AS fb_created_at")
    _JOIN = ("LEFT JOIN feedback f ON f.id = "
             "(SELECT MAX(f2.id) FROM feedback f2 WHERE f2.diagnosis_id = d.id)")

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        # JSON 列统一反序列化：rows 里既有字符串也有已解析对象的情况都覆盖到
        for key in ("parsed_json", "snapshot_json", "evidence_json", "steps_json"):
            if key in item and item[key]:
                try:
                    item[key[:-5]] = json.loads(item[key])
                except (json.JSONDecodeError, TypeError):
                    item[key[:-5]] = [] if key == "steps_json" else {}
        for key in ("kb_ids", "alarm_codes"):
            if isinstance(item.get(key), str) and item[key]:
                try:
                    item[key] = json.loads(item[key])
                except (json.JSONDecodeError, TypeError):
                    item[key] = [item[key]]
            elif key in item and item[key] is None:
                item[key] = []
        return item

    def _joined_rows(self, *, device_id: Optional[str] = None, factory_id: Optional[str] = None,
                     days: Optional[int] = None, fault_only: bool = False,
                     limit: int = 5000) -> List[Dict[str, Any]]:
        """诊断 + 反馈联查（本租户范围内；可按工厂/设备/时间窗过滤）。

        所有统计口径都走这一个入口，保证"页面上看到的数"与"导出表里的数"同源。
        """
        sql = (f"SELECT d.*, {self._JOIN_COLS} "
               f"FROM diagnoses d {self._JOIN} WHERE 1=1")
        params: List[Any] = [self.tenant_id]
        sql += " AND d.tenant_id=?"
        if device_id:
            sql += " AND d.device_id=?"
            params.append(device_id)
        if factory_id:
            sql += " AND d.factory_id=?"
            params.append(factory_id)
        if days:
            cutoff = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
            sql += " AND d.created_at>=?"
            params.append(cutoff)
        if fault_only:
            sql += " AND d.fault_id<>'' AND d.fault_id NOT LIKE ?"
            params.append(f"{_FALLBACK_PREFIX}%")
        sql += " ORDER BY d.id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def handling_minutes(row: Dict[str, Any]) -> Optional[int]:
        """处理时长：优先操作人填报值；缺省按"反馈时间 - 诊断时间"推算（分钟，≥0）。"""
        hm = row.get("handling_min")
        if hm is not None:
            try:
                return max(0, int(hm))
            except (TypeError, ValueError):
                return None
        fb_at, created = row.get("fb_created_at"), row.get("created_at")
        if fb_at and created:
            try:
                delta = (datetime.fromisoformat(fb_at) - datetime.fromisoformat(created)).total_seconds()
                return max(0, int(round(delta / 60.0)))
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def result_of(row: Dict[str, Any]) -> str:
        resolved = row.get("resolved")
        if resolved is None:
            return "未反馈"
        return "已解决" if int(resolved) == 1 else "未解决"

    def get(self, diagnosis_id: int) -> Optional[Dict[str, Any]]:
        """单条诊断（含最新一条反馈）：历史详情页要展示处理备注/时长，必须联查 feedback。"""
        row = self.conn.execute(
            f"SELECT d.*, {self._JOIN_COLS} FROM diagnoses d {self._JOIN} "
            "WHERE d.id=? AND d.tenant_id=?",
            (diagnosis_id, self.tenant_id)).fetchone()
        return self._row_to_dict(row) if row else None

    def device_history(self, device_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """该设备的历史诊断（含最新反馈结果）。"""
        rows = self.conn.execute(
            f"SELECT d.*, {self._JOIN_COLS} FROM diagnoses d {self._JOIN} "
            "WHERE d.device_id=? AND d.tenant_id=? ORDER BY d.id DESC LIMIT ?",
            (device_id, self.tenant_id, limit)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def same_signature_history(self, device_id: str, signature: str,
                               exclude_id: Optional[int] = None, limit: int = 3) -> List[Dict[str, Any]]:
        """同类故障（同一指纹）的历史记录，用于第二次发生时主动引用。"""
        sql = (f"SELECT d.*, {self._JOIN_COLS} FROM diagnoses d {self._JOIN} "
               "WHERE d.device_id=? AND d.signature=? AND d.tenant_id=?")
        params: List[Any] = [device_id, signature, self.tenant_id]
        if exclude_id is not None:
            sql += " AND d.id<>?"
            params.append(exclude_id)
        sql += " ORDER BY d.id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def fault_feedback_stats(self, device_id: str, fault_id: str) -> Tuple[int, int]:
        """统计某设备某故障的历史反馈：返回 (解决了次数, 没解决次数)。"""
        row = self.conn.execute(
            "SELECT SUM(CASE WHEN f.resolved=1 THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN f.resolved=0 THEN 1 ELSE 0 END) AS ng "
            "FROM diagnoses d JOIN feedback f ON f.diagnosis_id=d.id "
            "WHERE d.device_id=? AND d.fault_id=? AND d.tenant_id=?",
            (device_id, fault_id, self.tenant_id)).fetchone()
        ok = int(row["ok"] or 0) if row else 0
        ng = int(row["ng"] or 0) if row else 0
        return ok, ng

    # ---------- 步骤勾选 ----------
    def set_step_check(self, diagnosis_id: int, step_key: str, checked: bool) -> None:
        """写入/更新一条步骤勾选状态（同一诊断同一 step_key 只保留最新值）。"""
        self.conn.execute(
            "INSERT INTO step_checks (diagnosis_id, step_key, checked, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(diagnosis_id, step_key) DO UPDATE SET "
            "checked=excluded.checked, updated_at=excluded.updated_at",
            (diagnosis_id, step_key, 1 if checked else 0,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def get_step_checks(self, diagnosis_id: int) -> Dict[str, bool]:
        """取某次诊断的全部勾选状态：{step_key: 是否勾选}。"""
        rows = self.conn.execute(
            "SELECT step_key, checked FROM step_checks WHERE diagnosis_id=?", (diagnosis_id,)).fetchall()
        return {r["step_key"]: bool(int(r["checked"])) for r in rows}

    # ---------- L3 安全确认留痕（服务端强制闸门） ----------
    def add_l3_ack(self, diagnosis_id: int, step_key: str, ack_by: str,
                   mode: str = "manual", signal_source: str = "") -> Dict[str, Any]:
        """写入一次 L3 安全确认，返回确认凭据（确认人/时间/模式）。

        同一诊断同一 L3 步骤重复确认会覆盖留痕（以最后一次为准），
        便于确认超时失效后重新署名。合法性校验由 API 层负责。
        """
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO l3_acks (diagnosis_id, step_key, ack_by, ack_at, mode, signal_source) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(diagnosis_id, step_key) DO UPDATE SET "
            "ack_by=excluded.ack_by, ack_at=excluded.ack_at, mode=excluded.mode, "
            "signal_source=excluded.signal_source",
            (diagnosis_id, step_key, ack_by, now, mode, signal_source))
        self.conn.commit()
        return {"step_key": step_key, "ack_by": ack_by, "ack_at": now,
                "mode": mode, "signal_source": signal_source}

    def get_l3_acks(self, diagnosis_id: int) -> Dict[str, Dict[str, Any]]:
        """取某次诊断的全部 L3 确认：{step_key: {ack_by, ack_at, mode, signal_source}}。"""
        rows = self.conn.execute(
            "SELECT step_key, ack_by, ack_at, mode, signal_source FROM l3_acks "
            "WHERE diagnosis_id=?", (diagnosis_id,)).fetchall()
        return {r["step_key"]: {"step_key": r["step_key"], "ack_by": r["ack_by"],
                                "ack_at": r["ack_at"], "mode": r["mode"],
                                "signal_source": r["signal_source"]} for r in rows}

    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """最近若干条诊断（含反馈）。

        说明：这里必须与 device_history / same_signature_history 一样把 comment 以及
        feedback 侧的新列（操作人/处理时长/停机时长）一并 join 出来，否则历史页在
        "全部设备"筛选下会拿不到处理备注，出现同一条记录两种显示的问题。
        """
        rows = self.conn.execute(
            f"SELECT d.*, {self._JOIN_COLS} FROM diagnoses d {self._JOIN} WHERE d.tenant_id=? "
            "ORDER BY d.id DESC LIMIT ?", (self.tenant_id, limit)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ---------- 经验沉淀（统计） ----------
    def device_stats(self, device_id: Optional[str] = None, factory_id: Optional[str] = None,
                     days: Optional[int] = None) -> List[Dict[str, Any]]:
        """按设备统计：故障次数、已解决/未解决、平均处理时长、累计停机、高频故障 Top。

        返回按"记录数"降序的列表，每项：
        {device_id, device_name, factory_id, total, resolved, unresolved, no_feedback,
         avg_handling_min, total_downtime_min, top_faults: [{fault_id, fault_title, count,
         unresolved, avg_handling_min, last_at}]}
        """
        rows = self._joined_rows(device_id=device_id, factory_id=factory_id, days=days)
        by_dev: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            dev = by_dev.setdefault(r.get("device_id", ""), {
                "device_id": r.get("device_id", ""),
                "device_name": r.get("device_name") or r.get("device_id", ""),
                "factory_id": r.get("factory_id", ""),
                "total": 0, "resolved": 0, "unresolved": 0, "no_feedback": 0,
                "handling": [], "downtime": 0, "faults": {},
            })
            dev["total"] += 1
            result = self.result_of(r)
            if result == "已解决":
                dev["resolved"] += 1
            elif result == "未解决":
                dev["unresolved"] += 1
            else:
                dev["no_feedback"] += 1
            hm = self.handling_minutes(r)
            if hm is not None:
                dev["handling"].append(hm)
            dev["downtime"] += int(r.get("downtime_min") or 0)
            fid = r.get("fault_id") or ""
            if fid and not fid.startswith(_FALLBACK_PREFIX):
                f = dev["faults"].setdefault(fid, {
                    "fault_id": fid, "fault_title": r.get("fault_title") or fid,
                    "count": 0, "unresolved": 0, "handling": [], "last_at": "",
                })
                f["count"] += 1
                if result == "未解决":
                    f["unresolved"] += 1
                if hm is not None:
                    f["handling"].append(hm)
                if (r.get("created_at") or "") > f["last_at"]:
                    f["last_at"] = r.get("created_at") or ""

        out = []
        for dev in by_dev.values():
            faults = sorted(dev["faults"].values(), key=lambda f: -f["count"])
            top_faults = [{
                "fault_id": f["fault_id"], "fault_title": f["fault_title"], "count": f["count"],
                "unresolved": f["unresolved"],
                "avg_handling_min": (round(sum(f["handling"]) / len(f["handling"]), 1)
                                     if f["handling"] else None),
                "last_at": f["last_at"],
            } for f in faults[:5]]
            out.append({
                "device_id": dev["device_id"], "device_name": dev["device_name"],
                "factory_id": dev["factory_id"], "total": dev["total"],
                "resolved": dev["resolved"], "unresolved": dev["unresolved"],
                "no_feedback": dev["no_feedback"],
                "avg_handling_min": (round(sum(dev["handling"]) / len(dev["handling"]), 1)
                                     if dev["handling"] else None),
                "total_downtime_min": dev["downtime"],
                "top_faults": top_faults,
            })
        out.sort(key=lambda d: -d["total"])
        return out

    def recurring_faults(self, days: int = 30, min_count: int = 2,
                         device_id: Optional[str] = None,
                         factory_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """复发故障提醒：同一设备、同一故障在时间窗内出现 ≥ min_count 次即列入。

        验收口径：同设备 30 天内同故障 ≥2 次要提醒（count≥3 升为 critical）。
        """
        rows = self._joined_rows(device_id=device_id, factory_id=factory_id,
                                 days=days, fault_only=True)
        groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in rows:
            key = (r.get("device_id", ""), r.get("fault_id", ""))
            g = groups.setdefault(key, {
                "device_id": key[0], "device_name": r.get("device_name") or key[0],
                "factory_id": r.get("factory_id", ""),
                "fault_id": key[1], "fault_title": r.get("fault_title") or key[1],
                "count": 0, "unresolved": 0, "handling": [], "downtime": 0,
                "first_at": "", "last_at": "",
            })
            g["count"] += 1
            if self.result_of(r) == "未解决":
                g["unresolved"] += 1
            hm = self.handling_minutes(r)
            if hm is not None:
                g["handling"].append(hm)
            g["downtime"] += int(r.get("downtime_min") or 0)
            created = r.get("created_at") or ""
            if not g["first_at"] or created < g["first_at"]:
                g["first_at"] = created
            if created > g["last_at"]:
                g["last_at"] = created

        out = []
        for g in groups.values():
            if g["count"] < min_count:
                continue
            severity = "critical" if g["count"] >= 3 else "warn"
            hint = (f"{days} 天内第 {g['count']} 次复发，建议复核上次处置是否彻底"
                    "（换件/对中/清滤网），必要时安排工程师做根因排查")
            if g["unresolved"]:
                hint += f"；其中 {g['unresolved']} 次未解决，禁止反复复位报警继续生产"
            out.append({
                **g,
                "severity": severity,
                "avg_handling_min": (round(sum(g["handling"]) / len(g["handling"]), 1)
                                     if g["handling"] else None),
                "hint": hint,
            })
        out.sort(key=lambda g: (-g["count"], g["last_at"]), reverse=False)
        out.sort(key=lambda g: -g["count"])
        return out

    def insights_summary(self, device_id: Optional[str] = None, factory_id: Optional[str] = None,
                         days: Optional[int] = None) -> Dict[str, Any]:
        """经验沉淀总览：记录数、解决率、平均处理时长、累计停机。"""
        rows = self._joined_rows(device_id=device_id, factory_id=factory_id, days=days)
        handling = [h for h in (self.handling_minutes(r) for r in rows) if h is not None]
        resolved = sum(1 for r in rows if self.result_of(r) == "已解决")
        unresolved = sum(1 for r in rows if self.result_of(r) == "未解决")
        no_fb = len(rows) - resolved - unresolved
        return {
            "records": len(rows), "resolved": resolved, "unresolved": unresolved,
            "no_feedback": no_fb,
            "resolve_rate": round(resolved / len(rows), 3) if rows else None,
            "avg_handling_min": round(sum(handling) / len(handling), 1) if handling else None,
            "total_downtime_min": sum(int(r.get("downtime_min") or 0) for r in rows),
        }

    def fault_summary(self, device_id: Optional[str] = None, factory_id: Optional[str] = None,
                      days: Optional[int] = None) -> List[Dict[str, Any]]:
        """按"设备 × 故障"汇总（导出 Excel 第二张表：老板看的故障与停机统计）。"""
        rows = self._joined_rows(device_id=device_id, factory_id=factory_id, days=days,
                                 fault_only=True)
        groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in rows:
            key = (r.get("device_id", ""), r.get("fault_id", ""))
            g = groups.setdefault(key, {
                "device_id": key[0], "device_name": r.get("device_name") or key[0],
                "factory_id": r.get("factory_id", ""),
                "fault_id": key[1], "fault_title": r.get("fault_title") or key[1],
                "count": 0, "resolved": 0, "unresolved": 0, "no_feedback": 0,
                "handling": [], "downtime": 0, "last_at": "",
            })
            g["count"] += 1
            result = self.result_of(r)
            if result == "已解决":
                g["resolved"] += 1
            elif result == "未解决":
                g["unresolved"] += 1
            else:
                g["no_feedback"] += 1
            hm = self.handling_minutes(r)
            if hm is not None:
                g["handling"].append(hm)
            g["downtime"] += int(r.get("downtime_min") or 0)
            if (r.get("created_at") or "") > g["last_at"]:
                g["last_at"] = r.get("created_at") or ""
        out = sorted(groups.values(), key=lambda g: (-g["count"], g["device_id"]))
        for g in out:
            g["avg_handling_min"] = (round(sum(g["handling"]) / len(g["handling"]), 1)
                                     if g["handling"] else None)
            del g["handling"]
        return out

    def export_rows(self, device_id: Optional[str] = None, factory_id: Optional[str] = None,
                    result: str = "", keyword: str = "", days: Optional[int] = None,
                    limit: int = 5000) -> List[Dict[str, Any]]:
        """导出用明细行（与历史页同一批数据，附加工厂/操作人/停机等老板视角字段）。"""
        rows = self._joined_rows(device_id=device_id, factory_id=factory_id, days=days, limit=limit)
        kw = (keyword or "").strip().lower()
        out = []
        for r in rows:
            res = self.result_of(r)
            if result and res != result:
                continue
            if kw:
                hay = " ".join(str(r.get(k, "")) for k in
                               ("device_id", "device_name", "query", "conclusion",
                                "fault_title", "operator", "fb_operator", "fb_comment")).lower()
                if kw not in hay:
                    continue
            snapshot = r.get("snapshot") or {}
            out.append({
                "id": r.get("id"),
                "created_at": r.get("created_at", ""),
                "factory_id": r.get("factory_id", ""),
                "factory_name": self._factory_name(r.get("factory_id", "")),
                "device_id": r.get("device_id", ""),
                "device_name": r.get("device_name", ""),
                "fault_id": r.get("fault_id", ""),
                "fault_title": r.get("fault_title", ""),
                "alarm_codes": r.get("alarm_codes") or snapshot.get("alarm_codes") or [],
                "max_level": r.get("max_level", ""),
                "result": res,
                "handling_min": self.handling_minutes(r),
                "downtime_min": r.get("downtime_min"),
                "operator": r.get("operator", ""),
                "fb_operator": r.get("fb_operator", ""),
                "mode": r.get("mode", ""),
                "query": r.get("query", ""),
                "conclusion": r.get("conclusion", ""),
                "comment": r.get("fb_comment", ""),
            })
        return out

    def reset(self) -> None:
        """清空记忆库（仅用于演示/测试）。"""
        self.conn.execute("DELETE FROM feedback")
        self.conn.execute("DELETE FROM diagnoses")
        self.conn.execute("DELETE FROM step_checks")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


_STORE: Optional[MemoryStore] = None


def get_memory() -> MemoryStore:
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE
