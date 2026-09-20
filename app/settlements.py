"""结算批次模块（独立表、不可变快照、幂等键）。

- 创建批次（create_batch）：复用计费预览 logic.compute_charge_preview 的区间切分、
  全覆盖校验与逐段计价（同一规则：任一账单行出现 gap / conflict 片段即整批拒绝）。
  仅当全部行均可计价时，才在独立表 settlement_batches 中写入一行不可变快照，
  快照包含：规范化请求（known_at + 账单行）、逐段费率依据（rate_id / rate / charge /
  portion）、各行金额（line_total）与总额（total_amount）；校验失败不落任何数据。
- 幂等：创建时必须携带 Idempotency-Key（幂等命名空间按合同隔离）。规范化请求的
  sha256 指纹随快照保存：同键且请求一致时回放首次快照（idempotent=true，不再计价、
  不重新落库，即使此后费率已修订也返回历史快照）；同键但请求不同抛
  IdempotencyKeyReusedError（409 IDEMPOTENCY_KEY_REUSED）。
- 查询（get_batch）：按 batch_id 取回快照；批次只追加、不修改、不删除。

结算记录保存在独立表 settlement_batches，与 rate_versions / rate_locks /
rate_revision_approvals 物理分离；其他模块与接口的行为完全不变。
"""

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Optional

from . import logic
from .db import connect
from .timeutil import iso, now_iso, parse

SCHEMA = """
-- 结算批次（v1.9 起）：独立表保存已通过全覆盖校验的结算快照，只追加、不修改、不删除。
-- idempotency_key 为客户端提供的幂等键（按合同隔离命名空间）；request_hash 为规范化
-- 请求的 sha256 指纹，用于区分“同键重放”与“同键异请求”；request_json 保存规范化
-- 请求；snapshot_json 保存完整不可变快照（逐段费率依据、各行金额、总额）。
CREATE TABLE IF NOT EXISTS settlement_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no     TEXT NOT NULL,          -- 合同号
    idempotency_key TEXT NOT NULL,          -- 客户端幂等键（同一合同内唯一）
    request_hash    TEXT NOT NULL,          -- 规范化请求指纹（sha256，重放/复用判别）
    request_json    TEXT NOT NULL,          -- 规范化请求快照 JSON（known_at + 账单行）
    snapshot_json   TEXT NOT NULL,          -- 完整快照 JSON（逐段依据/各行金额/总额）
    total_amount    REAL NOT NULL,          -- 全部行费用合计（与 snapshot_json 内一致）
    created_at      TEXT NOT NULL,          -- 快照写入时间（审计用）
    UNIQUE (contract_no, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_contract
    ON settlement_batches (contract_no, id);
"""


def init_table() -> None:
    """建结算批次表（幂等），应用启动时自动调用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------- 异常 ----------

class SettlementBatchNotFoundError(LookupError):
    """结算批次不存在（或不属于路径中的合同）。"""


class IdempotencyKeyReusedError(ValueError):
    """同一幂等键已存在，但本次规范化请求与首次请求不一致。

    existing_batch_id 为首次成功创建的批次 id。
    """

    def __init__(self, message: str, existing_batch_id: int):
        super().__init__(message)
        self.existing_batch_id = existing_batch_id


class SettlementUnsafeError(ValueError):
    """账单行在该认知时点存在 gap / conflict 片段，整批拒绝、不落库。

    preview 携带与计费预览同构的计价结果（ok=False），便于 409 响应逐行逐段说明。
    """

    def __init__(self, message: str, preview: dict):
        super().__init__(message)
        self.preview = preview


# ---------- 内部工具 ----------

def _begin_immediate(conn: sqlite3.Connection) -> None:
    """切到显式事务并立即获取写锁：串行化同键并发创建，保证幂等判别原子。"""
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")


def _normalize_request(
    known_at: Optional[datetime],
    lines: list[tuple[datetime, datetime, float]],
) -> tuple[str, list[tuple[str, str, float]], dict]:
    """规范化请求：时间统一为等宽 UTC ISO 字符串，known_at 缺省取当前时间。

    返回 (规范化 known_at, 规范化账单行三元组, 请求快照字典)。
    金额已在 HTTP 层由 Pydantic 转为 float（整数入参亦为 float）。
    """
    ka = iso(known_at) if known_at is not None else now_iso()
    norm_lines = [(iso(from_dt), iso(to_dt), base_amount)
                  for from_dt, to_dt, base_amount in lines]
    request = {
        "known_at": ka,
        "lines": [
            {"from": f, "to": t, "base_amount": base_amount}
            for f, t, base_amount in norm_lines
        ],
    }
    return ka, norm_lines, request


def _request_fingerprint(request: dict) -> str:
    """规范化请求的稳定指纹：canonical JSON 的 sha256（键排序、无空白）。"""
    canonical = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batch_outcome(row: sqlite3.Row) -> dict:
    """把库行解析为批次视图（快照 JSON 反序列化），不依赖 HTTP 层模型。"""
    return {
        "batch_id": row["id"],
        "contract_no": row["contract_no"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "snapshot": json.loads(row["snapshot_json"]),
    }


# ---------- 操作 ----------

def create_batch(
    contract_no: str,
    lines: list[tuple[datetime, datetime, float]],
    known_at: Optional[datetime],
    idempotency_key: str,
) -> dict:
    """创建结算批次：全覆盖校验通过后原子写入不可变快照。

    流程（BEGIN IMMEDIATE 单事务）：
    1. 规范化请求并计算指纹；
    2. 同合同同幂等键已存在：指纹一致 → 回放首次快照（idempotent=true，不再计价，
       即使此后费率变化也返回历史快照）；指纹不一致 → 抛 IdempotencyKeyReusedError；
    3. 新键：复用 logic.compute_charge_preview 做区间切分、全覆盖校验与逐段计价，
       任一行含 gap/conflict 段时抛 SettlementUnsafeError，事务回滚、不落库；
    4. 全部行可计价时写入规范化请求与完整快照并提交。

    区间/金额/未来认知时点等非法输入由计费预览抛 logic 中对应异常（422 口径）。
    返回 {"idempotent", "batch": 批次视图字典}。
    """
    ka, norm_lines, request = _normalize_request(known_at, lines)
    request_hash = _request_fingerprint(request)

    conn = connect()
    try:
        _begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM settlement_batches "
            "WHERE contract_no = ? AND idempotency_key = ?",
            (contract_no, idempotency_key),
        ).fetchone()
        if existing is not None:
            # 幂等判别在写事务内完成：同键并发请求拿到锁后必然看到首次记录。
            if existing["request_hash"] != request_hash:
                raise IdempotencyKeyReusedError(
                    f"幂等键 '{idempotency_key}' 已用于合同 '{contract_no}' 的结算批次 "
                    f"batch_id={existing['id']}，但本次请求与首次请求不一致，"
                    "请使用新的幂等键或与首次请求保持一致",
                    existing_batch_id=existing["id"],
                )
            conn.execute("COMMIT")
            return {"idempotent": True, "batch": _batch_outcome(existing)}

        # 复用计费预览：同一区间切分与全覆盖校验规则；传入已规范化的时间，保证
        # known_at 缺省时计价口径与快照中的规范化请求完全一致。
        preview = logic.compute_charge_preview(
            contract_no,
            [
                (parse(f), parse(t), base_amount)
                for f, t, base_amount in norm_lines
            ],
            parse(ka),
        )
        if not preview["ok"]:
            raise SettlementUnsafeError(
                f"合同 '{contract_no}' 的结算批次在认知时点 {ka} 存在 "
                "gap/conflict 账单行，整批拒绝且不落库",
                preview,
            )

        snapshot = {
            "contract_no": contract_no,
            "known_at": ka,
            "total_amount": preview["total_amount"],
            "lines": preview["lines"],
            "request": request,
        }
        created_at = now_iso()
        cursor = conn.execute(
            "INSERT INTO settlement_batches "
            "(contract_no, idempotency_key, request_hash, request_json, "
            " snapshot_json, total_amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                contract_no,
                idempotency_key,
                request_hash,
                json.dumps(request, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                preview["total_amount"],
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM settlement_batches WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        conn.execute("COMMIT")
        return {"idempotent": False, "batch": _batch_outcome(row)}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_batch(contract_no: str, batch_id: int) -> dict:
    """按 id 取回合同的结算批次快照；不存在或不属于该合同抛 SettlementBatchNotFoundError。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM settlement_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    if row is None or row["contract_no"] != contract_no:
        raise SettlementBatchNotFoundError(
            f"合同 '{contract_no}' 下结算批次 id={batch_id} 不存在"
        )
    return _batch_outcome(row)
