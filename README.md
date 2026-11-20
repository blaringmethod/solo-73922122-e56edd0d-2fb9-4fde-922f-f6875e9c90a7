# 实验室 FHIR R4 报告接入研究库（Lab FHIR Research Ingest API）

FastAPI 实现的 FHIR R4 事务型 Bundle 接入服务。检验科报告包以 transaction
Bundle 提交，服务在**单数据库事务**内完成条件建患、观测/报告的新增或版本化
更新，并通过**接入单号幂等账本**保证“同内容重试复用首次回执、异内容拒绝、
响应丢失/进程重启不重复应用”。

默认部署形态为**两个 API 实例共享同一个 SQLite 数据库文件**（WAL 模式）；
版本化更新（PUT + If-Match）通过**原子比较并交换（CAS）认领**实现：
同一资源基于同一版本，全库最多成功更正一次，与实例数、进程数无关。

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 实例 1：<http://localhost:8001/docs>（Swagger UI，可直接在页面上发起 POST /fhir）
- 实例 2：<http://localhost:8002/docs>
- 两个实例挂载同一卷 `sqlite-data`，共用 `/data/research.db`：在任一实例
  提交的更正对另一实例立即可见，并受同一套版本约束。
- 健康检查：`GET /health`；OpenAPI：`/openapi.json`

首次启动时两个容器都会尝试建表（`app/db_init.py`，并发建表已做重试容忍）。

生产形态的 PostgreSQL 配置仍保留在 compose profile 中：

```bash
docker compose --profile postgres up --build db api-pg   # http://localhost:8000/docs
```

## 2. 提交报告包

`POST /fhir`，请求头必须携带接入单号：

```
Idempotency-Key: LAB-ACC-1001
Content-Type: application/json
```

示例（注意条目故意乱序：报告 → 观测 → 患者，依赖 `urn:uuid` 前向引用）：

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0003-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "result": [
          {"reference": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001"}
        ]
      },
      "request": {"method": "POST", "url": "DiagnosticReport"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "valueQuantity": {"value": 5.5, "unit": "mmol/L"}
      },
      "request": {"method": "POST", "url": "Observation"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Patient",
        "identifier": [{"system": "http://hospital/mrn", "value": "MRN-001"}]
      },
      "request": {"method": "POST", "url": "Patient"}
    }
  ]
}
```

### 更正（带 If-Match 的乐观锁）

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "resource": {
        "resourceType": "DiagnosticReport",
        "id": "<已有报告id>",
        "status": "final",
        "code": {"coding": [{"code": "24323-8"}]},
        "subject": {"reference": "Patient/<患者id>"},
        "result": [{"reference": "Observation/<观测id>"}]
      },
      "request": {
        "method": "PUT",
        "url": "DiagnosticReport/<已有报告id>",
        "ifMatch": "W/\"1\""
      }
    }
  ]
}
```

- `ifMatch` 与当前版本不一致 → `412 Precondition Failed`（OperationOutcome），
  整包拒绝：报告正文、观测关联、版本历史均不因该包改变。
- **同一报告基于同一版本最多成功更正一次**：两个更正包并行提交时，数据库以
  单条 `UPDATE ... WHERE id AND version` 原子认领版本号，后到者必然得到 412，
  不会出现两个“第 2 版”或观测关联混杂。
- 更正被拒后，接入员可用**原单号**携带新的 If-Match 版本重提（失败会释放
  幂等槽）；成功的包按原内容重试则返回首次回执。
- 更新包内引用已存在资源请使用绝对引用（`Patient/<id>`、`Observation/<id>`）；
  `urn:uuid` 仅在同一 Bundle 内、且对应条目存在时有效。

## 3. 回执与读取接口

提交成功返回 `transaction-response` Bundle：

- `entry[].response`：FHIR 风格的 status / etag / location；
- `resourceMap[]`：按请求顺序给出每个条目的 `resourceType / id / version / action / fullUrl` 映射。

读取接口（均支持按患者维度）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/patients?identifier=system|value` | 按标识查患者 |
| GET | `/patients/{id}` | 患者摘要（含报告/观测计数） |
| GET | `/patients/{id}/observations` | 该患者全部观测 |
| GET | `/patients/{id}/diagnostic-reports` | 该患者全部报告（含观测 id 列表） |
| GET | `/observations/{id}` | 单个观测（响应头带 `ETag: W/"v"`） |
| GET | `/diagnostic-reports/{id}` | 单个报告 |
| GET | `/observations/{id}/_history` | 观测全部历史版本 |
| GET | `/diagnostic-reports/{id}/_history` | 报告全部历史版本 |
| GET | `/patients/{id}/_history` | 患者历史版本 |

## 4. 业务规则与失败语义

1. **条目上限 20**，仅允许 `Patient / Observation / DiagnosticReport`；
   超限或类型不符 → `400`。
2. **患者条件创建**：按 `identifier.system/value` 命中则复用，否则新建；
   包内标识重复 → `400`；并发同标识由数据库唯一约束 + 写事务串行化保证只建一份。
3. **乱序前向引用**：`urn:uuid` 在解析阶段统一绑定，与条目顺序无关；
   无法解析、引用不存在的外部资源 → `422`，整包拒绝。
4. **患者一致性**：每份报告与其全部观测必须同属一个患者；把观测改挂其他
   患者时，若已有引用报告未在**同一 Bundle** 内一并更正 → `422`。
5. **原子性**：任何校验/版本/引用失败都会回滚整个事务，不留任何资源或
   修改；其他患者已提交的数据不受影响，后续合法 Bundle 仍可正常提交。
6. **并发更正（CAS 认领）**：PUT 更新在进入写事务后以一条
   `UPDATE ... WHERE id = :id AND version = :if_match` 原子认领目标版本。
   认领成功才继续后续校验与写入；认领失败（版本过期/资源不存在）→
   `412` / `404` 并回滚整包。该机制在 SQLite 与 PostgreSQL 上语义一致，
   不依赖 `SELECT ... FOR UPDATE`（SQLite 不支持）。
7. **校验不加锁**：Bundle 的结构校验（`validate_bundle`）是纯 Python 计算，
   不持有任何数据库锁；一个包在校验期间，另一实例仍可提交不涉及这些报告
   的更正。锁（SQLite 为库级写锁，PostgreSQL 为行级锁）只在短暂的写入
   阶段持有。
8. **幂等**：
   - 同 `Idempotency-Key` + 同内容（规范化 JSON 哈希，与键序/空白无关）→
     直接返回首次回执，不重复落库；
   - 同 key + 异内容 → `409`；
   - 失败的提交会释放幂等槽，修正后可用同一单号重试；
   - 幂等状态持久化在数据库中，进程重启/响应丢失后重试仍然安全。
9. 所有错误均返回 FHIR `OperationOutcome`（`application/fhir+json`）。

## 5. 本地开发与自测（无需 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL="sqlite:////tmp/research.db"   # 或指向 PostgreSQL
python -m app.db_init
uvicorn app.main:app --reload
pytest -q
```

测试默认连接 `postgresql+psycopg2://research@localhost:5433/researchdb_test?host=/tmp`，
可用 `DATABASE_URL` 覆盖为文件型 SQLite（如 `sqlite:////tmp/test.db`）。
并发用例（同标识建患、If-Match 竞争、双更正竞争）在 PostgreSQL 与文件型
SQLite 下都会执行；仅内存型 SQLite（连接间互不可见）跳过。

## 6. 目录结构

```
app/
  main.py            FastAPI 入口、异常处理
  config.py          环境配置
  database.py        引擎/会话/建表（SQLite：显式 BEGIN + WAL + busy_timeout）
  models.py          ORM（患者/标识/观测/报告/关联/版本/幂等账本，PG JSONB）
  errors.py          OperationOutcome 错误体系
  fhir_utils.py      ETag/版本工具
  routers/           /fhir 提交与资源读取路由
  services/
    bundle.py        Bundle 校验、幂等、CAS 认领、事务处理、患者一致性规则
    queries.py       读取与历史查询
scripts/entrypoint.sh  容器启动（等库+建表）
tests/               45 个 pytest 用例（含并发更正、重启、原子性）
docker-compose.yml   api1 + api2 两实例共享 SQLite 卷；postgres 为可选 profile
```
