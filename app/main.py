"""合同费率双时态追溯 API。"""

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic_core import PydanticCustomError

from . import approvals, logic, settlements
from .db import init_db
from .timeutil import iso


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时自动建表
    approvals.init_table()  # 审批模块独立建表（幂等）
    settlements.init_table()  # 结算批次模块独立建表（幂等）
    yield


app = FastAPI(
    title="合同费率双时态追溯 API",
    version="1.9.0",
    description="按业务时间（valid_at）与认知时间（known_at）双维度追溯合同费率，修订只追加版本、不覆盖历史；版本血缘以可审计的 supersedes / retained_from 边记录；结算期费率锁保证锁定窗口内费率不再被修订；费率修订预演（不写库）签发 state_token，POST /rates 携带 token 条件提交，保证预演与正式提交结果一致；最惠价合规审计按同一认知时点比较目标与同业合同费率，逐段判定 compliant / violation / inconclusive（全程只读）；费率修订审批模块在独立表上提供申请创建（复用预演、区间互斥）、审核（expected_revision 乐观锁）与执行（同事务条件提交、token 陈旧保留 approved、重复执行幂等）；结算批次模块复用计费预览的区间切分与全覆盖校验，通过后在独立表原子保存不可变快照，Idempotency-Key 提供创建幂等（同键异请求 409）。",
    lifespan=lifespan,
)


# ---------- 请求 / 响应模型 ----------

class RateCreate(BaseModel):
    contract_no: str = Field(min_length=1, max_length=128, description="合同号")
    rate: float = Field(description="费率值")
    valid_from: datetime = Field(description="业务生效起点（含），ISO-8601")
    valid_to: Optional[datetime] = Field(default=None, description="业务生效终点（不含），缺省为无限期")
    recorded_at: Optional[datetime] = Field(default=None, description="系统记录时间，缺省为当前时间；同一合同内必须单调递增")
    reason: Optional[str] = Field(default=None, max_length=500, description="录入/修订说明")
    state_token: Optional[str] = Field(
        default=None,
        description="修订预演签发的条件提交令牌；携带时仅当相关当前版本与锁集合自预演以来未变化才提交，否则 409 STALE_REVISION_PREVIEW",
    )


class VersionView(BaseModel):
    rate_id: int
    contract_no: str
    rate: float
    version: int
    valid_from: str
    valid_to: Optional[str]
    recorded_at: str
    superseded_at: Optional[str]
    status: str  # current / superseded
    reason: Optional[str]


class RateCreated(BaseModel):
    created: VersionView
    superseded_rate_ids: list[int]
    retained_versions: list[VersionView] = Field(
        default_factory=list,
        description="部分重叠修订时，被覆盖旧版本在新区间之外的左/右补偿版本（原费率、原 reason），按 valid_from 排序",
    )


class QuoteResponse(BaseModel):
    contract_no: str
    rate: float
    valid_at: str
    known_at: str
    basis: VersionView = Field(description="命中费率的版本依据")


class TimelineResponse(BaseModel):
    contract_no: str
    versions: list[VersionView]


class LineageEdge(BaseModel):
    from_rate_id: int = Field(description="后继版本 id（新费率或补偿片段）")
    to_rate_id: int = Field(description="祖先版本 id（被取代版本或补偿片段来源版本）")
    relation: str = Field(description="supersedes（取代）/ retained_from（补偿片段来源）")


class LineageResponse(BaseModel):
    target_rate_id: int
    nodes: list[VersionView] = Field(
        description="目标及其全部祖先版本（去重 DAG），按距目标从近到远、version 升序排序"
    )
    edges: list[LineageEdge] = Field(description="血缘边（后继 -> 祖先），按端点距离稳定排序")


class CoverageSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    status: str = Field(description="covered / gap / conflict")
    basis: Optional[VersionView] = Field(default=None, description="covered 时命中版本的唯一依据")
    candidates: list[VersionView] = Field(default_factory=list, description="conflict 时的全部候选版本")


class CoverageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="查询窗口起点（含）")
    to: str = Field(description="查询窗口终点（不含）")
    known_at: str = Field(description="实际使用的认知时点")
    segments: list[CoverageSegment]


class ChangeSideView(BaseModel):
    status: str = Field(description="该认知时点下的覆盖状态：covered / gap / conflict")
    basis: Optional[VersionView] = Field(default=None, description="covered 时命中版本的唯一依据")
    candidates: list[VersionView] = Field(default_factory=list, description="conflict 时的全部候选版本")


class ChangeSegmentView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    change: str = Field(description="added / removed / unchanged / rate_changed / conflict")
    duration_us: int = Field(description="该片段的持续时长（微秒）")
    before: ChangeSideView = Field(description="before_known_at 时点下的覆盖状态")
    after: ChangeSideView = Field(description="after_known_at 时点下的覆盖状态")


class ChangeSummary(BaseModel):
    added_us: int = Field(description="gap→covered 片段的总时长（微秒）")
    removed_us: int = Field(description="covered→gap 片段的总时长（微秒）")
    unchanged_us: int = Field(description="无变化片段的总时长（微秒）")
    rate_changed_us: int = Field(description="费率变化片段的总时长（微秒）")
    conflict_us: int = Field(description="任一侧冲突片段的总时长（微秒）")


class ChangesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="查询窗口起点（含）")
    to: str = Field(description="查询窗口终点（不含）")
    before_known_at: str = Field(description="较早认知时点")
    after_known_at: str = Field(description="较晚认知时点")
    segments: list[ChangeSegmentView]
    summary: ChangeSummary = Field(description="各变更分类的持续微秒数汇总")


class ChargeLineIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from", description="账单行起点（含），ISO-8601")
    to: datetime = Field(description="账单行终点（不含），ISO-8601")
    base_amount: float = Field(description="账单行基准金额（须为非负有限数值），按区间时长比例折算")


class ChargePreviewRequest(BaseModel):
    known_at: Optional[datetime] = Field(default=None, description="认知时点，缺省为当前时间；不得晚于当前时间")
    lines: list[ChargeLineIn] = Field(min_length=1, description="账单行列表，至少一条")


class ChargeCandidate(BaseModel):
    rate_id: int
    rate: float
    version: int


class ChargeSegmentView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    status: str = Field(description="covered / gap / conflict")
    portion: float = Field(description="该段时长占账单行总时长的比例（6 位小数）")
    rate_id: Optional[int] = Field(default=None, description="covered 时命中的费率版本 id")
    rate: Optional[float] = Field(default=None, description="covered 时命中的费率值")
    charge: Optional[float] = Field(default=None, description="covered 时该段折算费用（6 位小数）")
    candidates: list[ChargeCandidate] = Field(default_factory=list, description="conflict 时的全部候选版本")


class ChargeLineView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="账单行起点（含）")
    to: str = Field(description="账单行终点（不含）")
    base_amount: float
    status: str = Field(description="ok / gap / conflict / mixed（同时含 gap 与 conflict 段）")
    line_total: Optional[float] = Field(default=None, description="行内费用合计；含 gap/conflict 段时为 null")
    segments: list[ChargeSegmentView]


class ChargePreviewResponse(BaseModel):
    contract_no: str
    known_at: str = Field(description="实际使用的认知时点")
    status: str = Field(description="ok / rejected（任一行含 gap/conflict 段，总额被拒绝）")
    total_amount: Optional[float] = Field(default=None, description="全部行费用合计；rejected 时为 null")
    lines: list[ChargeLineView]


class MostFavoredAuditRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    peer_contract_nos: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        description="同业合同号列表：至少一个，不得含目标合同，不得重复"
    )
    from_: datetime = Field(alias="from", description="审计窗口起点（含），ISO-8601")
    to: datetime = Field(description="审计窗口终点（不含），ISO-8601")
    known_at: Optional[datetime] = Field(
        default=None, description="认知时点，缺省为当前时间；目标与全部同业共用同一时点"
    )
    max_markup: float = Field(description="允许的最大加价幅度（非负有限数值，如 0.1 表示 10%）")


class AuditIssueView(BaseModel):
    contract_no: str = Field(description="出现问题的合同号（目标或同业）")
    issue: str = Field(description="gap / conflict")
    candidates: list[ChargeCandidate] = Field(
        default_factory=list, description="仅 conflict：该合同在该段的全部候选版本"
    )


class BenchmarkView(BaseModel):
    contract_no: str = Field(description="benchmark 来源同业合同（并列最低时取同业列表中靠前者）")
    rate_id: int = Field(description="benchmark 命中的同业费率版本 id")
    rate: float = Field(description="同业最低费率")


class MostFavoredSegmentView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    status: str = Field(description="compliant / violation / inconclusive")
    duration_us: int = Field(description="该片段的持续时长（微秒）")
    target_basis: Optional[VersionView] = Field(
        default=None, description="可判定时目标合同命中的唯一版本依据；inconclusive 时为 null"
    )
    benchmark: Optional[BenchmarkView] = Field(
        default=None, description="可判定时的同业最低费率基准；inconclusive 时为 null"
    )
    threshold_rate: Optional[float] = Field(
        default=None, description="违规阈值 = benchmark × (1 + max_markup)；inconclusive 时为 null"
    )
    reasons: list[AuditIssueView] = Field(
        default_factory=list,
        description="inconclusive 时的全部原因（目标在前，同业按输入顺序）；可判定时为空",
    )


class MostFavoredSummary(BaseModel):
    compliant_us: int = Field(description="compliant 片段的总时长（微秒）")
    violation_us: int = Field(description="violation 片段的总时长（微秒）")
    inconclusive_us: int = Field(description="inconclusive 片段的总时长（微秒）")


class MostFavoredAuditResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    peer_contract_nos: list[str] = Field(description="实际使用的同业合同列表（输入原顺序）")
    from_: str = Field(alias="from", description="审计窗口起点（含）")
    to: str = Field(description="审计窗口终点（不含）")
    known_at: str = Field(description="实际使用的认知时点")
    max_markup: float
    segments: list[MostFavoredSegmentView]
    summary: MostFavoredSummary = Field(
        description="各状态持续微秒数汇总（三个字段恒存在，合计等于窗口总时长）"
    )


class RateLockCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from", description="锁定结算期起点（含），ISO-8601")
    to: datetime = Field(description="锁定结算期终点（不含），ISO-8601")
    known_at: datetime = Field(description="锁定所依据的认知时点，ISO-8601")
    reason: Optional[str] = Field(default=None, max_length=500, description="锁定原因")


class RateLockView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    lock_id: int
    contract_no: str
    from_: str = Field(alias="from", description="锁定片段起点（含）")
    to: str = Field(description="锁定片段终点（不含）")
    known_at: str = Field(description="锁定所依据的认知时点")
    basis_rate_id: int = Field(description="该片段在 known_at 时点唯一可见的费率版本 id")
    reason: Optional[str]
    created_at: str = Field(description="锁记录写入时间（审计用）")


class RateLockCreated(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="锁定窗口起点（含）")
    to: str = Field(description="锁定窗口终点（不含）")
    known_at: str
    locks: list[RateLockView] = Field(
        description="按覆盖率切分得到的连续锁记录（每段附 basis rate_id），按片段起点排序"
    )


class RateLockListResponse(BaseModel):
    contract_no: str
    locks: list[RateLockView]


class RateRevisionPreviewCreate(BaseModel):
    rate: float = Field(description="费率值")
    valid_from: datetime = Field(description="业务生效起点（含），ISO-8601")
    valid_to: Optional[datetime] = Field(default=None, description="业务生效终点（不含），缺省为无限期")
    recorded_at: Optional[datetime] = Field(default=None, description="系统记录时间，缺省为当前时间；同一合同内必须单调递增")
    reason: Optional[str] = Field(default=None, max_length=500, description="录入/修订说明")


class PlannedVersionView(BaseModel):
    """预演计划创建的版本（尚未落库，无 rate_id；version 为预计版本号）。"""

    contract_no: str
    rate: float
    version: int
    valid_from: str
    valid_to: Optional[str]
    recorded_at: str
    reason: Optional[str]


class IntersectionView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="交集起点（含）")
    to: str = Field(description="交集终点（不含）")


class RevisionPreviewBlocking(BaseModel):
    code: str = Field(description="阻断原因码：RATE_WINDOW_LOCKED / NON_MONOTONIC_RECORDED_AT")
    message: str
    locks: list[RateLockView] = Field(
        default_factory=list, description="仅 RATE_WINDOW_LOCKED：与拟议区间相交的锁片段"
    )
    intersections: list[IntersectionView] = Field(
        default_factory=list, description="仅 RATE_WINDOW_LOCKED：与各相交锁片段的交集，顺序同 locks"
    )


class RateRevisionPreviewResponse(BaseModel):
    contract_no: str
    ready: bool = Field(description="True=可按本预演条件提交；False=被阻断（blocking 附原因）")
    state_token: Optional[str] = Field(
        default=None, description="条件提交令牌（绑定本次参数与合同状态指纹）；阻断时为 null"
    )
    created: Optional[PlannedVersionView] = Field(
        default=None, description="将创建的主版本（含预计版本号，尚无 rate_id）"
    )
    superseded_versions: list[VersionView] = Field(
        default_factory=list, description="将被取代的当前版本（完整版本视图）"
    )
    retained_versions: list[PlannedVersionView] = Field(
        default_factory=list,
        description="将追加的补偿片段（原费率、原 reason，版本号紧随主版本），按 valid_from 排序",
    )
    blocking: Optional[RevisionPreviewBlocking] = Field(
        default=None, description="阻断原因；ready 时为 null"
    )


# ---------- 费率修订审批（独立模块/表） ----------

class RevisionApprovalCreate(BaseModel):
    rate: float = Field(description="提议费率值")
    valid_from: datetime = Field(description="业务生效起点（含），ISO-8601")
    valid_to: Optional[datetime] = Field(default=None, description="业务生效终点（不含），缺省为无限期")
    recorded_at: Optional[datetime] = Field(default=None, description="系统记录时间，缺省为当前时间；创建时由预演冻结")
    reason: Optional[str] = Field(default=None, max_length=500, description="提议修订说明")


class RevisionApprovalView(BaseModel):
    approval_id: int
    contract_no: str
    rate: float
    valid_from: str
    valid_to: Optional[str]
    recorded_at: str = Field(description="预演冻结的系统记录时间，执行条件提交时以此为准")
    reason: Optional[str]
    state_token: str = Field(description="创建申请时预演签发、随申请保存的条件提交令牌")
    status: str = Field(description="pending / approved / rejected / executed")
    revision: int = Field(description="乐观锁版本号：审核须携带 expected_revision，每次流转 +1")
    created_rate_id: Optional[int] = Field(
        default=None, description="执行成功后新建的主费率版本 id；未执行时为 null"
    )
    review_comment: Optional[str]
    reviewed_at: Optional[str]
    executed_at: Optional[str]
    created_at: str


class RevisionApprovalResponse(BaseModel):
    approval: RevisionApprovalView


class RevisionApprovalReview(BaseModel):
    action: Literal["approve", "reject"] = Field(
        description="审核动作：approve（pending→approved）/ reject（pending→rejected）"
    )
    expected_revision: int = Field(
        ge=1, description="客户端预期的 revision；与库中不一致返回 409 REVISION_MISMATCH"
    )
    comment: Optional[str] = Field(default=None, max_length=500, description="审核意见")


class RevisionApprovalExecutedResponse(BaseModel):
    approval: RevisionApprovalView
    result: RateCreated = Field(description="条件提交结果（与 POST /rates 响应同构）")
    idempotent: bool = Field(description="True=重复执行，回放首次执行保存的原结果")


class RevisionApprovalListResponse(BaseModel):
    contract_no: str
    approvals: list[RevisionApprovalView]


# ---------- 结算批次（独立模块/表，不可变快照 + 幂等键） ----------

class SettlementBatchCreate(BaseModel):
    known_at: Optional[datetime] = Field(
        default=None, description="认知时点，缺省为当前时间；不得晚于当前时间"
    )
    lines: list[ChargeLineIn] = Field(min_length=1, description="账单行列表，至少一条")


class SettlementRequestLineView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="规范化账单行起点（含）")
    to: str = Field(description="规范化账单行终点（不含）")
    base_amount: float


class SettlementRequestView(BaseModel):
    known_at: str = Field(description="实际使用的认知时点（规范化 UTC ISO 字符串）")
    lines: list[SettlementRequestLineView]


class SettlementBatchView(BaseModel):
    batch_id: int
    contract_no: str
    idempotency_key: str = Field(description="创建时携带的幂等键")
    created_at: str = Field(description="快照写入时间（审计用）")
    total_amount: float = Field(description="全部行费用合计")
    lines: list[ChargeLineView] = Field(
        description="各行金额（line_total）与逐段费率依据（rate_id/rate/charge/portion）"
    )
    request: SettlementRequestView = Field(description="创建时冻结的规范化请求快照")


class SettlementBatchResponse(BaseModel):
    batch: SettlementBatchView
    idempotent: bool = Field(
        description="True=相同幂等键且请求一致的重放，返回首次快照；False=首次创建"
    )


def to_view(row: sqlite3.Row) -> VersionView:
    return VersionView(
        rate_id=row["id"],
        contract_no=row["contract_no"],
        rate=row["rate"],
        version=row["version"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        superseded_at=row["superseded_at"],
        status="current" if row["superseded_at"] is None else "superseded",
        reason=row["reason"],
    )


def to_approval_view(row: sqlite3.Row) -> RevisionApprovalView:
    return RevisionApprovalView(
        approval_id=row["id"],
        contract_no=row["contract_no"],
        rate=row["rate"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        reason=row["reason"],
        state_token=row["state_token"],
        status=row["status"],
        revision=row["revision"],
        created_rate_id=row["created_rate_id"],
        review_comment=row["review_comment"],
        reviewed_at=row["reviewed_at"],
        executed_at=row["executed_at"],
        created_at=row["created_at"],
    )


def to_lock_view(row: sqlite3.Row) -> RateLockView:
    return RateLockView(
        lock_id=row["id"],
        contract_no=row["contract_no"],
        from_=row["valid_from"],
        to=row["valid_to"],
        known_at=row["known_at"],
        basis_rate_id=row["basis_rate_id"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


def to_coverage_segment(seg: dict) -> CoverageSegment:
    return CoverageSegment(
        from_=seg["from"],
        to=seg["to"],
        status=seg["status"],
        basis=to_view(seg["candidates"][0]) if seg["status"] == "covered" else None,
        candidates=[to_view(r) for r in seg["candidates"]] if seg["status"] == "conflict" else [],
    )


def to_batch_view(outcome: dict) -> SettlementBatchView:
    snapshot = outcome["snapshot"]
    return SettlementBatchView(
        batch_id=outcome["batch_id"],
        contract_no=outcome["contract_no"],
        idempotency_key=outcome["idempotency_key"],
        created_at=outcome["created_at"],
        total_amount=snapshot["total_amount"],
        lines=[ChargeLineView(**line) for line in snapshot["lines"]],
        request=SettlementRequestView(**snapshot["request"]),
    )


# ---------- 异常处理 ----------

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"code": "INVALID_REQUEST", "message": "请求参数不合法", "errors": jsonable_encoder(exc.errors())},
    )


# ---------- 路由 ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/rates", response_model=RateCreated, status_code=status.HTTP_201_CREATED)
def create_rate(payload: RateCreate):
    """录入费率。与既有当前版本区间重叠时自动修订：旧版本仅交集被替换，
    交集之外的左、右部分按原费率与 reason 追加为补偿版本，历史保留、窗口外报价不变。

    携带修订预演签发的 state_token 时为条件提交：token 绑定参数须与本次请求一致，
    且相关当前版本与锁集合自预演以来未变化，否则拒绝且不产生任何写入。"""
    try:
        row, superseded_ids, retained_rows = logic.record_rate(
            contract_no=payload.contract_no,
            rate=payload.rate,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            recorded_at=payload.recorded_at,
            reason=payload.reason,
            state_token=payload.state_token,
        )
    except logic.InvalidStateTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_STATE_TOKEN", "message": str(exc)},
        )
    except logic.StaleRevisionPreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "STALE_REVISION_PREVIEW", "message": str(exc)},
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.NonMonotonicRecordTimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NON_MONOTONIC_RECORDED_AT", "message": str(exc)},
        )
    except logic.RateWindowLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RATE_WINDOW_LOCKED",
                "message": str(exc),
                "locks": [to_lock_view(c["lock"]).model_dump(by_alias=True) for c in exc.conflicts],
                "intersections": [c["intersection"] for c in exc.conflicts],
            },
        )
    return RateCreated(
        created=to_view(row),
        superseded_rate_ids=superseded_ids,
        retained_versions=[to_view(r) for r in retained_rows],
    )


@app.post(
    "/contracts/{contract_no}/rate-revision-previews",
    response_model=RateRevisionPreviewResponse,
)
def rate_revision_preview(contract_no: str, payload: RateRevisionPreviewCreate):
    """费率修订预演（全程不写库）：与 POST /rates 共用同一规划逻辑
    （锁相交优先 → recorded_at 单调性 → 部分重叠补偿 → 版本号递增），
    返回将创建的主版本、被取代版本、补偿片段；被阻断时返回阻断原因。
    预演通过时签发 state_token，供 POST /rates 条件提交。"""
    try:
        result = logic.preview_rate_revision(
            contract_no=contract_no,
            rate=payload.rate,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            recorded_at=payload.recorded_at,
            reason=payload.reason,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    if not result["ready"]:
        blocking = result["blocking"]
        conflicts = blocking.get("conflicts") or []
        return RateRevisionPreviewResponse(
            contract_no=contract_no,
            ready=False,
            state_token=None,
            created=None,
            superseded_versions=[],
            retained_versions=[],
            blocking=RevisionPreviewBlocking(
                code=blocking["code"],
                message=blocking["message"],
                locks=[to_lock_view(c["lock"]) for c in conflicts],
                intersections=[
                    IntersectionView(
                        from_=c["intersection"]["from"], to=c["intersection"]["to"]
                    )
                    for c in conflicts
                ],
            ),
        )
    plan = result["plan"]
    return RateRevisionPreviewResponse(
        contract_no=contract_no,
        ready=True,
        state_token=result["state_token"],
        created=PlannedVersionView(
            contract_no=contract_no,
            rate=payload.rate,
            version=plan["next_version"],
            valid_from=result["valid_from"],
            valid_to=result["valid_to"],
            recorded_at=result["recorded_at"],
            reason=payload.reason,
        ),
        superseded_versions=[to_view(r) for r in plan["overlaps"]],
        retained_versions=[
            PlannedVersionView(
                contract_no=contract_no,
                rate=old["rate"],
                version=plan["next_version"] + 1 + i,
                valid_from=piece_vf,
                valid_to=piece_vt,
                recorded_at=result["recorded_at"],
                reason=old["reason"],
            )
            for i, (piece_vf, piece_vt, _, old) in enumerate(plan["pieces"])
        ],
        blocking=None,
    )


@app.get("/quote", response_model=QuoteResponse)
def quote(
    contract_no: str = Query(min_length=1, description="合同号"),
    valid_at: datetime = Query(description="业务时点，ISO-8601"),
    known_at: Optional[datetime] = Query(default=None, description="认知时点，缺省为当前时间"),
):
    """报价：返回在 valid_at 有效、且在 known_at 时点可见的唯一费率及版本依据。"""
    rows, ka = logic.find_visible_rates(contract_no, valid_at, known_at)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NO_MATCH",
                "message": f"合同 '{contract_no}' 在业务时点 {valid_at}、认知时点 {ka} 下无可见且有效的费率",
            },
        )
    if len(rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "OVERLAP",
                "message": f"合同 '{contract_no}' 在该时点坐标下命中 {len(rows)} 条费率，数据存在重叠冲突",
                "candidates": [to_view(r).model_dump() for r in rows],
            },
        )
    row = rows[0]
    return QuoteResponse(
        contract_no=row["contract_no"],
        rate=row["rate"],
        valid_at=iso(valid_at),
        known_at=ka,
        basis=to_view(row),
    )


@app.get("/contracts/{contract_no}/timeline", response_model=TimelineResponse)
def timeline(contract_no: str):
    """版本时间线：列出合同全部版本及其业务/系统时间区间与状态。"""
    rows = logic.get_timeline(contract_no)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTRACT_NOT_FOUND", "message": f"合同 '{contract_no}' 无任何费率版本"},
        )
    return TimelineResponse(contract_no=contract_no, versions=[to_view(r) for r in rows])


@app.get("/rates/{rate_id}/lineage", response_model=LineageResponse)
def rate_lineage(rate_id: int):
    """版本血缘：从目标版本沿 supersedes / retained_from 边向全部祖先展开的去重 DAG。
    节点含版本全部现有字段，边含 relation；节点按距目标从近到远、版本号稳定排序。
    没有出边的历史行视为根（单独返回目标节点、无边）。血缘损坏（环或悬空边）
    返回 409 LINEAGE_CORRUPT。"""
    try:
        result = logic.get_lineage(rate_id)
    except logic.RateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RATE_NOT_FOUND", "message": str(exc)},
        )
    except logic.LineageCorruptError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "LINEAGE_CORRUPT", "message": str(exc)},
        )
    return LineageResponse(
        target_rate_id=result["target_id"],
        nodes=[to_view(r) for r in result["nodes"]],
        edges=[LineageEdge(**e) for e in result["edges"]],
    )


@app.get("/contracts/{contract_no}/coverage", response_model=CoverageResponse)
def coverage(
    contract_no: str,
    from_: datetime = Query(alias="from", description="查询窗口起点（含），ISO-8601"),
    to: datetime = Query(description="查询窗口终点（不含），ISO-8601"),
    known_at: Optional[datetime] = Query(default=None, description="认知时点，缺省为当前时间"),
):
    """覆盖率分析：以 known_at 可见版本的业务区间边界把 [from, to) 切为连续片段，
    逐段标记 covered（附唯一版本依据）/ gap / conflict（附全部候选），
    分类与候选完全相同的相邻段合并。"""
    try:
        segments, ka = logic.compute_coverage(contract_no, from_, to, known_at)
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    return CoverageResponse(
        contract_no=contract_no,
        from_=iso(from_),
        to=iso(to),
        known_at=ka,
        segments=[to_coverage_segment(seg) for seg in segments],
    )


@app.get("/contracts/{contract_no}/changes", response_model=ChangesResponse)
def changes(
    contract_no: str,
    from_: datetime = Query(alias="from", description="查询窗口起点（含），ISO-8601"),
    to: datetime = Query(description="查询窗口终点（不含），ISO-8601"),
    before_known_at: datetime = Query(description="较早认知时点，ISO-8601"),
    after_known_at: datetime = Query(description="较晚认知时点，ISO-8601，须晚于 before_known_at"),
):
    """变更对比：分别按 before_known_at / after_known_at 两个认知时点筛选可见费率，
    以两侧业务区间边界的并集把 [from, to) 切为连续片段，逐段返回 before/after
    状态及版本依据并分类（added / removed / unchanged / rate_changed / conflict；
    分类与两侧候选完全一致的相邻段合并），并汇总各分类的持续微秒数。"""
    try:
        result = logic.compute_changes(contract_no, from_, to, before_known_at, after_known_at)
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.InvalidKnownAtOrderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_KNOWN_AT_ORDER", "message": str(exc)},
        )

    def side_view(side: dict) -> ChangeSideView:
        return ChangeSideView(
            status=side["status"],
            basis=to_view(side["candidates"][0]) if side["status"] == "covered" else None,
            candidates=[to_view(r) for r in side["candidates"]] if side["status"] == "conflict" else [],
        )

    return ChangesResponse(
        contract_no=contract_no,
        from_=iso(from_),
        to=iso(to),
        before_known_at=result["before_known_at"],
        after_known_at=result["after_known_at"],
        segments=[
            ChangeSegmentView(
                from_=seg["from"],
                to=seg["to"],
                change=seg["change"],
                duration_us=seg["duration_us"],
                before=side_view(seg["before"]),
                after=side_view(seg["after"]),
            )
            for seg in result["segments"]
        ],
        summary=ChangeSummary(**result["summary"]),
    )


@app.post("/contracts/{contract_no}/charge-preview", response_model=ChargePreviewResponse)
def charge_preview(contract_no: str, payload: ChargePreviewRequest):
    """计费预览：把每条账单行按 known_at 可见费率的业务边界拆分，
    按时长比例折算各版本费用并汇总；遇 gap/conflict 段时标记并拒绝总额。"""
    try:
        result = logic.compute_charge_preview(
            contract_no,
            [(line.from_, line.to, line.base_amount) for line in payload.lines],
            payload.known_at,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.InvalidAmountError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_AMOUNT", "message": str(exc)},
        )
    except logic.FutureKnownAtError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_KNOWN_AT", "message": str(exc)},
        )
    return ChargePreviewResponse(
        contract_no=contract_no,
        known_at=result["known_at"],
        status="ok" if result["ok"] else "rejected",
        total_amount=result["total_amount"],
        lines=result["lines"],
    )


@app.post("/contracts/{contract_no}/most-favored-audit", response_model=MostFavoredAuditResponse)
def most_favored_audit(contract_no: str, payload: MostFavoredAuditRequest):
    """最惠价合规审计（全程只读）：按同一 known_at 读取目标与同业合同的可见费率，
    以所有业务边界的并集把 [from, to) 切为连续片段；目标与全部同业均唯一覆盖时，
    以同业最低费率为 benchmark，目标费率超过 benchmark × (1 + max_markup) 判
    violation、否则 compliant；任一合同 gap/conflict 判 inconclusive 并列出原因。
    状态与判定依据完全相同的相邻段合并，并按状态汇总持续微秒数。"""
    try:
        result = logic.compute_most_favored_audit(
            contract_no=contract_no,
            peer_contract_nos=payload.peer_contract_nos,
            from_dt=payload.from_,
            to_dt=payload.to,
            known_at=payload.known_at,
            max_markup=payload.max_markup,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.InvalidMarkupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_MARKUP", "message": str(exc)},
        )
    except logic.InvalidPeerContractsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": str(exc)},
        )
    return MostFavoredAuditResponse(
        contract_no=contract_no,
        peer_contract_nos=payload.peer_contract_nos,
        from_=iso(payload.from_),
        to=iso(payload.to),
        known_at=result["known_at"],
        max_markup=payload.max_markup,
        segments=[
            MostFavoredSegmentView(
                from_=seg["from"],
                to=seg["to"],
                status=seg["status"],
                duration_us=seg["duration_us"],
                target_basis=(
                    to_view(seg["target_basis"])
                    if seg["target_basis"] is not None
                    else None
                ),
                benchmark=(
                    BenchmarkView(**seg["benchmark"]) if seg["benchmark"] else None
                ),
                threshold_rate=seg["threshold_rate"],
                reasons=[AuditIssueView(**r) for r in seg["reasons"]],
            )
            for seg in result["segments"]
        ],
        summary=MostFavoredSummary(**result["summary"]),
    )


@app.post(
    "/contracts/{contract_no}/rate-locks",
    response_model=RateLockCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_rate_lock(contract_no: str, payload: RateLockCreate):
    """锁定结算期费率：复用覆盖率切分规则校验 [from, to) 在 known_at 时点全部 covered。

    仅当整个窗口均为 covered 时，才在单一事务内按连续片段保存锁记录及各段的
    basis rate_id；存在 gap/conflict 返回 409 RATE_LOCK_UNSAFE 且不落库。
    """
    try:
        lock_rows, ka = logic.create_rate_lock(
            contract_no,
            payload.from_,
            payload.to,
            payload.known_at,
            payload.reason,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.RateLockUnsafeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RATE_LOCK_UNSAFE",
                "message": str(exc),
                "segments": [
                    to_coverage_segment(seg).model_dump(by_alias=True)
                    for seg in exc.segments
                ],
            },
        )
    return RateLockCreated(
        contract_no=contract_no,
        from_=iso(payload.from_),
        to=iso(payload.to),
        known_at=ka,
        locks=[to_lock_view(r) for r in lock_rows],
    )


@app.get("/contracts/{contract_no}/rate-locks", response_model=RateLockListResponse)
def list_rate_locks(contract_no: str):
    """合同的结算期费率锁记录：按锁定片段起点（写入顺序）列出全部锁及其 basis rate_id。"""
    if not logic.get_timeline(contract_no):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTRACT_NOT_FOUND", "message": f"合同 '{contract_no}' 无任何费率版本"},
        )
    return RateLockListResponse(
        contract_no=contract_no,
        locks=[to_lock_view(r) for r in logic.get_rate_locks(contract_no)],
    )


# ---------- 费率修订审批（独立模块/表，不影响单笔 /rates） ----------

@app.post(
    "/contracts/{contract_no}/revision-approvals",
    response_model=RevisionApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_revision_approval(contract_no: str, payload: RevisionApprovalCreate):
    """创建费率修订审批申请。

    复用修订预演（与 POST /rates 同一规划逻辑）：预演通过时保存提议参数、
    state_token 与 pending 状态；预演被锁相交 / recorded_at 单调性阻断时按
    预演口径返回错误且不产生申请。同一合同内业务区间与既有 pending / approved
    申请相交（左闭右开，仅端点相接允许）时返回 409 REVISION_APPROVAL_INTERVAL_CONFLICT，
    rejected / executed 申请不占区间。
    """
    try:
        row = approvals.create_approval(
            contract_no=contract_no,
            rate=payload.rate,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            recorded_at=payload.recorded_at,
            reason=payload.reason,
        )
    except approvals.ApprovalIntervalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_APPROVAL_INTERVAL_CONFLICT",
                "message": str(exc),
                "conflicts": [to_approval_view(r).model_dump() for r in exc.conflicts],
            },
        )
    except logic.RateWindowLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RATE_WINDOW_LOCKED",
                "message": str(exc),
                "locks": [to_lock_view(c["lock"]).model_dump(by_alias=True) for c in exc.conflicts],
                "intersections": [c["intersection"] for c in exc.conflicts],
            },
        )
    except logic.NonMonotonicRecordTimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NON_MONOTONIC_RECORDED_AT", "message": str(exc)},
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    return RevisionApprovalResponse(approval=to_approval_view(row))


@app.post(
    "/contracts/{contract_no}/revision-approvals/{approval_id}/review",
    response_model=RevisionApprovalResponse,
)
def review_revision_approval(
    contract_no: str, approval_id: int, payload: RevisionApprovalReview
):
    """审核费率修订申请：仅允许 pending → approved / rejected。

    携带 expected_revision 做乐观并发控制：与库中 revision 不一致返回
    409 REVISION_MISMATCH；目标不是 pending 返回 409 REVISION_APPROVAL_NOT_PENDING。
    """
    try:
        row = approvals.review_approval(
            contract_no=contract_no,
            approval_id=approval_id,
            action=payload.action,
            expected_revision=payload.expected_revision,
            comment=payload.comment,
        )
    except approvals.ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "REVISION_APPROVAL_NOT_FOUND", "message": str(exc)},
        )
    except approvals.ApprovalNotPendingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_APPROVAL_NOT_PENDING",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        )
    except approvals.ApprovalRevisionMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_MISMATCH",
                "message": str(exc),
                "expected_revision": exc.expected,
                "actual_revision": exc.actual,
            },
        )
    return RevisionApprovalResponse(approval=to_approval_view(row))


@app.post(
    "/contracts/{contract_no}/revision-approvals/{approval_id}/execute",
    response_model=RevisionApprovalExecutedResponse,
)
def execute_revision_approval(contract_no: str, approval_id: int):
    """执行已批准（approved）的费率修订申请。

    在同一 SQLite 事务内用申请保存的 state_token 调用现有条件提交，并把申请标记
    executed、记录 created rate_id 与完整结果。state_token 陈旧（预演后当前版本
    或锁集合已变化）时整体回滚、返回 409 STALE_REVISION_PREVIEW 且申请保留
    approved；对已 executed 申请重复执行幂等返回首次结果（idempotent=true）。
    pending / rejected 申请执行返回 409 REVISION_APPROVAL_NOT_APPROVED。
    """
    try:
        outcome = approvals.execute_approval(contract_no, approval_id)
    except approvals.ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "REVISION_APPROVAL_NOT_FOUND", "message": str(exc)},
        )
    except approvals.ApprovalNotApprovedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_APPROVAL_NOT_APPROVED",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        )
    except logic.StaleRevisionPreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STALE_REVISION_PREVIEW",
                "message": str(exc),
            },
        )
    except logic.InvalidStateTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_STATE_TOKEN", "message": str(exc)},
        )
    result = RateCreated.model_validate(outcome["result"])
    return RevisionApprovalExecutedResponse(
        approval=to_approval_view(outcome["approval"]),
        result=result,
        idempotent=outcome["idempotent"],
    )


@app.get(
    "/contracts/{contract_no}/revision-approvals",
    response_model=RevisionApprovalListResponse,
)
def list_revision_approvals(
    contract_no: str,
    status_filter: Optional[Literal["pending", "approved", "rejected", "executed"]] = Query(
        default=None, alias="status", description="可选状态过滤：pending/approved/rejected/executed"
    ),
):
    """列出合同的费率修订审批申请（按申请 id 升序），可用 status 过滤；合同无申请返回空数组。"""
    rows = approvals.list_approvals(contract_no, status_filter)
    return RevisionApprovalListResponse(
        contract_no=contract_no,
        approvals=[to_approval_view(r) for r in rows],
    )


# ---------- 结算批次（独立模块/表，不可变快照 + 幂等键） ----------

@app.post(
    "/contracts/{contract_no}/settlement-batches",
    response_model=SettlementBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_settlement_batch(
    contract_no: str,
    payload: SettlementBatchCreate,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
            description="必填幂等键：相同键且请求一致回放首次快照；相同键但请求不同返回 409",
        ),
    ],
):
    """创建结算批次。

    复用计费预览（POST /charge-preview）的区间切分、全覆盖校验与逐段计价：
    把每条账单行按 known_at 可见费率的业务边界拆分计价，**任一行出现 gap /
    conflict 片段时整批拒绝（409 SETTLEMENT_BATCH_UNSAFE）、不落库**（响应附
    与计费预览同构的逐行明细）。全部行可计价时，在**独立表** settlement_batches
    中**原子保存不可变快照**：规范化请求（known_at + 账单行）、逐段费率依据
    （rate_id / rate / charge / portion）、各行金额（line_total）与总额。

    必须携带 `Idempotency-Key` 请求头（幂等命名空间按合同隔离）：
    - 相同键且规范化请求一致：回放首次保存的快照，`idempotent=true`
      （HTTP 200，不再计价、不重新落库；即使此后费率已修订也返回历史快照）；
    - 相同键但请求不同：`409 IDEMPOTENCY_KEY_REUSED`，不覆盖首次快照；
    - 新键且校验通过：创建新快照，`idempotent=false`（HTTP 201）。
    """
    if not idempotency_key.strip():
        # 与缺头/请求体校验失败同一口径：交由全局 RequestValidationError 处理器，
        # 返回顶层 {"code": "INVALID_REQUEST", ...}。
        raise RequestValidationError(
            [PydanticCustomError(
                "blank_header",
                "Idempotency-Key 请求头不得为空白",
            )]
        )
    try:
        outcome = settlements.create_batch(
            contract_no=contract_no,
            lines=[
                (line.from_, line.to, line.base_amount) for line in payload.lines
            ],
            known_at=payload.known_at,
            idempotency_key=idempotency_key,
        )
    except settlements.IdempotencyKeyReusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": str(exc),
                "existing_batch_id": exc.existing_batch_id,
            },
        )
    except settlements.SettlementUnsafeError as exc:
        preview = exc.preview
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SETTLEMENT_BATCH_UNSAFE",
                "message": str(exc),
                "contract_no": contract_no,
                "known_at": preview["known_at"],
                "total_amount": None,
                "lines": preview["lines"],
            },
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.InvalidAmountError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_AMOUNT", "message": str(exc)},
        )
    except logic.FutureKnownAtError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_KNOWN_AT", "message": str(exc)},
        )
    if outcome["idempotent"]:
        # 幂等重放：非创建语义，返回 200；首次创建保持 201。
        response.status_code = status.HTTP_200_OK
    return SettlementBatchResponse(
        batch=to_batch_view(outcome["batch"]),
        idempotent=outcome["idempotent"],
    )


@app.get(
    "/contracts/{contract_no}/settlement-batches/{batch_id}",
    response_model=SettlementBatchResponse,
)
def get_settlement_batch(contract_no: str, batch_id: int):
    """查询结算批次快照（不可变，只追加）：返回创建时冻结的规范化请求、逐段费率依据、
    各行金额与总额；查询语义恒为 `idempotent=false`。批次不存在（或不属于路径合同）
    返回 404 SETTLEMENT_BATCH_NOT_FOUND。"""
    try:
        outcome = settlements.get_batch(contract_no, batch_id)
    except settlements.SettlementBatchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SETTLEMENT_BATCH_NOT_FOUND", "message": str(exc)},
        )
    return SettlementBatchResponse(batch=to_batch_view(outcome), idempotent=False)
