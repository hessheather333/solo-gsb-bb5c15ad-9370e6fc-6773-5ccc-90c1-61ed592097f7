"""结算批次模块（独立表 settlement_batches）。

POST /contracts/{contract_no}/settlement-batches 把一组账单行按 known_at 认知
时点结算为一个**不可变批次快照**：

- 复用 logic.compute_charge_preview 的区间切分与全覆盖校验（同一函数、同一
  事务内连接）：任一行出现 gap / conflict 段时整批拒绝
  （409 SETTLEMENT_BATCH_UNSAFE），不落任何数据；
- 全部行可计价时，在同一 SQLite 事务（BEGIN IMMEDIATE）内把**规范化请求**与
  **结算快照**（逐段费率依据、各行金额、总额）原子写入独立表
  settlement_batches；快照只追加、不修改、不删除，后续费率修订不影响已落库
  的批次；
- 幂等：Idempotency-Key 请求头必填，(contract_no, idempotency_key) 唯一。
  相同键且规范化请求一致时返回首次保存的快照并标记 idempotent=true（不重复
  写库）；键相同但请求不同返回 409 IDEMPOTENCY_KEY_REUSED。

结算批次保存在独立表，与 rate_versions / rate_locks / rate_revision_approvals
物理分离；计费预览（POST .../charge-preview）行为完全不变。
"""

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Optional

from . import logic
from .db import connect
from .timeutil import iso, now_iso

SCHEMA = """
-- 结算批次（v1.9 起）：独立表保存结算批次的规范化请求与不可变快照，不改动
-- rate_versions / rate_locks / rate_revision_approvals 的结构与行为。
-- (contract_no, idempotency_key) 唯一：幂等键去重；request_hash 判定"请求一致"；
-- snapshot 为计费预览通过时的完整结果 JSON（逐段费率依据、行金额、总额），
-- 写入后不可变，GET 与幂等重放均回放该快照。
CREATE TABLE IF NOT EXISTS settlement_batches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no      TEXT NOT NULL,          -- 合同号
    idempotency_key  TEXT NOT NULL,          -- 客户端幂等键（Idempotency-Key 请求头）
    request_hash     TEXT NOT NULL,          -- 规范化请求的 SHA-256（判定请求一致性）
    request_payload  TEXT NOT NULL,          -- 规范化请求 JSON（known_at + 账单行）
    snapshot         TEXT NOT NULL,          -- 不可变结算快照 JSON（逐段依据/行金额/总额）
    total_amount     REAL NOT NULL,          -- 批次总额（冗余自快照，便于审计查询）
    created_at       TEXT NOT NULL,          -- 批次写入时间（审计用）
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


class SettlementBatchUnsafeError(ValueError):
    """任一账单行存在 gap / conflict 段，整批拒绝且不落库。

    known_at / lines 携带计费预览结果，便于 409 响应逐行逐段说明拒绝原因。
    """

    def __init__(self, message: str, known_at: str, lines: list[dict]):
        super().__init__(message)
        self.known_at = known_at
        self.lines = lines


class IdempotencyKeyReusedError(ValueError):
    """Idempotency-Key 已被同合同使用，但本次规范化请求与首次不一致。

    batch_id 为首次使用该键的批次。
    """

    def __init__(self, message: str, batch_id: int):
        super().__init__(message)
        self.batch_id = batch_id


# ---------- 内部工具 ----------

def _begin_immediate(conn: sqlite3.Connection) -> None:
    """切到显式事务并立即获取写锁：串行化批次创建，保证幂等判定与快照写入原子。"""
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")


def _normalize_request_payload(
    lines: list[tuple[datetime, datetime, float]],
    known_at: Optional[datetime],
) -> tuple[str, str]:
    """规范化请求 → (请求 JSON, 请求哈希)。

    复用 logic._normalize_charge_request 的校验与规范化（与计费预览同一错误
    口径）；known_at 缺省时记为 null（"缺省"本身参与一致性判定），实际使用的
    认知时点以快照为准。返回的 JSON 为键序稳定的最紧凑形式。
    """
    _, normalized = logic._normalize_charge_request(lines, known_at)
    payload = {
        "known_at": iso(known_at) if known_at is not None else None,
        "lines": [
            {"from": f, "to": t, "base_amount": amount}
            for f, t, amount in normalized
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def batch_payload(row: sqlite3.Row) -> dict:
    """组装批次响应载荷：存储的不可变快照 + 批次元数据（POST 与 GET 共用）。"""
    snapshot = json.loads(row["snapshot"])
    return {
        "batch_id": row["id"],
        "contract_no": row["contract_no"],
        "idempotency_key": row["idempotency_key"],
        "known_at": snapshot["known_at"],
        "status": snapshot["status"],
        "total_amount": snapshot["total_amount"],
        "lines": snapshot["lines"],
        "created_at": row["created_at"],
    }


# ---------- 批次创建与查询 ----------

def create_batch(
    contract_no: str,
    known_at: Optional[datetime],
    lines: list[tuple[datetime, datetime, float]],
    idempotency_key: str,
) -> dict:
    """创建结算批次：全覆盖校验通过后，原子保存规范化请求与不可变快照。

    在同一 SQLite 事务（BEGIN IMMEDIATE）内：
    1. 按 (contract_no, idempotency_key) 查已落库批次——存在且规范化请求一致时
       直接返回首次快照（idempotent=true，不重复写库）；请求不同抛
       IdempotencyKeyReusedError；
    2. 不存在时用同一连接复用 logic.compute_charge_preview 逐行切分校验，
       任一行含 gap / conflict 段抛 SettlementBatchUnsafeError，事务回滚、
       不落任何数据；
    3. 全部行可计价时把规范化请求与结算快照（逐段费率依据、行金额、总额）
       一次插入并提交；快照此后不可变。

    返回 {"idempotent", "row": 批次行}。
    """
    request_payload, request_hash = _normalize_request_payload(lines, known_at)

    conn = connect()
    try:
        _begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM settlement_batches "
            "WHERE contract_no = ? AND idempotency_key = ?",
            (contract_no, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyKeyReusedError(
                    f"Idempotency-Key '{idempotency_key}' 已用于合同 "
                    f"'{contract_no}' 的结算批次 id={existing['id']}，"
                    "但本次请求与首次不一致，同一键不得复用于不同请求",
                    batch_id=existing["id"],
                )
            conn.execute("COMMIT")
            return {"idempotent": True, "row": existing}

        # 同事务复用计费预览：区间切分与全覆盖校验和预览口径完全一致；
        # 写锁已持有，校验与落库基于同一快照，不存在读写间隙。
        result = logic.compute_charge_preview(contract_no, lines, known_at, conn=conn)
        if not result["ok"]:
            raise SettlementBatchUnsafeError(
                f"合同 '{contract_no}' 的结算批次在认知时点 {result['known_at']} "
                "存在 gap/conflict 片段，整批拒绝、不落库",
                known_at=result["known_at"],
                lines=result["lines"],
            )

        snapshot = {
            "known_at": result["known_at"],
            "status": "ok",
            "total_amount": result["total_amount"],
            "lines": result["lines"],
        }
        cursor = conn.execute(
            "INSERT INTO settlement_batches "
            "(contract_no, idempotency_key, request_hash, request_payload, "
            " snapshot, total_amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contract_no, idempotency_key, request_hash, request_payload,
             json.dumps(snapshot, ensure_ascii=False),
             result["total_amount"], now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM settlement_batches WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        conn.execute("COMMIT")
        return {"idempotent": False, "row": row}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_batch(contract_no: str, batch_id: int) -> sqlite3.Row:
    """按 id 查询结算批次；不存在或不属于该合同抛 SettlementBatchNotFoundError。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM settlement_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    if row is None or row["contract_no"] != contract_no:
        raise SettlementBatchNotFoundError(
            f"合同 '{contract_no}' 下结算批次 id={batch_id} 不存在"
        )
    return row
