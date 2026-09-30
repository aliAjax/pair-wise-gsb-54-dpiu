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
- `tests/`：完整流程、规则计算、失败场景和区段接续档案测试。

## 区段接续档案

接续损耗不再只留在故障单里，而是按 `(光缆, 区段)` 沉淀到 `segment_splices` 档案：

- 每条接续记录包含现场发生时刻 `occurred_at`、接续点里程、损耗、工程师和来源，档案一律按现场时刻排序累计。
- `splice` 流程动作与现场补报都会在同一个 `BEGIN IMMEDIATE` 事务内写档案并更新故障单，累计损耗超过 `splice_loss_budget_db`（创建时可选，默认 0.5dB）时退回 `rectification`（待整改），由 `rectify` 动作确认整改并重设预算后继续。
- `tested` 之后到达的接续（晚到记录）会使原测试结果失效：清除 `test_passed`/端到端损耗并退回 `spliced`，必须重新测试。
- 同一物理接续（同时刻、同接续点）两名工程师并发提交时，只确认一条，另一条落 `pending_confirmation`，由维修经理 `resolve`（confirm 替换原档案 / reject 留痕）。
- 现场补报必须携带 `client_token`；服务端唯一索引保证写入失败后重试幂等，不重复累加。
- `restore` 在提交锁内按最新档案复核：本单必须有确认的接续记录、区段无待确认数据、累计损耗不超预算、测试仍然有效。旧单缺少接续记录时先用 `backfill_splices` 补录历史项（整批原子、幂等），补齐前不能恢复。

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
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.splice_loss_budget_db` 可选。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作含 `approve/mobilize/survey/splice/test/restore/cancel/rectify`；`splice` 的 data 可带 `occurred_at`、`splice_point_km`、`engineer_id`、`client_token`。
- `POST /api/records/{id}/splices`：现场补报接续（`cable_engineer`），data 为 `{splice_loss_db,splice_point_km,occurred_at,engineer_id,client_token}`，返回 `outcome=accepted|pending|duplicate`，pending 时 HTTP 202。
- `POST /api/records/{id}/splices/backfill`：补录历史接续，data 为 `{"splices":[{...}]}`，整批原子生效、可幂等重放。
- `POST /api/splices/{id}`：裁决待确认接续，请求体为`{"action":"resolve","data":{"resolution":"confirm|reject","note":"..."}}`。
- `GET /api/splices`：区段接续档案查询，可带 `cable`、`segment`、`status`、`limit`；指定区段时附带 `confirmed_count` 和 `cumulative_splice_loss_db`。
- `GET /api/splices/{id}`：单条接续记录。

故障单状态：`detected → approved → mobilized → surveyed → spliced → tested → restored`，超预算进入 `rectification`（整改后回 `spliced`），`detected/approved/mobilized/rectification` 可 `cancel`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及接续档案的按时刻累计、超预算退回、晚到测试失效、并发只收一条、重试幂等、历史补录和恢复前复核。
