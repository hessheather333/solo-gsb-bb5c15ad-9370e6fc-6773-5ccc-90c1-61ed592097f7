"""双时态核心逻辑。

两个时间维度：
- 业务时间（valid time）  [valid_from, valid_to)：费率在现实中生效的区间，左闭右开；
- 系统时间（system time） [recorded_at, superseded_at)：该记录被系统认知为真的区间，左闭右开。

修订 = 追加新版本并把被覆盖旧版本的交集标记 superseded_at，交集之外的左、右
部分按原费率与 reason 追加补偿版本；历史行永不修改、永不删除。

版本血缘（rate_version_edges）随修订在同一事务写入：新费率对每个被取代版本建立
supersedes 边，补偿片段对其来源旧版本建立 retained_from 边；边方向为
后继版本 -> 祖先版本，没有出边的版本视为血缘根。

修订预演（preview_rate_revision）与正式录入（record_rate）共用同一规划逻辑
（_plan_rate_revision：锁相交优先 → recorded_at 单调性 → 部分重叠补偿 → 版本号
递增）；预演全程只读并签发 state_token，携带 token 的提交仅当合同当前版本集合
与锁集合自预演以来未变化时才落库，否则整体拒绝、不写任何数据。

最惠价合规审计（compute_most_favored_audit）按同一认知时点读取目标合同与
同业合同的可见费率，以全部业务边界的并集切分审计窗口：目标与所有同业均唯一
覆盖时，以同业最低费率为 benchmark 判定 compliant / violation；任一合同出现
gap / conflict 时判 inconclusive 并列出原因，绝不错判合规。审计全程只读。
"""

import base64
import hashlib
import hmac
import json
import math
import os
import sqlite3
from collections import deque
from datetime import datetime
from typing import Optional

from .db import connect
from .timeutil import iso, now_iso, parse

# 无限期区间的上界占位（仅用于 SQL 比较）
INF = "9999-12-31T23:59:59.999999+00:00"


class InvalidIntervalError(ValueError):
    """区间终点不晚于起点（valid_to <= valid_from，或查询窗口 from >= to）。"""


class NonMonotonicRecordTimeError(ValueError):
    """recorded_at 早于该合同已有记录，破坏系统时间单调性。"""


class InvalidAmountError(ValueError):
    """账单行基准金额为负数或非有限数值。"""


class FutureKnownAtError(ValueError):
    """known_at 晚于当前时间，认知时点取自未来。"""


class InvalidKnownAtOrderError(ValueError):
    """before_known_at 不早于 after_known_at，认知时点顺序非法。"""


class RateNotFoundError(LookupError):
    """指定 rate_id 的版本不存在。"""


class LineageCorruptError(ValueError):
    """血缘图损坏：存在环（不再是 DAG）或悬空边（祖先版本不存在）。"""


class RateLockUnsafeError(ValueError):
    """锁定窗口在该认知时点并非全部 covered（存在 gap 或 conflict），锁不落库。

    segments 携带覆盖率切分结果，便于 409 响应逐段说明不安全原因。
    """

    def __init__(self, message: str, segments: list[dict]):
        super().__init__(message)
        self.segments = segments


class RateWindowLockedError(ValueError):
    """拟议费率区间与既有结算期锁片段相交，修订被拒绝。

    conflicts 为每个相交锁 {"lock", "intersection": {"from", "to"}} 列表。
    """

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.conflicts = conflicts


class InvalidStateTokenError(ValueError):
    """state_token 非法：格式/签名错误、缺少必要字段，或与本次录入参数不一致。"""


class StaleRevisionPreviewError(ValueError):
    """携带 state_token 提交时，相关当前版本或锁集合在预演后已变化，预演失效。"""


class InvalidMarkupError(ValueError):
    """max_markup 为负数或非有限数值。"""


class InvalidPeerContractsError(ValueError):
    """同业合同列表非法：为空、混入目标合同或含重复合同。

    code 为稳定错误码：EMPTY_PEER_CONTRACTS / TARGET_IN_PEER_CONTRACTS /
    DUPLICATE_PEER_CONTRACT。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _normalize_revision(
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
) -> tuple[str, Optional[str], str]:
    """规范化录入/预演的时间参数为等宽 ISO 字符串；区间非法抛 InvalidIntervalError。"""
    vf = iso(valid_from)
    vt = iso(valid_to) if valid_to is not None else None
    ra = iso(recorded_at) if recorded_at is not None else now_iso()
    if vt is not None and vt <= vf:
        raise InvalidIntervalError("valid_to 必须晚于 valid_from")
    return vf, vt, ra


def _plan_rate_revision(
    conn: sqlite3.Connection,
    contract_no: str,
    vf: str,
    vt: Optional[str],
    ra: str,
) -> dict:
    """计算费率修订计划（只读，不写库）；预演与正式录入共用，保证结果一致。

    校验顺序：结算期锁相交优先，其次 recorded_at 单调性；任一不满足即抛错。
    返回 {"next_version", "overlaps", "pieces"}：overlaps 为将被取代的当前版本行
    （按 valid_from、id 排序）；pieces 为补偿片段 (片段起点, 片段终点, 源顺序, 源版本行)
    列表，按 (片段起点, 源顺序) 排序。
    """
    # 结算期锁优先：拟议区间与任一锁定片段相交（左闭右开，仅相接不算）时
    # 直接拒绝，此时尚未写入任何新版本、补偿片段或血缘边。
    conflicts = _find_lock_conflicts(conn, contract_no, vf, vt)
    if conflicts:
        raise RateWindowLockedError(
            f"合同 '{contract_no}' 的拟议区间 [{vf}, {vt or '∞'}) 与 "
            f"{len(conflicts)} 个结算期费率锁片段相交，锁定窗口内不得修订费率",
            conflicts,
        )

    state = conn.execute(
        "SELECT MAX(recorded_at) AS max_ra, COALESCE(MAX(version), 0) AS max_v "
        "FROM rate_versions WHERE contract_no = ?",
        (contract_no,),
    ).fetchone()
    if state["max_ra"] is not None and ra < state["max_ra"]:
        raise NonMonotonicRecordTimeError(
            f"recorded_at={ra} 早于合同 {contract_no} 已有记录时间 {state['max_ra']}，"
            "系统时间必须单调递增"
        )

    overlaps = conn.execute(
        "SELECT * FROM rate_versions "
        "WHERE contract_no = ? AND superseded_at IS NULL "
        "  AND valid_from < COALESCE(?, ?) "
        "  AND COALESCE(valid_to, ?) > ? "
        "ORDER BY valid_from ASC, id ASC",
        (contract_no, vt, INF, INF, vf),
    ).fetchall()

    # 逐条计算交集之外的补偿片段：(valid_from, valid_to, 源顺序, 旧版本行)。
    # 左段严格以 vf 为终点、右段严格以 vt 为起点，故片段必非空；
    # 新区间开放上界（vt IS NULL）时不存在右段。
    pieces: list[tuple[str, Optional[str], int, sqlite3.Row]] = []
    for order, old in enumerate(overlaps):
        if old["valid_from"] < vf:
            pieces.append((old["valid_from"], vf, order, old))
        if vt is not None and (old["valid_to"] is None or vt < old["valid_to"]):
            pieces.append((vt, old["valid_to"], order, old))
    pieces.sort(key=lambda p: (p[0], p[2]))

    return {
        "next_version": state["max_v"] + 1,
        "overlaps": list(overlaps),
        "pieces": pieces,
    }


def _apply_rate_revision(
    conn: sqlite3.Connection,
    contract_no: str,
    rate: float,
    vf: str,
    vt: Optional[str],
    ra: str,
    reason: Optional[str],
    plan: dict,
) -> tuple[sqlite3.Row, list[int], list[sqlite3.Row]]:
    """执行已规划的修订：新版本、补偿片段、取代标记与血缘边。

    调用方保证这些写入处于同一事务；返回 (新版本行, 被取代的版本 id 列表, 补偿版本行列表)。
    """
    # 先创建新费率，version 严格递增；补偿片段按 valid_from 顺序紧随其后。
    next_version = plan["next_version"]
    cursor = conn.execute(
        "INSERT INTO rate_versions "
        "(contract_no, rate, valid_from, valid_to, recorded_at, version, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (contract_no, rate, vf, vt, ra, next_version, reason),
    )
    new_row = conn.execute(
        "SELECT * FROM rate_versions WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()

    retained_rows: list[sqlite3.Row] = []
    for piece_vf, piece_vt, _, old in plan["pieces"]:
        piece_cursor = conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                contract_no,
                old["rate"],
                piece_vf,
                piece_vt,
                ra,
                next_version + 1 + len(retained_rows),
                old["reason"],
            ),
        )
        retained_id = piece_cursor.lastrowid
        retained_rows.append(
            conn.execute(
                "SELECT * FROM rate_versions WHERE id = ?",
                (retained_id,),
            ).fetchone()
        )
        # 补偿片段 -> 切出它的来源旧版本（retained_from）
        conn.execute(
            "INSERT INTO rate_version_edges "
            "(from_rate_id, to_rate_id, relation, recorded_at) "
            "VALUES (?, ?, 'retained_from', ?)",
            (retained_id, old["id"], ra),
        )

    # 取代标记与新增行在同一事务提交，任一失败整体回滚。
    superseded_ids = [row["id"] for row in plan["overlaps"]]
    for version_id in superseded_ids:
        conn.execute(
            "UPDATE rate_versions SET superseded_at = ? WHERE id = ?",
            (ra, version_id),
        )
        # 新费率 -> 本次被取代的每个旧版本（supersedes）
        conn.execute(
            "INSERT INTO rate_version_edges "
            "(from_rate_id, to_rate_id, relation, recorded_at) "
            "VALUES (?, ?, 'supersedes', ?)",
            (new_row["id"], version_id, ra),
        )
    return new_row, superseded_ids, retained_rows


def record_rate(
    contract_no: str,
    rate: float,
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
    reason: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
    state_token: Optional[str] = None,
) -> tuple[sqlite3.Row, list[int], list[sqlite3.Row]]:
    """录入费率（追加式）。

    若新区间 [vf, vt) 与当前认知中（superseded_at IS NULL）的既有版本重叠，
    仅用新版本替换交集部分：每条被覆盖的旧版本在 recorded_at 时刻标记被取代，
    交集之外未被覆盖的左段 [old.vf, vf)、右段 [vt, old.vt)（含开放上界情形）
    按旧版本原费率与 reason 追加为补偿版本。因此修订窗口之外的报价保持不变，
    不会产生 gap。

    取代标记、新版本、补偿版本在同一事务内完成；版本号严格递增，新版本在前，
    补偿片段按 valid_from 排序（同点按被覆盖版本的原始顺序）在后。血缘边也在
    同一事务写入：新版本对每个被取代版本建 supersedes 边，每个补偿片段对其
    来源旧版本建 retained_from 边。

    携带 state_token（修订预演签发）时为条件提交：先校验 token 签名与绑定参数
    （contract_no / rate / 区间 / reason；recorded_at 缺省时以 token 绑定的预演
    时间为准），再比对合同状态指纹——当前版本集合、版本号/记录时间水位与锁集合
    自预演以来有任何变化即抛 StaleRevisionPreviewError，不产生任何写入；
    指纹一致时才按同一规划逻辑提交，结果与预演完全一致。

    返回 (新版本行, 被取代的版本 id 列表, 补偿版本行列表)。
    """
    vf, vt, ra = _normalize_revision(valid_from, valid_to, recorded_at)

    token_payload = None
    if state_token is not None:
        token_payload = _decode_state_token(state_token)
        bound = {
            "contract_no": contract_no,
            "rate": rate,
            "valid_from": vf,
            "valid_to": vt,
            "reason": reason,
        }
        for key, value in bound.items():
            if token_payload[key] != value:
                raise InvalidStateTokenError(
                    f"state_token 绑定的 {key}={token_payload[key]!r} "
                    f"与本次录入参数 {value!r} 不一致"
                )
        if recorded_at is not None and token_payload["recorded_at"] != ra:
            raise InvalidStateTokenError(
                f"state_token 绑定的 recorded_at={token_payload['recorded_at']!r} "
                f"与本次录入参数 {ra!r} 不一致"
            )
        # recorded_at 缺省时以 token 绑定的预演时间为准，保证提交与预演完全一致
        ra = token_payload["recorded_at"]

    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        if token_payload is not None:
            current_fingerprint = _contract_state_fingerprint(conn, contract_no)
            if current_fingerprint != token_payload["fingerprint"]:
                raise StaleRevisionPreviewError(
                    f"合同 '{contract_no}' 的当前版本集合或锁集合在预演后已变化，"
                    "state_token 已失效，请重新预演后再提交"
                )
        plan = _plan_rate_revision(conn, contract_no, vf, vt, ra)
        result = _apply_rate_revision(conn, contract_no, rate, vf, vt, ra, reason, plan)
        if own_conn:
            conn.commit()
        return result
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


# ---------- 修订预演与条件提交 ----------

def _contract_state_fingerprint(conn: sqlite3.Connection, contract_no: str) -> str:
    """合同级规划状态指纹：当前版本集合、版本号/记录时间水位与锁集合的哈希。

    预演签发 state_token 时计算一次，携带 token 提交时重算比对；任何会影响
    规划结果的状态变化（新版本、取代标记、新锁）都会改变指纹。
    """
    state = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS max_v, MAX(recorded_at) AS max_ra "
        "FROM rate_versions WHERE contract_no = ?",
        (contract_no,),
    ).fetchone()
    currents = conn.execute(
        "SELECT id, version, rate, valid_from, valid_to, recorded_at, reason "
        "FROM rate_versions WHERE contract_no = ? AND superseded_at IS NULL "
        "ORDER BY id ASC",
        (contract_no,),
    ).fetchall()
    locks = conn.execute(
        "SELECT id, valid_from, valid_to, known_at, basis_rate_id, reason "
        "FROM rate_locks WHERE contract_no = ? ORDER BY id ASC",
        (contract_no,),
    ).fetchall()
    h = hashlib.sha256()
    h.update(repr(("state", state["max_v"], state["max_ra"])).encode("utf-8"))
    for row in currents:
        h.update(repr(("version", *tuple(row))).encode("utf-8"))
    for row in locks:
        h.update(repr(("lock", *tuple(row))).encode("utf-8"))
    return h.hexdigest()


def _token_secret() -> bytes:
    """state_token 的 HMAC 密钥：环境变量 STATE_TOKEN_SECRET，缺省为开发用固定值。"""
    return os.environ.get(
        "STATE_TOKEN_SECRET", "rates-dev-state-token-secret"
    ).encode("utf-8")


def _issue_state_token(payload: dict) -> str:
    """把预演绑定参数与状态指纹签名成无状态令牌：base64url(json).hmac_sha256。"""
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_token_secret(), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _decode_state_token(token: str) -> dict:
    """校验并解析 state_token；格式、签名或字段非法时抛 InvalidStateTokenError。"""
    try:
        raw, sig = token.rsplit(".", 1)
        expected = hmac.new(
            _token_secret(), raw.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise InvalidStateTokenError("state_token 签名校验失败")
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except InvalidStateTokenError:
        raise
    except Exception:
        raise InvalidStateTokenError("state_token 格式非法或内容无法解析") from None
    required = {"v", "contract_no", "rate", "valid_from", "valid_to",
                "recorded_at", "reason", "fingerprint"}
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or not required.issubset(payload)
    ):
        raise InvalidStateTokenError("state_token 缺少必要字段或版本不受支持")
    return payload


def preview_rate_revision(
    contract_no: str,
    rate: float,
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
    reason: Optional[str],
) -> dict:
    """费率修订预演：与 record_rate 共用同一规划逻辑，全程只读、不写库。

    返回 {"ready", "valid_from", "valid_to", "recorded_at", ...}：
    - ready=True：附 "plan"（_plan_rate_revision 的计划：主版本版本号、被取代
      版本、补偿片段）与 "state_token"（绑定本次参数与合同状态指纹，供
      POST /rates 条件提交）；
    - ready=False：附 "blocking"（{"code", "message", "conflicts"?}），code 为
      RATE_WINDOW_LOCKED（锁相交优先）或 NON_MONOTONIC_RECORDED_AT。
    """
    vf, vt, ra = _normalize_revision(valid_from, valid_to, recorded_at)
    conn = connect()
    try:
        try:
            plan = _plan_rate_revision(conn, contract_no, vf, vt, ra)
        except RateWindowLockedError as exc:
            return {
                "ready": False, "valid_from": vf, "valid_to": vt, "recorded_at": ra,
                "blocking": {
                    "code": "RATE_WINDOW_LOCKED",
                    "message": str(exc),
                    "conflicts": exc.conflicts,
                },
            }
        except NonMonotonicRecordTimeError as exc:
            return {
                "ready": False, "valid_from": vf, "valid_to": vt, "recorded_at": ra,
                "blocking": {
                    "code": "NON_MONOTONIC_RECORDED_AT",
                    "message": str(exc),
                },
            }
        fingerprint = _contract_state_fingerprint(conn, contract_no)
    finally:
        conn.close()
    return {
        "ready": True,
        "valid_from": vf,
        "valid_to": vt,
        "recorded_at": ra,
        "plan": plan,
        "state_token": _issue_state_token({
            "v": 1,
            "contract_no": contract_no,
            "rate": rate,
            "valid_from": vf,
            "valid_to": vt,
            "recorded_at": ra,
            "reason": reason,
            "fingerprint": fingerprint,
            "iat": now_iso(),
        }),
    }


def find_visible_rates(
    contract_no: str, valid_at: datetime, known_at: Optional[datetime]
) -> tuple[list[sqlite3.Row], str]:
    """查询在 (valid_at, known_at) 双时态坐标下可见且有效的所有版本。

    正常情况下结果应为 0 或 1 条；多于 1 条说明数据存在重叠冲突。
    返回 (命中行列表, 实际使用的 known_at)。
    """
    va = iso(valid_at)
    ka = iso(known_at) if known_at is not None else now_iso()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND valid_from <= ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?)",
            (contract_no, va, va, ka, ka),
        ).fetchall()
    return list(rows), ka


def get_timeline(contract_no: str) -> list[sqlite3.Row]:
    """合同的全部版本，按版本号升序（即录入顺序）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions WHERE contract_no = ? ORDER BY version ASC, id ASC",
            (contract_no,),
        ).fetchall()
    return list(rows)


def get_lineage(rate_id: int) -> dict:
    """从目标版本出发、沿血缘边（后继 -> 祖先）展开到全部祖先的去重 DAG。

    遍历为带访问标记的迭代式 BFS（按层推进，天然按最短路径给出各节点到目标的
    距离），DAG 内节点只出现一次；即使血缘图里被注入环也不会无限递归。展开
    完成后以 Kahn 拓扑排序校验是否存在环，并校验所有边的祖先版本真实存在
    （悬空边）；发现任一异常抛出 LineageCorruptError。目标版本不存在时抛出
    RateNotFoundError。

    节点按 (距目标的距离升序, version 升序, id 升序) 排序；边按
    (源节点距离, 目标节点距离, from_rate_id, to_rate_id, relation) 稳定排序。
    返回 {"target_id", "nodes": [Row], "edges": [dict]}。
    """
    with connect() as conn:
        target = conn.execute(
            "SELECT * FROM rate_versions WHERE id = ?", (rate_id,)
        ).fetchone()
        if target is None:
            raise RateNotFoundError(f"费率版本 id={rate_id} 不存在")

        # 迭代式 BFS：visited 保证每个版本只展开一次，遇环也会终止。
        distances: dict[int, int] = {rate_id: 0}
        nodes_by_id: dict[int, sqlite3.Row] = {rate_id: target}
        edges: list[dict] = []
        frontier = [rate_id]
        distance = 0
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            found_edges = conn.execute(
                f"SELECT from_rate_id, to_rate_id, relation FROM rate_version_edges "
                f"WHERE from_rate_id IN ({placeholders})",
                frontier,
            ).fetchall()
            next_frontier: list[int] = []
            for e in found_edges:
                edges.append(
                    {"from_rate_id": e["from_rate_id"],
                     "to_rate_id": e["to_rate_id"], "relation": e["relation"]}
                )
                parent_id = e["to_rate_id"]
                if parent_id not in distances:
                    distances[parent_id] = distance + 1
                    next_frontier.append(parent_id)
            # 一次性取本层新发现的祖先版本
            if next_frontier:
                placeholders = ",".join("?" for _ in next_frontier)
                rows = conn.execute(
                    f"SELECT * FROM rate_versions WHERE id IN ({placeholders})",
                    next_frontier,
                ).fetchall()
                found_ids = set()
                for row in rows:
                    nodes_by_id[row["id"]] = row
                    found_ids.add(row["id"])
                missing = [rid for rid in next_frontier if rid not in found_ids]
                if missing:
                    raise LineageCorruptError(
                        f"血缘存在悬空边：祖先版本 {sorted(missing)} 不存在"
                    )
            frontier = next_frontier
            distance += 1

        # Kahn 拓扑排序校验环：边方向为后继 -> 祖先，入度为 0 的点即最远祖先。
        # 去重后的边集（UNIQUE 约束已保证不重复，这里再防御一次）。
        unique_edges = {
            (e["from_rate_id"], e["to_rate_id"], e["relation"]): e for e in edges
        }.values()
        indegree: dict[int, int] = {rid: 0 for rid in nodes_by_id}
        children: dict[int, list[int]] = {rid: [] for rid in nodes_by_id}
        for e in unique_edges:
            indegree[e["to_rate_id"]] += 1
            children[e["from_rate_id"]].append(e["to_rate_id"])
        queue = deque(rid for rid, deg in indegree.items() if deg == 0)
        topo_count = 0
        while queue:
            node = queue.popleft()
            topo_count += 1
            for parent in children[node]:
                indegree[parent] -= 1
                if indegree[parent] == 0:
                    queue.append(parent)
        if topo_count != len(nodes_by_id):
            raise LineageCorruptError(
                f"血缘存在环：从版本 id={rate_id} 展开的版本图不是 DAG"
            )

    nodes = sorted(
        nodes_by_id.values(),
        key=lambda row: (distances[row["id"]], row["version"], row["id"]),
    )
    sorted_edges = sorted(
        {(e["from_rate_id"], e["to_rate_id"], e["relation"]): e for e in edges}.values(),
        key=lambda e: (
            distances[e["from_rate_id"]],
            distances[e["to_rate_id"]],
            e["from_rate_id"],
            e["to_rate_id"],
            e["relation"],
        ),
    )
    return {"target_id": rate_id, "nodes": nodes, "edges": sorted_edges}


def _visible_clipped(
    contract_no: str, f: str, t: str, ka: str,
    conn: Optional[sqlite3.Connection] = None,
) -> list[tuple[str, str, sqlite3.Row]]:
    """ka 时点可见版本的业务区间裁剪到窗口 [f, t)，返回 (起点, 终点, 版本行) 列表。

    f / t / ka 均须为已规范化的等宽 ISO 字符串；NULL valid_to 用 INF 占位参与比较。
    传入 conn 时复用该连接（用于同事务内的校验），否则自行开连接。
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?) "
            "  AND valid_from < ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY version ASC, id ASC",
            (contract_no, ka, ka, t, f),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()
    return [
        (max(row["valid_from"], f), min(row["valid_to"] or INF, t), row)
        for row in rows
    ]


def _candidates_at(
    clipped: list[tuple[str, str, sqlite3.Row]], seg_from: str, seg_to: str
) -> list[sqlite3.Row]:
    """完整覆盖片段 [seg_from, seg_to) 的裁剪区间对应版本（保持查询顺序）。"""
    return [row for s, e, row in clipped if s <= seg_from and e >= seg_to]


def _coverage_status(candidates: list[sqlite3.Row]) -> str:
    """按候选版本数分类：0 → gap，1 → covered，>1 → conflict。"""
    if not candidates:
        return "gap"
    if len(candidates) == 1:
        return "covered"
    return "conflict"


def _visible_segments(
    contract_no: str, f: str, t: str, ka: str,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """以 ka 时点可见版本的业务区间边界把窗口 [f, t) 切为连续片段。

    f / t / ka 均须为已规范化的等宽 ISO 字符串。先筛出 ka 可见的版本
    （recorded_at <= ka 且未被取代），将其业务区间裁剪到窗口后切分；
    每段按命中的候选版本数分类：0 → gap，1 → covered，>1 → conflict；
    分类与候选完全相同的相邻段合并为一段。

    返回 {"from", "to", "status", "candidates": [Row]} 字典列表，连续铺满整个窗口；
    无可见版本时自然得到覆盖整窗的单个 gap 段。传入 conn 时复用该连接。
    """
    clipped = _visible_clipped(contract_no, f, t, ka, conn=conn)

    # 以所有区间边界（含窗口端点）切分 [f, t)
    bounds = sorted({f, t, *(p for s, e, _ in clipped for p in (s, e))})

    segments = []
    last_key = None  # 上一段的 (分类, 候选 id 元组)，用于相邻段合并
    for seg_from, seg_to in zip(bounds, bounds[1:]):
        # 区间边界全部参与切分，故与片段相交的裁剪区间必完整覆盖该片段
        candidates = _candidates_at(clipped, seg_from, seg_to)
        seg_status = _coverage_status(candidates)
        key = (seg_status, tuple(row["id"] for row in candidates))
        if key == last_key:
            segments[-1]["to"] = seg_to
        else:
            segments.append(
                {"from": seg_from, "to": seg_to, "status": seg_status, "candidates": candidates}
            )
            last_key = key
    return segments


def compute_coverage(
    contract_no: str,
    from_dt: datetime,
    to_dt: datetime,
    known_at: Optional[datetime],
) -> tuple[list[dict], str]:
    """计算查询窗口 [from, to) 在 known_at 认知时点下的覆盖率分段。

    分段规则见 _visible_segments。返回 (segments, 实际使用的 known_at)。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    ka = iso(known_at) if known_at is not None else now_iso()
    return _visible_segments(contract_no, f, t, ka), ka


# ---------- 结算期费率锁 ----------

def _find_lock_conflicts(
    conn: sqlite3.Connection,
    contract_no: str,
    vf: str,
    vt: Optional[str],
) -> list[dict]:
    """拟议费率区间 [vf, vt)（vt 为 None 表示开放上界）与该合同全部锁片段的交集。

    区间均左闭右开：valid_from < 上界、valid_to > 下界才相交，仅相接不算。
    返回 {"lock": Row, "intersection": {"from", "to"}} 列表，按锁片段起点排序。
    """
    rows = conn.execute(
        "SELECT * FROM rate_locks "
        "WHERE contract_no = ? "
        "  AND valid_from < COALESCE(?, ?) "
        "  AND valid_to > ? "
        "ORDER BY valid_from ASC, id ASC",
        (contract_no, vt, INF, vf),
    ).fetchall()
    return [
        {
            "lock": row,
            "intersection": {
                "from": max(row["valid_from"], vf),
                "to": min(row["valid_to"], vt or INF),
            },
        }
        for row in rows
    ]


def create_rate_lock(
    contract_no: str,
    from_dt: datetime,
    to_dt: datetime,
    known_at: datetime,
    reason: Optional[str],
) -> tuple[list[sqlite3.Row], str]:
    """锁定结算期窗口 [from, to) 在 known_at 认知时点下的费率依据。

    复用覆盖率切分规则：以 known_at 可见版本的业务边界把窗口切为连续片段。
    仅当**整个窗口**在该认知时点均为 covered（每段恰有一个可见版本）时才落库——
    按切分得到的连续片段，每个片段写入一行 rate_locks 并记录其 basis rate_id；
    全部插入在**单一事务**提交，任一段落为 gap 或 conflict 则抛 RateLockUnsafeError，
    已做的插入随事务回滚，不留任何锁记录。

    返回 (新写入的锁记录行列表（按片段起点排序）, 实际使用的 known_at)。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    ka = iso(known_at)

    conn = connect()
    try:
        # 覆盖率切分与锁写入共用同一连接/事务：校验通过后不切换快照。
        segments = _visible_segments(contract_no, f, t, ka, conn=conn)
        unsafe = [seg for seg in segments if seg["status"] != "covered"]
        if unsafe:
            statuses = sorted({seg["status"] for seg in unsafe})
            raise RateLockUnsafeError(
                f"合同 '{contract_no}' 的锁定窗口 [{f}, {t}) 在认知时点 {ka} 存在 "
                f"{'/'.join(statuses)} 片段，无法安全锁定费率",
                segments,
            )

        created_at = now_iso()
        lock_rows: list[sqlite3.Row] = []
        for seg in segments:
            basis_id = seg["candidates"][0]["id"]
            cursor = conn.execute(
                "INSERT INTO rate_locks "
                "(contract_no, valid_from, valid_to, known_at, basis_rate_id, "
                " reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contract_no, seg["from"], seg["to"], ka, basis_id, reason, created_at),
            )
            lock_rows.append(
                conn.execute(
                    "SELECT * FROM rate_locks WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )
        conn.commit()
        return lock_rows, ka
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_rate_locks(contract_no: str) -> list[sqlite3.Row]:
    """合同的全部结算期锁记录，按锁定时间（写入顺序）升序。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_locks WHERE contract_no = ? ORDER BY valid_from ASC, id ASC",
            (contract_no,),
        ).fetchall()
    return list(rows)


def _duration_us(f: str, t: str) -> int:
    """两个等宽 ISO 字符串之间的时长（微秒）。"""
    delta = parse(t) - parse(f)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _classify_change(
    before_status: str,
    before_candidates: list[sqlite3.Row],
    after_status: str,
    after_candidates: list[sqlite3.Row],
) -> str:
    """双侧覆盖状态 → 变更分类（规则见 compute_changes）。"""
    if "conflict" in (before_status, after_status):
        return "conflict"
    if before_status == "gap":
        return "added" if after_status == "covered" else "unchanged"
    if after_status == "gap":
        return "removed"
    # 两侧均唯一覆盖：费率相同为 unchanged，不同为 rate_changed
    if before_candidates[0]["rate"] == after_candidates[0]["rate"]:
        return "unchanged"
    return "rate_changed"


def compute_changes(
    contract_no: str,
    from_dt: datetime,
    to_dt: datetime,
    before_known_at: datetime,
    after_known_at: datetime,
) -> dict:
    """对比两个认知时点下窗口 [from, to) 的费率覆盖差异。

    分别按 before_known_at / after_known_at 筛出可见版本，以两侧业务区间边界的
    并集把窗口切为连续片段，逐段给出 before/after 两侧的覆盖状态（gap / covered /
    conflict 及候选版本），并分类：

    - 任一侧 conflict                   → conflict；
    - gap → covered                     → added；
    - covered → gap                     → removed；
    - 两侧均 gap，或均唯一覆盖且费率相同 → unchanged；
    - 其余两侧唯一覆盖（费率不同）       → rate_changed。

    分类与两侧候选完全一致的相邻片段合并为一段；summary 汇总各分类的持续微秒数。
    返回 {"before_known_at", "after_known_at", "segments", "summary"}。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    bka = iso(before_known_at)
    aka = iso(after_known_at)
    if bka >= aka:
        raise InvalidKnownAtOrderError(
            f"before_known_at={bka} 必须早于 after_known_at={aka}"
        )

    before_clipped = _visible_clipped(contract_no, f, t, bka)
    after_clipped = _visible_clipped(contract_no, f, t, aka)

    # 两侧可见版本业务区间边界的并集（含窗口端点）切分 [f, t)
    bounds = sorted({
        f,
        t,
        *(p for s, e, _ in before_clipped for p in (s, e)),
        *(p for s, e, _ in after_clipped for p in (s, e)),
    })

    segments = []
    summary = {"added_us": 0, "removed_us": 0, "unchanged_us": 0,
               "rate_changed_us": 0, "conflict_us": 0}
    last_key = None  # 上一段的 (分类, before 候选 id 元组, after 候选 id 元组)
    for seg_from, seg_to in zip(bounds, bounds[1:]):
        before_candidates = _candidates_at(before_clipped, seg_from, seg_to)
        after_candidates = _candidates_at(after_clipped, seg_from, seg_to)
        change = _classify_change(
            _coverage_status(before_candidates), before_candidates,
            _coverage_status(after_candidates), after_candidates,
        )
        duration = _duration_us(seg_from, seg_to)
        summary[f"{change}_us"] += duration
        key = (
            change,
            tuple(row["id"] for row in before_candidates),
            tuple(row["id"] for row in after_candidates),
        )
        if key == last_key:
            segments[-1]["to"] = seg_to
            segments[-1]["duration_us"] += duration
        else:
            segments.append(
                {
                    "from": seg_from,
                    "to": seg_to,
                    "change": change,
                    "duration_us": duration,
                    "before": {"status": _coverage_status(before_candidates),
                               "candidates": before_candidates},
                    "after": {"status": _coverage_status(after_candidates),
                              "candidates": after_candidates},
                }
            )
            last_key = key
    return {
        "before_known_at": bka,
        "after_known_at": aka,
        "segments": segments,
        "summary": summary,
    }


# 费用与时长占比的小数精度（预览口径）
CHARGE_SCALE = 6


def compute_charge_preview(
    contract_no: str,
    lines: list[tuple[datetime, datetime, float]],
    known_at: Optional[datetime],
) -> dict:
    """计费预览：把每条账单行 [from, to) 按 known_at 可见费率的业务边界拆分，
    每段费用 = base_amount × (段时长 / 行时长) × 命中费率，行内汇总为 line_total，
    全部行再汇总为 total_amount。

    任一行出现 gap（无费率）或 conflict（重叠冲突）片段时，该行 line_total 置 None，
    且整体拒绝汇总（ok=False、total_amount=None）；未受影响的行仍正常计价。

    lines 为 (from, to, base_amount) 三元组。返回
    {"known_at", "ok", "total_amount", "lines": [...]}；每个 line 为
    {"from", "to", "base_amount", "status", "line_total", "segments"}，
    行状态为 ok / gap / conflict / mixed（同时含 gap 与 conflict 段）；
    每个 segment 为 {"from", "to", "status", "portion", "rate_id", "rate",
    "charge", "candidates"}。
    """
    ka = iso(known_at) if known_at is not None else now_iso()
    if ka > now_iso():
        raise FutureKnownAtError(f"known_at={ka} 晚于当前时间，认知时点不能取自未来")

    # 先校验全部账单行，再统一计价
    normalized = []
    for from_dt, to_dt, base_amount in lines:
        if not math.isfinite(base_amount) or base_amount < 0:
            raise InvalidAmountError(f"base_amount 必须是非负有限数值，收到 {base_amount!r}")
        f, t = iso(from_dt), iso(to_dt)
        if f >= t:
            raise InvalidIntervalError(f"账单行区间非法：from={f} 必须早于 to={t}")
        normalized.append((f, t, base_amount))

    out_lines = []
    total = 0.0
    all_ok = True
    for f, t, base_amount in normalized:
        line_us = _duration_us(f, t)
        segments = []
        flags = set()
        line_total = 0.0
        for seg in _visible_segments(contract_no, f, t, ka):
            portion = _duration_us(seg["from"], seg["to"]) / line_us
            view = {
                "from": seg["from"],
                "to": seg["to"],
                "status": seg["status"],
                "portion": round(portion, CHARGE_SCALE),
                "rate_id": None,
                "rate": None,
                "charge": None,
                "candidates": [],
            }
            if seg["status"] == "covered":
                row = seg["candidates"][0]
                charge = round(base_amount * portion * row["rate"], CHARGE_SCALE)
                line_total += charge
                view.update(rate_id=row["id"], rate=row["rate"], charge=charge)
            else:
                flags.add(seg["status"])
                if seg["status"] == "conflict":
                    view["candidates"] = [
                        {"rate_id": r["id"], "rate": r["rate"], "version": r["version"]}
                        for r in seg["candidates"]
                    ]
            segments.append(view)

        if flags:
            all_ok = False
            line_status = "mixed" if len(flags) > 1 else next(iter(flags))
            line_total_view = None
        else:
            line_status = "ok"
            line_total_view = round(line_total, CHARGE_SCALE)
            total += line_total_view
        out_lines.append(
            {
                "from": f,
                "to": t,
                "base_amount": base_amount,
                "status": line_status,
                "line_total": line_total_view,
                "segments": segments,
            }
        )

    return {
        "known_at": ka,
        "ok": all_ok,
        "total_amount": round(total, CHARGE_SCALE) if all_ok else None,
        "lines": out_lines,
    }


# ---------- 最惠价合规审计 ----------

def _validate_peer_contracts(
    contract_no: str, peer_contract_nos: list[str]
) -> list[str]:
    """校验同业合同列表：非空、不含目标合同、无重复；按输入原顺序返回。"""
    if not peer_contract_nos:
        raise InvalidPeerContractsError(
            "EMPTY_PEER_CONTRACTS", "peer_contract_nos 至少需要一个同业合同"
        )
    if contract_no in peer_contract_nos:
        raise InvalidPeerContractsError(
            "TARGET_IN_PEER_CONTRACTS",
            f"目标合同 '{contract_no}' 不得混入同业合同列表",
        )
    seen: set[str] = set()
    duplicates: list[str] = []
    for peer in peer_contract_nos:
        if peer in seen and peer not in duplicates:
            duplicates.append(peer)
        seen.add(peer)
    if duplicates:
        raise InvalidPeerContractsError(
            "DUPLICATE_PEER_CONTRACT",
            f"同业合同列表含重复合同：{', '.join(sorted(duplicates))}",
        )
    return list(peer_contract_nos)


def _issue_entry(
    contract_no: str, issue: str, candidates: list[sqlite3.Row]
) -> dict:
    """inconclusive 原因条目：问题合同 + gap/conflict；conflict 附全部候选版本。"""
    return {
        "contract_no": contract_no,
        "issue": issue,
        "candidates": [
            {"rate_id": r["id"], "rate": r["rate"], "version": r["version"]}
            for r in candidates
        ]
        if issue == "conflict"
        else [],
    }


def compute_most_favored_audit(
    contract_no: str,
    peer_contract_nos: list[str],
    from_dt: datetime,
    to_dt: datetime,
    known_at: Optional[datetime],
    max_markup: float,
) -> dict:
    """最惠价合规审计：按同一认知时点比较目标合同与同业合同费率（全程只读）。

    以目标及全部同业合同在 known_at 时点可见版本的业务区间边界的**并集**把
    窗口 [from, to) 切为连续片段，逐段判定：

    - 目标与所有同业均唯一覆盖（covered）：以同业最低费率为 benchmark
      （并列最低时取同业列表中靠前者），目标费率 > benchmark × (1 + max_markup)
      判 violation，否则 compliant；
    - 任一合同 gap / conflict：判 inconclusive，reasons 逐条列出问题合同与
      原因（目标在前，同业按输入顺序；conflict 附全部候选），绝不错判合规。

    状态与判定依据（compliant/violation：目标版本与 benchmark 版本；
    inconclusive：原因列表）完全相同的相邻段合并为一段；summary 汇总各状态的
    持续微秒数（三个 *_us 字段恒存在，合计等于窗口总时长）。

    返回 {"known_at", "segments", "summary"}。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    if not math.isfinite(max_markup) or max_markup < 0:
        raise InvalidMarkupError(f"max_markup 必须是非负有限数值，收到 {max_markup!r}")
    peers = _validate_peer_contracts(contract_no, peer_contract_nos)
    ka = iso(known_at) if known_at is not None else now_iso()

    conn = connect()
    try:
        # 同一连接读取目标与全部同业，保证审计基于同一快照；全程只读。
        target_clipped = _visible_clipped(contract_no, f, t, ka, conn=conn)
        peer_clipped = [
            (peer, _visible_clipped(peer, f, t, ka, conn=conn)) for peer in peers
        ]
    finally:
        conn.close()

    # 目标与全部同业可见版本业务区间边界的并集（含窗口端点）切分 [f, t)
    bounds = sorted({
        f,
        t,
        *(p for s, e, _ in target_clipped for p in (s, e)),
        *(p for _, clipped in peer_clipped for s, e, _ in clipped for p in (s, e)),
    })

    segments = []
    summary = {"compliant_us": 0, "violation_us": 0, "inconclusive_us": 0}
    last_key = None  # 上一段的判定依据（状态 + 目标/benchmark 版本或原因列表）
    for seg_from, seg_to in zip(bounds, bounds[1:]):
        duration = _duration_us(seg_from, seg_to)
        target_candidates = _candidates_at(target_clipped, seg_from, seg_to)
        peer_candidates = [
            (peer, _candidates_at(clipped, seg_from, seg_to))
            for peer, clipped in peer_clipped
        ]

        # inconclusive 原因：目标在前，同业按输入顺序；conflict 附全部候选
        reasons = []
        target_status = _coverage_status(target_candidates)
        if target_status != "covered":
            reasons.append(_issue_entry(contract_no, target_status, target_candidates))
        for peer, candidates in peer_candidates:
            peer_status = _coverage_status(candidates)
            if peer_status != "covered":
                reasons.append(_issue_entry(peer, peer_status, candidates))

        if reasons:
            status = "inconclusive"
            target_basis = None
            benchmark = None
            threshold = None
            key = (
                status,
                tuple(
                    (
                        r["contract_no"],
                        r["issue"],
                        tuple(c["rate_id"] for c in r["candidates"]),
                    )
                    for r in reasons
                ),
            )
        else:
            target_basis = target_candidates[0]
            benchmark_peer, benchmark_row = min(
                ((peer, candidates[0]) for peer, candidates in peer_candidates),
                key=lambda item: item[1]["rate"],
            )
            benchmark = {
                "contract_no": benchmark_peer,
                "rate_id": benchmark_row["id"],
                "rate": benchmark_row["rate"],
            }
            threshold = benchmark_row["rate"] * (1 + max_markup)
            status = "violation" if target_basis["rate"] > threshold else "compliant"
            key = (status, target_basis["id"], benchmark["rate_id"])

        summary[f"{status}_us"] += duration
        if key == last_key:
            segments[-1]["to"] = seg_to
            segments[-1]["duration_us"] += duration
        else:
            segments.append(
                {
                    "from": seg_from,
                    "to": seg_to,
                    "status": status,
                    "duration_us": duration,
                    "target_basis": target_basis,
                    "benchmark": benchmark,
                    "threshold_rate": threshold,
                    "reasons": reasons,
                }
            )
            last_key = key
    return {"known_at": ka, "segments": segments, "summary": summary}
