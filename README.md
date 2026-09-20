# 合同费率双时态追溯 API

基于 **Python + FastAPI + SQLite** 的合同费率追溯服务。录入费率时同时保存
**业务生效区间**（valid time）与**系统记录时间**（system time）两个维度；
修订既有区间只追加新版本、旧版本标记取代，历史永不覆盖。

## 一键启动

```bash
docker compose up --build
```

依赖安装与数据库建表全部自动完成。启动后访问：

- API 入口： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_PATH` | 容器内 `/data/rates.db`（本地直跑默认 `./rates.db`） | SQLite 数据库文件路径，启动时自动建表 |
| `STATE_TOKEN_SECRET` | 内置开发值 | 修订预演 `state_token` 的 HMAC 签名密钥，生产环境应覆盖 |
| `ports` | `8000:8000` | 在 `docker-compose.yml` 中修改宿主机端口 |

数据通过 Compose 命名卷 `rates-data` 持久化，删除容器不丢数据。

## 双时态模型

每条费率版本记录两个时间区间（均左闭右开）：

| 维度 | 字段 | 含义 |
|---|---|---|
| 业务时间 | `valid_from` ~ `valid_to` | 费率在现实中生效的区间，`valid_to` 缺省为无限期 |
| 系统时间 | `recorded_at` ~ `superseded_at` | 该记录被系统认知为真的区间，`superseded_at` 为 NULL 表示当前认知 |

修订 = 插入新版本（`version` 递增）并把区间重叠的旧版本置上 `superseded_at`，
旧行不修改、不删除，任意历史认知时点均可回放。

**部分重叠只替换交集（v1.2 起）**：当新区间 `[valid_from, valid_to)` 只压住
某条当前版本的一部分时，旧版本仅在交集内被取代；交集之外未被覆盖的
**左段 `[old.valid_from, valid_from)`、右段 `[valid_to, old.valid_to)`** 会按
旧版本的**原费率与原 `reason`** 追加为新的“补偿版本”。修订窗口之外的报价因此
保持不变，不会凭空产生 gap；旧版本开放上界（`valid_to` 缺省）时，右补偿片段
同样开放上界。一次修订同时覆盖多条旧版本时，每条各自保留窗口外侧片段。

取代标记、新版本与补偿版本在**同一事务**提交：先创建新费率（取得最小的新
版本号），补偿片段再按 `valid_from` 排序紧随其后，`version` 严格递增。

**可审计版本血缘（v1.4 起）**：每次修订还会在**同一事务**向 `rate_version_edges`
写入血缘边（只追加、不修改），边的方向为**后继版本 → 祖先版本**：

| 边 `relation` | 起点 | 终点 |
|---|---|---|
| `supersedes` | 本次新建费率 | 本次被取代的**每个**旧版本 |
| `retained_from` | 补偿片段（左/右保留版本） | 切出该片段的来源旧版本 |

因此一次部分重叠修订会同时产生 1 条（或多条）`supersedes` 边和每个补偿片段
各 1 条 `retained_from` 边。**没有任何出边的版本（含功能上线前录入的历史行）
视为血缘根**，血缘查询对根版本返回单节点、无边。

**结算期费率锁（v1.5 起）**：费率锁定把某个结算期窗口 `[from, to)` 在指定认知时点
`known_at` 下唯一可见的费率**固定下来**。锁定复用覆盖率切分规则：以该时点可见版本
的业务边界把窗口切为连续片段，**仅当整个窗口逐段均为 `covered`** 时，才在**单一事务**
内按片段写入锁记录（`rate_locks`，只追加），每段记录其当时唯一依据的 `basis rate_id`；
任一片段是 `gap` 或 `conflict`，整批拒绝（`409 RATE_LOCK_UNSAFE`）、**不落任何锁**。

锁一旦存在，`POST /rates` 的拟议区间与**任一锁定片段相交**（左闭右开，仅端点相接
不算）即被拒绝（`409 RATE_WINDOW_LOCKED`，响应列出相交的锁及各自交集），
**不新增版本、不产生补偿片段、不写血缘边**——锁外区间的正常修订不受影响。
锁记录不因后续修订而改变，可在审计时与当前费率对照。

**修订预演与条件提交（v1.6 起）**：`POST /contracts/{contract_no}/rate-revision-previews`
与 `POST /rates` 共用**同一规划逻辑**（锁相交优先 → `recorded_at` 单调性 → 部分重叠
补偿 → 版本号递增），但**全程只读、不写库**：返回将创建的主版本（含预计版本号）、
被取代版本、补偿片段；若会被锁或单调性阻断，则返回阻断原因（`ready=false`、不签发
令牌）。预演通过时签发 `state_token`（HMAC 签名的无状态令牌，绑定本次全部参数与
合同状态指纹）；`POST /rates` 携带该 token 时为**条件提交**：仅当合同的当前版本
集合与锁集合自预演以来未变化才真正落库，否则 `409 STALE_REVISION_PREVIEW`、
不写任何数据。令牌事实上一次性：成功提交后合同状态即变，同一 token 再用即失效。

**费率修订审批（v1.8 起）**：费率修订除了单笔 `POST /rates` 直接录入外，还提供
独立的**申请 → 审核 → 执行**审批工作流（独立模块 `approvals.py`、独立表
`rate_revision_approvals`，与费率/锁数据物理分离，单笔 `/rates` 行为完全不变）：

- **创建申请**：复用第 10 节的修订预演（同一规划逻辑、全程只读），预演通过时把
  提议参数、签发的 `state_token` 与 `pending` 状态一起冻结保存；被锁或
  `recorded_at` 单调性阻断时申请不落库。
- **区间互斥**：同一合同中业务区间**相交**（左闭右开，仅端点相接允许）的
  `pending` / `approved` 申请互斥（`409 REVISION_APPROVAL_INTERVAL_CONFLICT`）；
  `rejected` / `executed` 申请不再占用区间。
- **审核**：仅允许 `pending → approved / rejected`；请求携带
  `expected_revision` 做乐观并发控制，与库中 `revision` 不一致返回
  `409 REVISION_MISMATCH`，每次合法流转 `revision +1`。
- **执行**：仅允许 `approved`。在**同一 SQLite 事务**内用申请保存的
  `state_token` 调用现有**条件提交**（与 `POST /rates` 完全同路径），成功后标记
  `executed`、`revision +1`、记录 `created_rate_id` 与完整结果快照；
  `state_token` 陈旧（预演后合同当前版本或锁集合已变化）时整体回滚、返回
  `409 STALE_REVISION_PREVIEW` 且申请**保留 `approved`**（可在合同状态处理后重新
  尝试语义，但需重新创建申请获取新令牌）；对已 `executed` 的申请**重复执行幂等**，
  回放首次结果（`idempotent=true`），不重复写费率。

**最惠价合规审计（v1.7 起）**：`POST /contracts/{contract_no}/most-favored-audit`
按**同一认知时点** `known_at` 读取目标合同与若干同业合同的可见费率，以**所有合同
业务边界的并集**把审计窗口 `[from, to)` 切为连续片段：目标与全部同业均唯一覆盖时，
以**同业最低费率**为 benchmark，目标费率超过 `benchmark × (1 + max_markup)` 判
`violation`、否则判 `compliant`；任一合同在该段出现 `gap` / `conflict` 时判
`inconclusive` 并逐条列出原因（**绝不错判合规**）。状态与判定依据完全相同的相邻段
自动合并，并按状态汇总持续微秒数。审计**全程只读**，不产生任何写入。

**结算批次（v1.9 起）**：`POST /contracts/{contract_no}/settlement-batches` 把一组
账单行按 `known_at` 认知时点结算为**不可变批次快照**（独立模块 `settlements.py`、
独立表 `settlement_batches`，与费率/锁/审批数据物理分离，计费预览行为完全不变）：

- **复用计费预览**：区间切分与全覆盖校验与 `POST .../charge-preview` 同一函数、
  同一事务内连接，口径完全一致；任一行出现 `gap` / `conflict` 段时**整批拒绝**
  （`409 SETTLEMENT_BATCH_UNSAFE`，附逐行明细）且**不落任何数据**。
- **原子快照**：全部行可计价时，在同一 SQLite 事务（`BEGIN IMMEDIATE`）内把
  **规范化请求**与**结算快照**（逐段费率依据 `rate_id`/`rate`/`charge`、各行
  `line_total`、批次 `total_amount`）一次写入；快照只追加、不修改、不删除，
  后续费率修订不影响已落库批次。
- **幂等**：`Idempotency-Key` 请求头必填，`(contract_no, key)` 唯一。相同键且
  规范化请求一致时返回**首次快照**并标记 `idempotent=true`（不重复写库）；
  键相同但请求不同返回 `409 IDEMPOTENCY_KEY_REUSED`。

## 接口

### 1. 录入 / 修订费率 — `POST /rates`

```bash
curl -X POST http://localhost:8000/rates \
  -H 'Content-Type: application/json' \
  -d '{
        "contract_no": "HT-2026-001",
        "rate": 0.052,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to":   "2027-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T09:00:00Z",
        "reason": "初始费率"
      }'
```

- `valid_to`、`recorded_at`、`reason` 均可选；`recorded_at` 缺省为当前时间。
- 与当前版本区间重叠时自动修订：响应中 `superseded_rate_ids` 列出被取代版本，
  `retained_versions` 列出窗口外保留的补偿版本（原费率、原 `reason`，按
  `valid_from` 排序；完整覆盖或无重叠时为空列表）。
- 部分重叠只替换交集：左、右未覆盖部分追加为补偿版本，修订窗口外报价不变；
  历史 `known_at` 仍可看到未被切分的原版本（见下文“双时态模型”）。
- 同一合同 `recorded_at` 必须单调递增，否则 `422 NON_MONOTONIC_RECORDED_AT`；
  `valid_to <= valid_from` 返回 `422 INVALID_INTERVAL`。
- 拟议区间与任一**结算期费率锁**片段相交时返回 `409 RATE_WINDOW_LOCKED`
  （见第 8 节）：`detail.locks` 列出相交锁、`detail.intersections` 按同序列出
  各自交集 `{from, to}`；此时**不新增版本、不产生补偿片段、不写血缘边**。
  区间左闭右开，仅端点相接不算相交，锁外修订照常进行。
- 可携带修订预演签发的 `state_token` 进行**条件提交**（见第 10 节）：token 绑定的
  `contract_no` / `rate` / 区间 / `reason` 必须与本次请求一致（`recorded_at` 缺省时
  以 token 绑定的预演时间为准），且合同的当前版本集合与锁集合自预演以来未变化；
  否则分别返回 `422 INVALID_STATE_TOKEN` / `409 STALE_REVISION_PREVIEW`，
  均**不产生任何写入**。

部分重叠修订的响应示例（原费率 0.05 覆盖全年，新费率 0.09 只替换 4–6 月）：

```json
{
  "created": {"rate_id": 2, "rate": 0.09, "version": 2,
              "valid_from": "2026-04-01T00:00:00.000000+00:00",
              "valid_to": "2026-07-01T00:00:00.000000+00:00", "status": "current"},
  "superseded_rate_ids": [1],
  "retained_versions": [
    {"rate_id": 3, "rate": 0.05, "version": 3,
     "valid_from": "2026-01-01T00:00:00.000000+00:00",
     "valid_to": "2026-04-01T00:00:00.000000+00:00", "status": "current", "reason": "原费率"},
    {"rate_id": 4, "rate": 0.05, "version": 4,
     "valid_from": "2026-07-01T00:00:00.000000+00:00",
     "valid_to": "2027-01-01T00:00:00.000000+00:00", "status": "current", "reason": "原费率"}
  ]
}
```

### 2. 报价 — `GET /quote`

```bash
curl "http://localhost:8000/quote?contract_no=HT-2026-001&valid_at=2026-03-01T00:00:00Z&known_at=2026-06-01T00:00:00Z"
```

返回该双时态坐标下唯一可见且有效的费率及版本依据（`basis` 含版本号、
记录时间、生效区间）。`known_at` 缺省为当前时间。

| 情形 | 状态码 | `code` |
|---|---|---|
| 恰好一条命中 | 200 | — |
| 无匹配 | 404 | `NO_MATCH` |
| 多条重叠命中 | 409 | `OVERLAP`（附 `candidates` 冲突明细） |

### 3. 版本时间线 — `GET /contracts/{contract_no}/timeline`

```bash
curl http://localhost:8000/contracts/HT-2026-001/timeline
```

按版本号列出全部版本及其业务/系统时间区间与状态（`current` / `superseded`）；
合同不存在返回 `404 CONTRACT_NOT_FOUND`。

### 4. 覆盖率分析 — `GET /contracts/{contract_no}/coverage`

```bash
curl "http://localhost:8000/contracts/HT-2026-001/coverage?from=2026-01-01T00:00:00Z&to=2027-01-01T00:00:00Z&known_at=2026-06-01T00:00:00Z"
```

先按 `recorded_at` / `superseded_at` 筛出 `known_at` 时点可见的版本，
再以这些版本业务区间的全部边界把查询窗口 `[from, to)` 切分为**连续**片段，
逐段标记：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `covered` | 恰好一个可见版本覆盖该段 | `basis`：唯一版本依据 |
| `gap` | 无任何可见版本覆盖 | — |
| `conflict` | 多个可见版本重叠覆盖（脏数据） | `candidates`：全部候选版本 |

分类与候选完全相同的相邻片段会合并为一段。`known_at` 缺省为当前时间；
`from >= to` 返回 `422 INVALID_INTERVAL`；该认知时点下无任何可见版本时，
返回一个覆盖整个查询窗口的 `gap` 片段（合同不存在亦然，不报 404）。

响应示例（上半年为旧费率的补偿版本、下半年为修订后的新版本，全年连续无 gap）：

```json
{
  "contract_no": "HT-2026-001",
  "from": "2026-01-01T00:00:00.000000+00:00",
  "to": "2027-01-01T00:00:00.000000+00:00",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "segments": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-06-01T00:00:00.000000+00:00",
     "status": "covered", "basis": {"rate_id": 3, "rate": 0.052, "version": 3, "...": "..."},
     "candidates": []},
    {"from": "2026-06-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "status": "covered", "basis": {"rate_id": 2, "rate": 0.058, "version": 2, "...": "..."},
     "candidates": []}
  ]
}
```

### 5. 计费预览 — `POST /contracts/{contract_no}/charge-preview`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/charge-preview \
  -H 'Content-Type: application/json' \
  -d '{
        "known_at": "2026-06-01T00:00:00Z",
        "lines": [
          {"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z", "base_amount": 10000.0},
          {"from": "2026-07-01T00:00:00Z", "to": "2027-01-01T00:00:00Z", "base_amount": 12000.0}
        ]
      }'
```

每条账单行 `[from, to)` 按 `known_at` 时点可见版本的业务边界拆分
（与覆盖率分析同一切分规则），`covered` 段费用 =
`base_amount × (段时长 / 行时长) × 命中费率`；行内汇总为 `line_total`，
全部行再汇总为 `total_amount`。费用与时长占比 `portion` 均保留 6 位小数（预览口径）。
`known_at` 缺省为当前时间。

片段（`segments[]`）状态：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `covered` | 恰好一个可见版本覆盖该段 | `rate_id`、`rate`、`charge` |
| `gap` | 无任何可见版本覆盖 | — |
| `conflict` | 多个可见版本重叠覆盖（脏数据） | `candidates`：全部候选版本（`rate_id` / `rate` / `version`） |

账单行（`lines[]`）状态：`ok`（全部片段 covered）/ `gap` / `conflict` /
`mixed`（同时含 gap 与 conflict 段）。含 gap/conflict 段的行 `line_total` 为 `null`；
此时顶层 `status` 为 `rejected`、`total_amount` 为 `null`（**拒绝总额**），
但未受影响的行仍返回各自的计价结果。合同不存在或该认知时点无可见版本时，
所有行自然得到整行 `gap`，不报 404。

校验规则：

| 情形 | 状态码 | `code` |
|---|---|---|
| 账单行 `from >= to` | 422 | `INVALID_INTERVAL` |
| `base_amount` 为负或非有限数值 | 422 | `INVALID_AMOUNT` |
| `known_at` 晚于当前时间 | 422 | `INVALID_KNOWN_AT` |
| 缺少必填字段 / `lines` 为空 / 时间格式非法 | 422 | `INVALID_REQUEST` |

响应示例（两个版本各覆盖一行，全部可计价）：

```json
{
  "contract_no": "HT-2026-001",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "status": "ok",
  "total_amount": 1216.0,
  "lines": [
    {
      "from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
      "base_amount": 10000.0, "status": "ok", "line_total": 520.0,
      "segments": [
        {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
         "status": "covered", "portion": 1.0, "rate_id": 1, "rate": 0.052,
         "charge": 520.0, "candidates": []}
      ]
    },
    {
      "from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
      "base_amount": 12000.0, "status": "ok", "line_total": 696.0,
      "segments": [
        {"from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
         "status": "covered", "portion": 1.0, "rate_id": 2, "rate": 0.058,
         "charge": 696.0, "candidates": []}
      ]
    }
  ]
}
```

### 6. 变更对比 — `GET /contracts/{contract_no}/changes`

```bash
curl "http://localhost:8000/contracts/HT-2026-001/changes?from=2026-01-01T00:00:00Z&to=2027-01-01T00:00:00Z&before_known_at=2026-02-01T00:00:00Z&after_known_at=2026-06-01T00:00:00Z"
```

分别按 `before_known_at` / `after_known_at` 两个认知时点筛选可见费率，
以**两侧业务区间边界的并集**把 `[from, to)` 切分为连续片段，逐段返回
`before` / `after` 两侧的覆盖状态（结构与覆盖率分析的片段相同：`covered`
附 `basis` 唯一依据、`conflict` 附 `candidates` 全部候选），并给出变更分类：

| `change` | 含义 |
|---|---|
| `added` | before 为 `gap`、after 唯一覆盖（该认知区间内新可见的费率） |
| `removed` | before 唯一覆盖、after 为 `gap` |
| `unchanged` | 两侧均 `gap`，或均唯一覆盖且费率相同 |
| `rate_changed` | 两侧均唯一覆盖但费率不同 |
| `conflict` | 任一侧存在重叠冲突（脏数据） |

分类与两侧候选完全一致的相邻片段合并为一段；每段附 `duration_us`（持续微秒数），
响应末尾的 `summary` 按分类汇总持续微秒数（五个 `*_us` 字段恒存在，合计等于
窗口总时长）。四个时间参数均必填。

校验规则（均返回 422 及稳定错误码）：

| 情形 | `code` |
|---|---|
| `from >= to` | `INVALID_INTERVAL` |
| `before_known_at >= after_known_at` | `INVALID_KNOWN_AT_ORDER` |
| 缺少必填参数 / 时间格式非法 | `INVALID_REQUEST` |

响应示例（3 月起费率由 0.052 修订为 0.058：前半段同费率补偿版本 → `unchanged`，
后半段费率变化 → `rate_changed`）：

```json
{
  "contract_no": "HT-2026-001",
  "from": "2026-01-01T00:00:00.000000+00:00",
  "to": "2027-01-01T00:00:00.000000+00:00",
  "before_known_at": "2026-02-01T00:00:00.000000+00:00",
  "after_known_at": "2026-06-01T00:00:00.000000+00:00",
  "segments": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-03-01T00:00:00.000000+00:00",
     "change": "unchanged", "duration_us": 5097600000000,
     "before": {"status": "covered",
                "basis": {"rate_id": 1, "rate": 0.052, "version": 1, "...": "..."},
                "candidates": []},
     "after":  {"status": "covered",
                "basis": {"rate_id": 3, "rate": 0.052, "version": 3, "...": "..."},
                "candidates": []}},
    {"from": "2026-03-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "change": "rate_changed", "duration_us": 26438400000000,
     "before": {"status": "covered",
                "basis": {"rate_id": 1, "rate": 0.052, "version": 1, "...": "..."},
                "candidates": []},
     "after":  {"status": "covered",
                "basis": {"rate_id": 2, "rate": 0.058, "version": 2, "...": "..."},
                "candidates": []}}
  ],
  "summary": {"added_us": 0, "removed_us": 0, "unchanged_us": 5097600000000,
              "rate_changed_us": 26438400000000, "conflict_us": 0}
}
```

### 7. 版本血缘 — `GET /rates/{rate_id}/lineage`

```bash
curl http://localhost:8000/rates/2/lineage
```

从目标版本出发，沿 `supersedes` / `retained_from` 边（后继 → 祖先）向
**全部祖先**展开，返回去重后的有向无环图（DAG）：同一祖先经多条路径到达
（例如既被新费率取代、又是某补偿片段的来源）时只出现一次。

- `nodes`：目标版本及其全部祖先版本，节点字段与时间线/报价中的版本字段完全
  一致（`rate_id`、`rate`、`version`、业务/系统时间区间、`status`、`reason` 等），
  **按距目标从近到远排序（距离相同时按 `version` 升序）**；目标自身距离为 0。
- `edges`：展开到的全部血缘边，每条含 `from_rate_id`、`to_rate_id`、`relation`
  （`supersedes` / `retained_from`），按端点距离与 id 稳定排序。
- 目标版本没有任何出边时（初始版本或历史行）返回单节点、空边列表。

| 情形 | 状态码 | `code` |
|---|---|---|
| 正常展开（含根版本） | 200 | — |
| `rate_id` 不存在 | 404 | `RATE_NOT_FOUND` |
| 血缘存在环（不再是 DAG）或悬空边（祖先版本不存在） | 409 | `LINEAGE_CORRUPT` |

遍历使用带访问标记的迭代式广度优先展开 + Kahn 拓扑校验，即使血缘数据被直接
篡改成带环也**不会无限递归**，而是返回 `409 LINEAGE_CORRUPT`。

响应示例（v2 部分重叠取代 v1 并产生两个补偿版本 v3、v4；查询 `v2` 与 `v3`）：

```json
// GET /rates/2/lineage
{
  "target_rate_id": 2,
  "nodes": [
    {"rate_id": 2, "rate": 0.09, "version": 2, "...": "..."},
    {"rate_id": 1, "rate": 0.05, "version": 1, "status": "superseded", "...": "..."}
  ],
  "edges": [
    {"from_rate_id": 2, "to_rate_id": 1, "relation": "supersedes"}
  ]
}

// GET /rates/3/lineage（左补偿片段：1–3 月，保留原费率）
{
  "target_rate_id": 3,
  "nodes": [
    {"rate_id": 3, "rate": 0.05, "version": 3, "status": "current", "...": "..."},
    {"rate_id": 1, "rate": 0.05, "version": 1, "status": "superseded", "...": "..."}
  ],
  "edges": [
    {"from_rate_id": 3, "to_rate_id": 1, "relation": "retained_from"}
  ]
}
```

### 8. 锁定结算期费率 — `POST /contracts/{contract_no}/rate-locks`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/rate-locks \
  -H 'Content-Type: application/json' \
  -d '{
        "from": "2026-04-01T00:00:00Z",
        "to":   "2026-07-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z",
        "reason": "Q2 结算期锁价"
      }'
```

- 请求体：`from`、`to`、`known_at` 必填（均 ISO-8601），`reason` 可选（≤500 字）；
  窗口左闭右开 `[from, to)`。
- 服务端**复用覆盖率切分规则**：以 `known_at` 时点可见版本的业务边界把锁定窗口
  切为连续片段，逐段判定覆盖状态。
- 仅当**整个窗口在该认知时点均为 `covered`** 时，才在**单一事务**内按连续片段
  保存锁记录，每段记录该片段当时唯一可见版本的 `basis_rate_id`；全部片段共享
  本次的 `known_at` / `reason`，并各自带审计用 `created_at`。
- 任一片段是 `gap`（含合同不存在、版本在该认知时点尚不可见）或 `conflict`
  （重叠脏数据）时，返回 `409 RATE_LOCK_UNSAFE`，响应 `segments` 与覆盖率分析
   结构一致（`gap` 无依据、`conflict` 附全部候选），**整批回滚、不落任何锁**。

| 情形 | 状态码 | `code` |
|---|---|---|
| 整窗逐段 covered，锁已写入 | 201 | — |
| 窗口含 gap / conflict | 409 | `RATE_LOCK_UNSAFE`（附 `segments`） |
| `from >= to` | 422 | `INVALID_INTERVAL` |
| 缺必填字段 / 时间格式非法 | 422 | `INVALID_REQUEST` |

响应示例（窗口跨越两个可见版本：2 月是修订前费率的补偿版本 v3，4–6 月是修订后的 v2）：

```json
{
  "contract_no": "HT-2026-001",
  "from": "2026-02-01T00:00:00.000000+00:00",
  "to":   "2026-07-01T00:00:00.000000+00:00",
  "known_at": "2026-03-02T00:00:00.000000+00:00",
  "locks": [
    {"lock_id": 1, "contract_no": "HT-2026-001",
     "from": "2026-02-01T00:00:00.000000+00:00",
     "to":   "2026-04-01T00:00:00.000000+00:00",
     "known_at": "2026-03-02T00:00:00.000000+00:00",
     "basis_rate_id": 3, "reason": "Q2 结算期锁价",
     "created_at": "2026-09-19T10:00:00.000000+00:00"},
    {"lock_id": 2, "contract_no": "HT-2026-001",
     "from": "2026-04-01T00:00:00.000000+00:00",
     "to":   "2026-07-01T00:00:00.000000+00:00",
     "known_at": "2026-03-02T00:00:00.000000+00:00",
     "basis_rate_id": 2, "reason": "Q2 结算期锁价",
     "created_at": "2026-09-19T10:00:00.000000+00:00"}
  ]
}
```

锁定后，`POST /rates` 拟议区间与任一片段相交会被拒绝，响应示例：

```json
{
  "detail": {
    "code": "RATE_WINDOW_LOCKED",
    "message": "合同 'HT-2026-001' 的拟议区间 [...] 与 1 个结算期费率锁片段相交，锁定窗口内不得修订费率",
    "locks": [ {"lock_id": 2, "basis_rate_id": 2, "...": "..."} ],
    "intersections": [
      {"from": "2026-05-01T00:00:00.000000+00:00",
       "to":   "2026-06-01T00:00:00.000000+00:00"}
    ]
  }
}
```

### 9. 查询合同锁记录 — `GET /contracts/{contract_no}/rate-locks`

```bash
curl http://localhost:8000/contracts/HT-2026-001/rate-locks
```

按锁定片段起点（写入顺序）列出该合同的全部锁记录（只追加、不修改、不删除），
每条含锁定区间、`known_at`、`basis_rate_id`、`reason` 与 `created_at`；
多次锁定产生的片段一并返回。合同有费率版本但尚无锁时返回空数组；
合同不存在（无任何费率版本）返回 `404 CONTRACT_NOT_FOUND`。

### 10. 费率修订预演 — `POST /contracts/{contract_no}/rate-revision-previews`

```bash
# 预演：请求体与录入接口一致（contract_no 由路径给出），全程不写库
curl -X POST http://localhost:8000/contracts/HT-2026-001/rate-revision-previews \
  -H 'Content-Type: application/json' \
  -d '{
        "rate": 0.09,
        "valid_from": "2026-04-01T00:00:00Z",
        "valid_to":   "2026-07-01T00:00:00Z",
        "recorded_at": "2026-03-01T00:00:00Z",
        "reason": "年中调整"
      }'

# 条件提交：携带预演签发的 state_token，相关状态未变才落库
curl -X POST http://localhost:8000/rates \
  -H 'Content-Type: application/json' \
  -d '{
        "contract_no": "HT-2026-001",
        "rate": 0.09,
        "valid_from": "2026-04-01T00:00:00Z",
        "valid_to":   "2026-07-01T00:00:00Z",
        "recorded_at": "2026-03-01T00:00:00Z",
        "reason": "年中调整",
        "state_token": "<预演响应中的 state_token>"
      }'
```

- 请求体字段与 `POST /rates` 一致：`rate`、`valid_from` 必填，`valid_to` /
  `recorded_at` / `reason` 可选（`recorded_at` 缺省为当前时间）。
- 预演与正式提交**共用同一规划逻辑**：开放上界、多旧版本重叠、仅端点相接等
  情形下，将创建的主版本、被取代版本与补偿片段**完全一致**；预演**全程不写库**。
- `ready=true`：`created` 为将创建的主版本（含预计 `version`，尚无 `rate_id`），
  `superseded_versions` 为将被取代的当前版本（完整版本视图），
  `retained_versions` 为将追加的补偿片段（原费率、原 `reason`，版本号紧随主版本，
  按 `valid_from` 排序），并签发 `state_token`。
- `ready=false`：`blocking` 给出阻断原因（锁相交优先于 `recorded_at` 单调性），
  不签发令牌；`created` / `state_token` 为 `null`，两个版本列表为空。
- `state_token` 绑定本次全部参数与合同状态指纹（当前版本集合、版本号/记录时间
  水位、锁集合）；`POST /rates` 携带时仅当这些相关状态未变化才提交，令牌事实上
  一次性（成功提交后即失效）。

| 情形 | 状态码 | `code` |
|---|---|---|
| 预演完成（`ready` 为 true/false 均可能） | 200 | — |
| `valid_to <= valid_from` | 422 | `INVALID_INTERVAL` |
| 缺必填字段 / 时间格式非法 | 422 | `INVALID_REQUEST` |
| 提交时 token 非法、被篡改或与本次参数不一致 | 422 | `INVALID_STATE_TOKEN` |
| 提交时相关当前版本或锁集合已变化 | 409 | `STALE_REVISION_PREVIEW` |

预演响应示例（部分重叠：v2 将替换 4–6 月，v3/v4 为左右补偿片段）：

```json
{
  "contract_no": "HT-2026-001",
  "ready": true,
  "state_token": "eyJ2IjoxLCJjb250...<省略>...1a2b3c",
  "created": {"contract_no": "HT-2026-001", "rate": 0.09, "version": 2,
              "valid_from": "2026-04-01T00:00:00.000000+00:00",
              "valid_to": "2026-07-01T00:00:00.000000+00:00",
              "recorded_at": "2026-03-01T00:00:00.000000+00:00",
              "reason": "年中调整"},
  "superseded_versions": [
    {"rate_id": 1, "rate": 0.05, "version": 1, "status": "current", "...": "..."}
  ],
  "retained_versions": [
    {"contract_no": "HT-2026-001", "rate": 0.05, "version": 3,
     "valid_from": "2026-01-01T00:00:00.000000+00:00",
     "valid_to": "2026-04-01T00:00:00.000000+00:00",
     "recorded_at": "2026-03-01T00:00:00.000000+00:00", "reason": "原费率"},
    {"contract_no": "HT-2026-001", "rate": 0.05, "version": 4,
     "valid_from": "2026-07-01T00:00:00.000000+00:00",
     "valid_to": "2027-01-01T00:00:00.000000+00:00",
     "recorded_at": "2026-03-01T00:00:00.000000+00:00", "reason": "原费率"}
  ],
  "blocking": null
}
```

被阻断的预演响应示例（拟议区间与结算期锁相交）：

```json
{
  "contract_no": "HT-2026-001",
  "ready": false,
  "state_token": null,
  "created": null,
  "superseded_versions": [],
  "retained_versions": [],
  "blocking": {
    "code": "RATE_WINDOW_LOCKED",
    "message": "合同 'HT-2026-001' 的拟议区间 [...] 与 1 个结算期费率锁片段相交，锁定窗口内不得修订费率",
    "locks": [{"lock_id": 2, "basis_rate_id": 2, "...": "..."}],
    "intersections": [{"from": "2026-05-01T00:00:00.000000+00:00",
                       "to": "2026-06-01T00:00:00.000000+00:00"}]
  }
}
```

### 11. 最惠价合规审计 — `POST /contracts/{contract_no}/most-favored-audit`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/most-favored-audit \
  -H 'Content-Type: application/json' \
  -d '{
        "peer_contract_nos": ["HT-2026-002", "HT-2026-003"],
        "from": "2026-01-01T00:00:00Z",
        "to":   "2027-01-01T00:00:00Z",
        "known_at": "2026-06-01T00:00:00Z",
        "max_markup": 0.25
      }'
```

- 请求体：`peer_contract_nos`（同业合同号列表）、`from` / `to`（审计窗口，
  左闭右开）、`max_markup`（允许的最大加价幅度，**非负有限数值**，如 `0.25`
  表示 25%）必填；`known_at` 可选，缺省为当前时间，目标与全部同业**共用同一
  认知时点**。接口**全程只读**，不产生任何写入。
- 切分：以目标及全部同业在该时点可见版本的**业务边界并集**把窗口切为连续片段。
- 逐段判定（`segments[]`）：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `compliant` | 目标与全部同业均唯一覆盖，且目标费率 ≤ `benchmark × (1 + max_markup)` | `target_basis`、`benchmark`、`threshold_rate` |
| `violation` | 均唯一覆盖，但目标费率**严格大于** `benchmark × (1 + max_markup)` | 同上 |
| `inconclusive` | 任一合同在该段 `gap` / `conflict`，无法判定（**不会误判合规**） | `reasons`：问题合同与原因列表 |

- `benchmark` 为该段**同业最低费率**及其来源版本（并列最低时取同业列表中
  靠前者）；`threshold_rate = benchmark.rate × (1 + max_markup)`。
- `reasons[]` 目标合同在前、同业按输入顺序，每条含 `contract_no`、`issue`
  （`gap` / `conflict`）；`conflict` 附全部候选版本（`rate_id` / `rate` / `version`）。
- **状态与判定依据完全相同的相邻段合并**（compliant/violation 的依据为目标版本
  与 benchmark 版本；inconclusive 的依据为原因列表），每段附 `duration_us`；
  `summary` 按状态汇总持续微秒数（三个 `*_us` 字段恒存在，合计等于窗口总时长）。
- 目标或同业合同不存在（或该认知时点无可见版本）时不报 404，相应片段自然得到
  `inconclusive`（原因 `gap`）。

校验规则（均返回 422 及稳定错误码）：

| 情形 | `code` |
|---|---|
| `peer_contract_nos` 为空 | `EMPTY_PEER_CONTRACTS` |
| 目标合同混入同业列表 | `TARGET_IN_PEER_CONTRACTS` |
| 同业列表含重复合同 | `DUPLICATE_PEER_CONTRACT` |
| `max_markup` 为负或非有限数值 | `INVALID_MARKUP` |
| `from >= to` | `INVALID_INTERVAL` |
| 缺必填字段 / 同业合同号为空串 / 时间格式非法 | `INVALID_REQUEST` |

响应示例（上半年目标 0.06 超过 benchmark 0.04 × 1.25 = 0.05 判 `violation`，
下半年同业提价后合规）：

```json
{
  "contract_no": "HT-2026-001",
  "peer_contract_nos": ["HT-2026-002", "HT-2026-003"],
  "from": "2026-01-01T00:00:00.000000+00:00",
  "to": "2027-01-01T00:00:00.000000+00:00",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "max_markup": 0.25,
  "segments": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
     "status": "violation", "duration_us": 15638400000000,
     "target_basis": {"rate_id": 1, "rate": 0.06, "version": 1, "...": "..."},
     "benchmark": {"contract_no": "HT-2026-002", "rate_id": 2, "rate": 0.04},
     "threshold_rate": 0.05, "reasons": []},
    {"from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "status": "compliant", "duration_us": 15897600000000,
     "target_basis": {"rate_id": 1, "rate": 0.06, "version": 1, "...": "..."},
     "benchmark": {"contract_no": "HT-2026-002", "rate_id": 3, "rate": 0.055},
     "threshold_rate": 0.06875, "reasons": []}
  ],
  "summary": {"compliant_us": 15897600000000, "violation_us": 15638400000000,
              "inconclusive_us": 0}
}
```

### 12. 费率修订审批 — `/contracts/{contract_no}/revision-approvals`

独立的「申请 → 审核 → 执行」工作流，审批记录保存在独立表
`rate_revision_approvals`（启动时自动建表），不改单笔 `POST /rates` 的任何行为。

#### 12.1 创建申请 — `POST .../revision-approvals`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/revision-approvals \
  -H 'Content-Type: application/json' \
  -d '{
        "rate": 0.09,
        "valid_from": "2026-04-01T00:00:00Z",
        "valid_to":   "2026-07-01T00:00:00Z",
        "recorded_at": "2026-03-01T00:00:00Z",
        "reason": "年中调整（待审批）"
      }'
```

请求体与修订预演一致（`rate`、`valid_from` 必填，其余可选）。服务端复用预演：
预演通过才创建 `pending` 申请（`revision=1`），响应 `approval` 中回显冻结的提议
参数与 `state_token`；预演被阻断时申请不落库。

| 情形 | 状态码 | `code` |
|---|---|---|
| 申请已创建（pending） | 201 | — |
| 与同合同 pending/approved 申请业务区间相交（端点相接允许） | 409 | `REVISION_APPROVAL_INTERVAL_CONFLICT`（附 `conflicts` 冲突申请视图） |
| 拟议区间与结算期锁相交 | 409 | `RATE_WINDOW_LOCKED`（附锁与交集） |
| `recorded_at` 破坏单调性 / `valid_to <= valid_from` | 422 | `NON_MONOTONIC_RECORDED_AT` / `INVALID_INTERVAL` |
| 缺必填字段 / 时间格式非法 | 422 | `INVALID_REQUEST` |

#### 12.2 审核申请 — `POST .../revision-approvals/{id}/review`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/revision-approvals/1/review \
  -H 'Content-Type: application/json' \
  -d '{"action": "approve", "expected_revision": 1, "comment": "同意"}'
```

仅允许 `pending → approved/rejected`。`expected_revision` 为客户端读到的
`revision`，与库中不一致即拒绝（乐观锁，防止两个审核人互相覆盖）；流转成功后
`revision` 变为 2，并记录 `review_comment` / `reviewed_at`。

| 情形 | 状态码 | `code` |
|---|---|---|
| 审核成功 | 200 | — |
| 申请不存在（或不属于路径合同） | 404 | `REVISION_APPROVAL_NOT_FOUND` |
| 目标不是 pending | 409 | `REVISION_APPROVAL_NOT_PENDING`（附 `current_status`） |
| `expected_revision` 与当前 `revision` 不一致 | 409 | `REVISION_MISMATCH`（附 `expected_revision` / `actual_revision`） |
| `action` 非法 / 缺 `expected_revision` 等 | 422 | `INVALID_REQUEST` |

#### 12.3 执行申请 — `POST .../revision-approvals/{id}/execute`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/revision-approvals/1/execute
```

仅 `approved` 可执行：在**同一 SQLite 事务**内用申请保存的 `state_token` 走现有
条件提交逻辑写入费率（主版本、补偿片段、取代标记、血缘边与直接 `POST /rates`
完全一致），随后标记 `executed`、`revision +1`、记录 `created_rate_id` 与结果
快照——费率写入与审批状态更新原子提交，任一失败整体回滚。

- 预演后合同状态已变化（其他修订落库、新增锁等）→ token 陈旧：
  `409 STALE_REVISION_PREVIEW`，**申请保留 `approved`**、无任何费率写入。
- 对已 `executed` 申请重复执行：**幂等**返回首次保存的原结果，
  `idempotent=true`，不重复写费率。
- 目标为 `pending` / `rejected`：`409 REVISION_APPROVAL_NOT_APPROVED`
  （附 `current_status`）；申请不存在返回 404。

响应示例：

```json
{
  "approval": {"approval_id": 1, "status": "executed", "revision": 3,
               "created_rate_id": 2, "executed_at": "2026-09-20T10:00:00.000000+00:00",
               "...": "..."},
  "result": {
    "created": {"rate_id": 2, "rate": 0.09, "version": 2, "...": "..."},
    "superseded_rate_ids": [1],
    "retained_versions": [{"rate_id": 3, "...": "..."}, {"rate_id": 4, "...": "..."}]
  },
  "idempotent": false
}
```

#### 12.4 申请列表 — `GET .../revision-approvals[?status=pending|approved|rejected|executed]`

```bash
curl "http://localhost:8000/contracts/HT-2026-001/revision-approvals?status=pending"
```

按申请 id（创建顺序）升序列出审批记录；`status` 可选过滤，非法值返回
`422 INVALID_REQUEST`。合同无任何申请（含合同不存在）时返回
`{"contract_no": ..., "approvals": []}`，不报 404。

### 13. 结算批次 — `/contracts/{contract_no}/settlement-batches`

把一批账单行按 `known_at` 认知时点结算为**不可变快照**，记录保存在独立表
`settlement_batches`（启动时自动建表），不改计费预览的任何行为。

#### 13.1 创建结算批次 — `POST .../settlement-batches`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/settlement-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: settle-2026H1-001' \
  -d '{
        "known_at": "2026-06-01T00:00:00Z",
        "lines": [
          {"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z", "base_amount": 10000.0},
          {"from": "2026-07-01T00:00:00Z", "to": "2027-01-01T00:00:00Z", "base_amount": 12000.0}
        ]
      }'
```

- 请求体与计费预览一致：`lines`（至少一条）必填，`known_at` 可选（缺省为当前
  时间，不得晚于当前时间）；`Idempotency-Key` 请求头**必填**（1–255 字符）。
- 服务端**复用计费预览的区间切分与全覆盖校验**（同一事务内连接）：仅当**全部
  账单行逐段均为 `covered`** 时才创建批次，在同一事务内原子写入规范化请求与
  结算快照——`lines[]` 逐段附费率依据（`rate_id` / `rate` / `portion` /
  `charge`），逐行附 `line_total`，顶层附 `total_amount`；快照写入后**不可变**。
- 任一行含 `gap` / `conflict` 段：**整批拒绝、不落任何数据**，返回
  `409 SETTLEMENT_BATCH_UNSAFE`，`detail.lines` 与计费预览的行结构一致
  （`gap` 段无依据、`conflict` 段附全部候选），未受影响行仍返回各自计价结果。
- **幂等**：相同 `Idempotency-Key` 且规范化请求一致（`known_at` 缺省与否、
  各行区间与金额逐项相同）时，返回**首次保存的快照**并标记 `idempotent=true`
  （状态码 200，不重复写库）；键相同但请求不同返回
  `409 IDEMPOTENCY_KEY_REUSED`（`detail.batch_id` 为首次批次 id）。
  被拒绝（未落库）的请求不占用幂等键，修正后可用同键重试。

| 情形 | 状态码 | `code` |
|---|---|---|
| 批次已创建（首次） | 201 | — |
| 同键同请求幂等重放 | 200 | —（`idempotent=true`） |
| 同键不同请求 | 409 | `IDEMPOTENCY_KEY_REUSED`（附 `batch_id`） |
| 任一行含 gap / conflict | 409 | `SETTLEMENT_BATCH_UNSAFE`（附 `lines` 明细） |
| 缺 `Idempotency-Key` 头 / 空 `lines` / 缺字段 / 时间格式非法 | 422 | `INVALID_REQUEST` |
| 账单行 `from >= to` | 422 | `INVALID_INTERVAL` |
| `base_amount` 为负或非有限数值 | 422 | `INVALID_AMOUNT` |
| `known_at` 晚于当前时间 | 422 | `INVALID_KNOWN_AT` |

响应示例（两个版本各覆盖一行，全部可计价）：

```json
{
  "batch_id": 1,
  "contract_no": "HT-2026-001",
  "idempotency_key": "settle-2026H1-001",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "status": "ok",
  "total_amount": 1216.0,
  "lines": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
     "base_amount": 10000.0, "status": "ok", "line_total": 520.0,
     "segments": [
       {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
        "status": "covered", "portion": 1.0, "rate_id": 1, "rate": 0.052,
        "charge": 520.0, "candidates": []}
     ]},
    {"from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "base_amount": 12000.0, "status": "ok", "line_total": 696.0,
     "segments": [
       {"from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
        "status": "covered", "portion": 1.0, "rate_id": 2, "rate": 0.058,
        "charge": 696.0, "candidates": []}
     ]}
  ],
  "idempotent": false,
  "created_at": "2026-09-20T10:00:00.000000+00:00"
}
```

#### 13.2 查询结算批次 — `GET .../settlement-batches/{batch_id}`

```bash
curl http://localhost:8000/contracts/HT-2026-001/settlement-batches/1
```

返回创建时冻结的**不可变快照**（字段与创建响应一致，无 `idempotent` 字段）；
后续费率修订不影响已落库批次。批次不存在或不属于路径合同返回
`404 SETTLEMENT_BATCH_NOT_FOUND`。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 项目结构

```
app/
  main.py        FastAPI 路由与请求/响应模型
  logic.py       双时态核心逻辑（追加式录入、修订预演与条件提交、时点查询、时间线、覆盖率分段、计费预览、变更对比、版本血缘 DAG、结算期费率锁、最惠价合规审计）
  approvals.py   费率修订审批模块（申请创建/区间互斥、expected_revision 审核、同事务执行与幂等重放）
  settlements.py 结算批次模块（复用计费预览全覆盖校验、整批拒绝不落库、规范化请求与不可变快照原子落库、Idempotency-Key 幂等）
  db.py          SQLite 连接与自动建表（rate_versions + rate_version_edges 血缘边 + rate_locks 费率锁）
  timeutil.py    UTC 时间规范化（等宽 ISO 字符串，可字典序比较）
tests/test_api.py
Dockerfile / docker-compose.yml
```

审批表 `rate_revision_approvals` 与结算批次表 `settlement_batches` 分别由
`approvals.init_table()` / `settlements.init_table()` 在建库流程中幂等创建，
Docker Compose 一键启动（`docker compose up --build`）行为不变。
