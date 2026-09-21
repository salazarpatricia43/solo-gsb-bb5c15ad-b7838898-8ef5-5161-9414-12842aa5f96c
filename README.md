# Release Wave Planning API（发布波次规划服务）

基于 **Python 3.11 + FastAPI + OR-Tools CP-SAT** 的发布波次规划服务。客户端提交
服务依赖图、每波并发上限以及不能同波发布的故障域，服务给出**顺序确定、波次最少**
的发布方案；依赖成环或在给定发布窗口内无法排入时，返回**可定位到具体服务与约束**
的结构化原因。方案持久化到 SQLite，支持查询与删除；`POST /plans/{id}/simulate`
可在不落库的前提下模拟约束变更对方案的影响。容器启动时自动安装依赖并建表。

## 一条命令启动

```bash
docker compose up --build -d
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | API 根地址 |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger UI（可直接发请求） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |

停止并保留数据：`docker compose down`；连同数据卷一起清除：`docker compose down -v`。

> 端口可通过环境变量调整：`PLANNER_PORT=9000 docker compose up --build -d`。

## 请求示例

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "payment-platform",
    "services": ["web", "api", "auth", "db", "cache"],
    "dependencies": [
      {"service": "web", "depends_on": "api"},
      {"service": "api",  "depends_on": "auth"},
      {"service": "api",  "depends_on": "db"},
      {"service": "auth", "depends_on": "db"}
    ],
    "max_concurrency": 2,
    "fault_domains": [
      {"domain": "az-a", "services": ["db", "cache"]}
    ],
    "max_waves": null
  }'
```

响应：

```json
{
  "id": 1,
  "name": "payment-platform",
  "waves": [
    {"index": 1, "services": ["db"]},
    {"index": 2, "services": ["auth", "cache"]},
    {"index": 3, "services": ["api"]},
    {"index": 4, "services": ["web"]}
  ],
  "wave_count": 4,
  "max_concurrency": 2,
  "max_waves": null,
  "optimal": true,
  "created_at": "2026-09-21T12:00:00+00:00"
}
```

## 约束语义

- **依赖（dependencies）**：`service -> depends_on`，被依赖服务必须出现在更早的波次；
  依赖图必须是 DAG。
- **每波并发上限（max_concurrency）**：每波服务数不超过该值。
- **故障域（fault_domains）**：同一 `domain` 内的任意两个服务不能出现在同一波
  （反亲和）。典型用法：一个物理故障域（机房/可用区/机架）对应一条记录，列出其中的
  服务；若两个服务跨多个域同时冲突，只需让它们在任一域内同组即可。
- **发布窗口（max_waves，可选）**：传入后若最优方案需要更多波，返回 422 及原因；
  不传则以「波次最少」为目标（DAG 下始终可排）。

**确定性**：CP-SAT 固定随机种子、单搜索线程，并在「最少波次」基础上做第二阶段
优化，使服务尽量靠前且波内按 id 排序。相同输入永远得到相同输出。
`optimal=true` 表示求解器证明了波次最少；`false` 仅在触发求解时间上限时出现
（此时方案仍可行但不保证最优）。

## 约束变更模拟（POST /plans/{id}/simulate）

在已存储方案的基础上评估「如果约束这样改，方案会变成什么样」。**只读不落库**，
不影响已有记录。请求体所有字段均可选，未提供的约束保持原方案的值：

```bash
curl -X POST http://localhost:8000/plans/1/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "max_concurrency": 3,
    "add_dependencies": [{"service": "web", "depends_on": "cache"}],
    "remove_dependencies": [{"service": "api", "depends_on": "db"}],
    "add_fault_domains": [{"domain": "az-b", "services": ["api", "auth"]}],
    "remove_fault_domains": ["az-a"]
  }'
```

变更语义：

- **删除**必须命中已存储的约束，否则 422（`dependency_not_found` /
  `fault_domain_not_found`），便于发现客户端与服务端的状态漂移。
- **修改故障域** = 同一请求内先 `remove_fault_domains` 再同名 `add_fault_domains`；
  未删除就重复添加同名域返回 422 `duplicate_fault_domain`。
- 添加已存在的依赖边视为无操作（与创建时重复边静默合并一致）。
- 原方案的 `max_waves` 发布窗口**不参与**模拟——模拟直接报告波次增减。

求解目标按字典序：**先保证新方案波次最少，再在其中最小化相对原方案的换波
服务数**，最后做与创建相同的规范化，保证确定性（`optimal=true` 表示波次数与
换波数都被证明最小；触发求解时间上限时为 `false`，结果仍可行但不保证最优）。

响应：

```json
{
  "plan_id": 1,
  "name": "payment-platform",
  "waves": [
    {"index": 1, "services": ["db"]},
    {"index": 2, "services": ["auth", "cache"]}
  ],
  "wave_count": 5,
  "original_wave_count": 4,
  "wave_delta": 1,
  "max_concurrency": 3,
  "optimal": true,
  "assignments": [
    {"service": "api", "from_wave": 3, "to_wave": 4}
  ],
  "moved_services": ["api"],
  "affected_services": ["api", "auth", "cache", "web"]
}
```

| 字段 | 含义 |
| --- | --- |
| `waves` / `wave_count` | 模拟得到的新波次（结构同创建响应） |
| `original_wave_count` / `wave_delta` | 原波次数与增减量（新 − 原，正为增波） |
| `assignments` | 每个服务的原/新波次（1 起始，按服务 id 排序） |
| `moved_services` | 换了波次的服务 |
| `affected_services` | 被**新增**约束直接引用的服务（新增依赖的两端、新增故障域的成员） |

模拟不可行时返回与创建相同的结构化 422：新增依赖成环（`dependency_cycle`）、
引用未知服务（`unknown_dependency_*` / `unknown_fault_domain_member`）、
删除不存在的约束等，均可定位到具体服务与约束。

## 错误结构（422）

所有失败使用统一信封 `error + message + issues[]`，每个 issue 可定位：

```json
{
  "error": "planning_failed",
  "message": "The dependency graph contains a cycle.",
  "issues": [
    {
      "code": "dependency_cycle",
      "message": "... First cycle: x -> y -> x",
      "constraint": "dependencies",
      "service": "x",
      "related_services": ["x", "y"],
      "context": {"cycles": [["x", "y"]], "cycle_count": 1}
    }
  ]
}
```

| issue code | 定位的问题 |
| --- | --- |
| `dependency_cycle` | 依赖成环（含自依赖），给出具体环路径 |
| `unknown_dependency_service` / `unknown_dependency_target` | 依赖引用了不存在的服务 |
| `unknown_fault_domain_member` / `duplicate_*` | 故障域引用未知服务或重复声明 |
| `dependency_chain_too_long` | 最长依赖链所需波次超过 `max_waves`（给出关键路径） |
| `concurrency_cap_too_low` | `ceil(服务数 / 并发上限)` 超过 `max_waves` |
| `fault_domain_anti_affinity` | 故障域反亲和所需的最少波次超过 `max_waves` |
| `release_window_infeasible` | 组合约束下窗口不可行，附无窗口时所需的确切波次 |
| `dependency_not_found` / `fault_domain_not_found` | 模拟请求要求删除不存在的依赖/故障域 |
| `invalid_request_field` | JSON/Schema 校验失败，附带字段位置 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans` | 提交并生成方案，成功 201，失败 422（结构化原因） |
| POST | `/plans/{id}/simulate` | 约束变更影响模拟（不落库）：新波次、波次增减、逐服务新旧波次、受影响服务 |
| GET | `/plans?limit=50&offset=0` | 方案列表（按 id 倒序分页） |
| GET | `/plans/{id}` | 查询单个方案（含完整波次与原始约束） |
| DELETE | `/plans/{id}` | 删除方案，成功 200，不存在 404 |
| GET | `/health` | 健康检查 |

## 必要配置（环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PLANNER_DB_PATH` | `/data/plans.db` | SQLite 文件路径，目录自动创建 |
| `PLANNER_SOLVER_TIME_LIMIT` | `10` | 每个求解阶段的时间上限（秒） |
| `PLANNER_SOLVER_WORKERS` | `1` | CP-SAT 搜索线程数（1 保证确定性） |
| `PLANNER_PORT`（仅 compose） | `8000` | 宿主机映射端口 |

数据存储在 compose 命名卷 `planner-data`（容器内 `/data`），重启不丢记录。
数据库表在**每次启动时**通过 `CREATE TABLE IF NOT EXISTS` 自动创建，无需手工迁移。

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PLANNER_DB_PATH=./data/plans.db uvicorn app.main:app --reload --port 8000
```

运行测试（额外需要 pytest、httpx）：

```bash
pip install -r requirements.txt pytest httpx
python3 -m pytest -q
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  schemas.py    # 请求/响应模型与错误结构
  errors.py     # 结构化规划异常
  graph.py      # 依赖图：环检测（Tarjan SCC）、关键路径
  planner.py    # CP-SAT 建模、两阶段求解、变更模拟、不可行诊断
  db.py         # SQLite 初始化与 CRUD
  main.py       # FastAPI 路由、启动建表、异常处理
tests/          # 端到端测试（规划/错误定位/CRUD/确定性）
Dockerfile
docker-compose.yml
requirements.txt
```
