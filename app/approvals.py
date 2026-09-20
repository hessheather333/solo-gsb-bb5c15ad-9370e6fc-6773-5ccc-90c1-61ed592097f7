"""费率修订审批模块（独立于单笔录入的工作流）。

生命周期：pending（申请）→ approved / rejected（审核）→ executed（执行）。

- 创建申请：复用 logic.preview_rate_revision 修订预演（同一规划逻辑、全程只读），
  预演通过（ready）时把提议参数、预演签发的 state_token 与 pending 状态一起落库；
  同一合同内业务区间与既有 pending / approved 申请**相交**（左闭右开，仅端点相接
  不算）的新申请被拒绝，rejected / executed 申请不占区间。
- 审核：仅允许 pending → approved / rejected；请求须携带 expected_revision，
  与库中 revision 不一致即拒绝（乐观锁，防止并发覆盖），每次流转 revision +1。
- 执行：仅允许 approved。在**同一 SQLite 事务**内用申请保存的 state_token 调用
  logic.record_rate 条件提交，成功后标记 executed、revision +1、记录 created
  rate_id 与完整结果快照；state_token 陈旧（预演后合同状态已变）时整体回滚、
  返回 409 且申请**保留 approved**；对已 executed 的申请重复执行幂等返回原结果。

审批记录保存在独立表 rate_revision_approvals，与 rate_versions / rate_locks
物理分离；单笔 POST /rates 不经本模块、行为完全不变。
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional

from . import logic
from .db import connect
from .timeutil import now_iso, parse

# 审批状态：pending 待审核 / approved 审核通过 / rejected 已驳回 / executed 已执行
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXECUTED = "executed"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXECUTED)

# 占用业务区间、参与互斥的状态
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_APPROVED)

SCHEMA = """
-- 费率修订审批申请（v1.8 起）：独立表保存审批记录，不改动 rate_versions /
-- rate_locks 的结构与单笔录入行为。提议参数与预演签发的 state_token 在创建时
-- 冻结；revision 为审核乐观锁版本号（每次状态流转 +1）；执行成功后 created_rate_id
-- 记录新建主费率版本，execution_result 保存完整提交结果快照（供幂等重放）。
CREATE TABLE IF NOT EXISTS rate_revision_approvals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no      TEXT NOT NULL,          -- 合同号
    rate             REAL NOT NULL,          -- 提议费率值
    valid_from       TEXT NOT NULL,          -- 提议业务生效起点（含），UTC ISO 字符串
    valid_to         TEXT,                   -- 提议业务生效终点（不含），NULL 表示无限期
    recorded_at      TEXT NOT NULL,          -- 预演冻结的系统记录时间（条件提交以此为准）
    reason           TEXT,                   -- 提议修订说明
    state_token      TEXT NOT NULL,          -- 预演签发的条件提交令牌
    status           TEXT NOT NULL,          -- pending / approved / rejected / executed
    revision         INTEGER NOT NULL,       -- 乐观锁版本号，审核期望 expected_revision
    created_rate_id  INTEGER,                -- 执行成功后新建主费率版本 id
    execution_result TEXT,                   -- 执行结果快照 JSON（幂等重放返回原结果）
    review_comment   TEXT,                   -- 审核意见（approve / reject 均可附）
    reviewed_at      TEXT,                   -- 审核时间（审计用）
    executed_at      TEXT,                   -- 执行时间（审计用）
    created_at       TEXT NOT NULL,          -- 申请创建时间（审计用）
    CHECK (status IN ('pending', 'approved', 'rejected', 'executed')),
    FOREIGN KEY (created_rate_id) REFERENCES rate_versions (id)
);
CREATE INDEX IF NOT EXISTS idx_rate_revision_approvals_contract
    ON rate_revision_approvals (contract_no, id);
"""


def init_table() -> None:
    """建审批表（幂等），应用启动时自动调用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------- 异常 ----------

class ApprovalNotFoundError(LookupError):
    """审批申请不存在（或不属于路径中的合同）。"""


class ApprovalIntervalConflictError(ValueError):
    """新申请区间与同合同 pending / approved 申请相交（端点相接允许）。

    conflicts 为相交申请行列表（按 valid_from、id 排序）。
    """

    def __init__(self, message: str, conflicts: list[sqlite3.Row]):
        super().__init__(message)
        self.conflicts = conflicts


class ApprovalNotPendingError(ValueError):
    """审核目标不是 pending 状态，不允许审核。

    current_status 为库中实际状态。
    """

    def __init__(self, message: str, current_status: str):
        super().__init__(message)
        self.current_status = current_status


class ApprovalRevisionMismatchError(ValueError):
    """expected_revision 与库中 revision 不一致（并发覆盖）。"""

    def __init__(self, message: str, expected: int, actual: int):
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class ApprovalNotApprovedError(ValueError):
    """执行目标不是 approved 状态（pending 须先审核，rejected/executed 不可执行）。

    current_status 为库中实际状态（executed 由调用方按幂等分支提前处理）。
    """

    def __init__(self, message: str, current_status: str):
        super().__init__(message)
        self.current_status = current_status


# ---------- 内部工具 ----------

def _begin_immediate(conn: sqlite3.Connection) -> None:
    """切到显式事务并立即获取写锁：串行化创建/审核/执行，保证互斥与状态判断原子。"""
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")


def _find_interval_conflicts(
    conn: sqlite3.Connection,
    contract_no: str,
    vf: str,
    vt: Optional[str],
) -> list[sqlite3.Row]:
    """同合同 pending/approved 申请中与 [vf, vt) 业务区间相交者（左闭右开）。

    仅端点相接不算相交；vt 为 None 表示开放上界。按 valid_from、id 排序。
    """
    return conn.execute(
        "SELECT * FROM rate_revision_approvals "
        "WHERE contract_no = ? AND status IN ('pending', 'approved') "
        "  AND valid_from < COALESCE(?, ?) "
        "  AND COALESCE(valid_to, ?) > ? "
        "ORDER BY valid_from ASC, id ASC",
        (contract_no, vt, logic.INF, logic.INF, vf),
    ).fetchall()


def _version_payload(row: sqlite3.Row) -> dict:
    """与 main.VersionView 同构的版本字典（执行快照不依赖 HTTP 层模型）。"""
    return {
        "rate_id": row["id"],
        "contract_no": row["contract_no"],
        "rate": row["rate"],
        "version": row["version"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "recorded_at": row["recorded_at"],
        "superseded_at": row["superseded_at"],
        "status": "current" if row["superseded_at"] is None else "superseded",
        "reason": row["reason"],
    }


def _rate_created_payload(
    new_row: sqlite3.Row,
    superseded_ids: list[int],
    retained_rows: list[sqlite3.Row],
) -> dict:
    """与 main.RateCreated 同构的提交结果快照（写入 execution_result，幂等重放）。"""
    return {
        "created": _version_payload(new_row),
        "superseded_rate_ids": list(superseded_ids),
        "retained_versions": [_version_payload(r) for r in retained_rows],
    }


# ---------- 工作流操作 ----------

def create_approval(
    contract_no: str,
    rate: float,
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
    reason: Optional[str],
) -> sqlite3.Row:
    """创建费率修订审批申请。

    先复用修订预演（与 POST /rates 同一规划逻辑）：预演被锁相交 / recorded_at
    单调性阻断时抛 logic 中对应异常（不落任何申请）；预演通过则冻结提议参数与
    state_token。随后在同一写事务内校验同合同 pending/approved 申请的业务区间
    互斥（端点相接允许），通过后以 pending / revision=1 落库。
    """
    preview = logic.preview_rate_revision(
        contract_no, rate, valid_from, valid_to, recorded_at, reason
    )
    if not preview["ready"]:
        blocking = preview["blocking"]
        if blocking["code"] == "RATE_WINDOW_LOCKED":
            raise logic.RateWindowLockedError(blocking["message"], blocking["conflicts"])
        raise logic.NonMonotonicRecordTimeError(blocking["message"])

    vf = preview["valid_from"]
    vt = preview["valid_to"]
    ra = preview["recorded_at"]
    token = preview["state_token"]

    conn = connect()
    try:
        _begin_immediate(conn)
        conflicts = _find_interval_conflicts(conn, contract_no, vf, vt)
        if conflicts:
            raise ApprovalIntervalConflictError(
                f"合同 '{contract_no}' 的提议区间 [{vf}, {vt or '∞'}) 与 "
                f"{len(conflicts)} 个 pending/approved 审批申请的业务区间相交，"
                "同一合同相交区间的待办/已批准申请互斥（端点相接允许）",
                conflicts,
            )
        cursor = conn.execute(
            "INSERT INTO rate_revision_approvals "
            "(contract_no, rate, valid_from, valid_to, recorded_at, reason, "
            " state_token, status, revision, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (contract_no, rate, vf, vt, ra, reason, token,
             STATUS_PENDING, 1, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM rate_revision_approvals WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        conn.execute("COMMIT")
        return row
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def review_approval(
    contract_no: str,
    approval_id: int,
    action: str,
    expected_revision: int,
    comment: Optional[str],
) -> sqlite3.Row:
    """审核申请：仅允许 pending → approved / rejected。

    expected_revision 必须与库中 revision 一致，否则抛 ApprovalRevisionMismatchError
    （乐观锁防止并发审核互相覆盖）；流转后 revision +1 并记录审核时间与意见。
    """
    new_status = STATUS_APPROVED if action == "approve" else STATUS_REJECTED
    conn = connect()
    try:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT * FROM rate_revision_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None or row["contract_no"] != contract_no:
            raise ApprovalNotFoundError(
                f"合同 '{contract_no}' 下审批申请 id={approval_id} 不存在"
            )
        if row["status"] != STATUS_PENDING:
            raise ApprovalNotPendingError(
                f"审批申请 id={approval_id} 当前状态为 {row['status']}，"
                "仅 pending 申请可审核（pending→approved/rejected）",
                current_status=row["status"],
            )
        if row["revision"] != expected_revision:
            raise ApprovalRevisionMismatchError(
                f"审批申请 id={approval_id} 的 expected_revision={expected_revision} "
                f"与当前 revision={row['revision']} 不一致，申请可能已被并发审核",
                expected=expected_revision,
                actual=row["revision"],
            )
        conn.execute(
            "UPDATE rate_revision_approvals "
            "SET status = ?, revision = revision + 1, review_comment = ?, reviewed_at = ? "
            "WHERE id = ?",
            (new_status, comment, now_iso(), approval_id),
        )
        updated = conn.execute(
            "SELECT * FROM rate_revision_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return updated
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def execute_approval(contract_no: str, approval_id: int) -> dict:
    """执行已批准申请：approved → executed。

    在同一 SQLite 事务（BEGIN IMMEDIATE）内：
    1. 重新读取申请状态——已 executed 时直接回放 execution_result（幂等）；
    2. approved 申请用保存的 state_token 调 logic.record_rate 条件提交（同一连接 /
       事务），费率版本写入与审批状态更新原子提交；
    3. token 陈旧（预演后当前版本或锁集合变化）时 logic 抛
       StaleRevisionPreviewError，事务整体回滚、申请保留 approved；
    4. 成功后标记 executed、revision +1、记录 created_rate_id / executed_at 与
       完整结果快照。

    返回 {"idempotent", "approval": Row, "result": 结果字典}。
    """
    conn = connect()
    try:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT * FROM rate_revision_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None or row["contract_no"] != contract_no:
            raise ApprovalNotFoundError(
                f"合同 '{contract_no}' 下审批申请 id={approval_id} 不存在"
            )

        # 事务内复查 executed：并发的重复执行在拿到锁后走幂等分支而非再次提交。
        if row["status"] == STATUS_EXECUTED:
            result = json.loads(row["execution_result"])
            conn.execute("COMMIT")
            return {"idempotent": True, "approval": row, "result": result}
        if row["status"] != STATUS_APPROVED:
            raise ApprovalNotApprovedError(
                f"审批申请 id={approval_id} 当前状态为 {row['status']}，"
                "仅 approved 申请可执行",
                current_status=row["status"],
            )

        # 复用现有条件提交：state_token 绑定参数与申请冻结参数一致；指纹不符时
        # logic 抛 StaleRevisionPreviewError，本事务回滚、申请保留 approved。
        new_row, superseded_ids, retained_rows = logic.record_rate(
            contract_no=row["contract_no"],
            rate=row["rate"],
            valid_from=parse(row["valid_from"]),
            valid_to=parse(row["valid_to"]) if row["valid_to"] is not None else None,
            recorded_at=parse(row["recorded_at"]),
            reason=row["reason"],
            conn=conn,
            state_token=row["state_token"],
        )
        result = _rate_created_payload(new_row, superseded_ids, retained_rows)
        executed_at = now_iso()
        conn.execute(
            "UPDATE rate_revision_approvals "
            "SET status = 'executed', revision = revision + 1, created_rate_id = ?, "
            "    execution_result = ?, executed_at = ? "
            "WHERE id = ?",
            (new_row["id"], json.dumps(result, ensure_ascii=False),
             executed_at, approval_id),
        )
        updated = conn.execute(
            "SELECT * FROM rate_revision_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return {"idempotent": False, "approval": updated, "result": result}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def list_approvals(
    contract_no: str, status: Optional[str] = None
) -> list[sqlite3.Row]:
    """列出合同的审批申请（按 id 升序），可按状态过滤。"""
    with connect() as conn:
        if status is None:
            rows = conn.execute(
                "SELECT * FROM rate_revision_approvals WHERE contract_no = ? "
                "ORDER BY id ASC",
                (contract_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rate_revision_approvals "
                "WHERE contract_no = ? AND status = ? ORDER BY id ASC",
                (contract_no, status),
            ).fetchall()
    return list(rows)
