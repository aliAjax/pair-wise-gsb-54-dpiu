# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data`可选`splice_loss_budget_db`指定区段接续损耗预算（默认0.5dB）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/cables/{cable}/segments/{segment}/splice-archive`：区段接续档案（confirmed/pending/duplicate条目、按现场时刻排序、累计损耗）。
- `POST /api/splice-entries/{id}/confirm`：复核暂存的现场数据，请求体为`{"decision":"confirm|duplicate"}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 区段接续档案

状态机在原流程上新增`rectification`（待整改）：

```
surveyed ──splice──► spliced ──test──► tested ──restore──► restored
   ▲                   │  ▲              │
   └──── rectification ◄┴──┴────────────┘（累计超预算 / 晚到记录使测试失效）
```

- **接续上报`splice`**：数据含`occurred_at`（现场时刻）、`splice_loss_db`、`spare_used_km`，可选`report_id`（现场报告单号，幂等）、`idempotency_key`、`engineer_id`、`note`。单条损耗>0.2dB直接拒绝。
- **按区段累计**：接续不再只留在故障单，而以`(cable, segment)`归档，按`occurred_at`累计。累计超过区段预算（默认0.5dB）时，本单和同区段在途单一律退回`rectification`。
- **并发只收一条**：两名工程师基于同一版本同时提交：先到者`confirmed`并推进工单，另一条存为`pending`现场数据；同一现场时刻的暂存条目复核时用`decision=duplicate`判重，绝不重复累加。
- **失败重试幂等**：整个判定与写入在`BEGIN IMMEDIATE`事务内完成；写入失败整体回滚。带相同`report_id`或`idempotency_key`的重试直接回放首次结果（响应中`splice_report.status=replayed`），不二次累加。
- **晚到记录**：工单已越过接续环节时上报只暂存（`pending`）；`confirm`入档后，已`tested`的工单原测试结果失效（必须复测），累计超预算的退回`rectification`。
- **旧单补历史项`backfill`**：旧单档案里没有任何接续记录时，`test`/`restore`会被拒绝（409，提示先补历史项）。补录请求体`{"expected_version":n,"data":{"items":[{"occurred_at":"...","splice_loss_db":0.1,"engineer_id":"..."}]}}`；补齐后按最新档案复核，超预算同样退回`rectification`。
- **恢复流量前复核`restore`**：在锁内读取最新档案——无接续记录拦截、未测试拦截、测试后档案序号变化（`archive_seq_at_test`不匹配，即有晚到记录）拦截要求复测、累计超预算拦截。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及区段接续档案的累计预算、并发去重、重试幂等、晚到记录失效和旧单补历史项。
