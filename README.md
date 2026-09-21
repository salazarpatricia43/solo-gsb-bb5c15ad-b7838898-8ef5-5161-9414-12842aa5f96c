# Release Wave Planning API（发布波次规划服务）

基于 **Python 3.11 + FastAPI + OR-Tools CP-SAT** 的发布波次规划服务。客户端提交
服务依赖图、每波并发上限以及不能同波发布的故障域，服务给出**顺序确定、波次最少**
的发布方案；依赖成环或在给定发布窗口内无法排入时，返回**可定位到具体服务与约束**
的结构化原因。方案持久化到 SQLite，支持查询与删除。容器启动时自动安装依赖并建表。

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
| `invalid_request_field` | JSON/Schema 校验失败，附带字段位置 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans` | 提交并生成方案，成功 201，失败 422（结构化原因） |
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
  planner.py    # CP-SAT 建模、两阶段求解、不可行诊断
  db.py         # SQLite 初始化与 CRUD
  main.py       # FastAPI 路由、启动建表、异常处理
tests/          # 端到端测试（规划/错误定位/CRUD/确定性）
Dockerfile
docker-compose.yml
requirements.txt
```
