# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/advisory.py`：限行建议评估——数据类别校验、风险定级、最高风险合成、触发项、会签状态机和恢复门槛（纯函数）。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

评估（`advisory.py`）、存储（`repository.py`）和入口（`http_api.py`）分开承担，`service.py`只做编排。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

### 限行建议台

- `POST /api/items/{id}/readings`：登记数据，`kind`为`sensor_peak`（传感峰值）、`inspection_defect`（巡检缺陷）、`vehicle_load`（车型轴重）或`weather`（桥面天气）。
- `GET /api/items/{id}/readings`
- `POST /api/items/{id}/readings/{rid}/review`：复核数据，`decision`为`confirm`或`void`（作废后不参与评估）。
- `POST /api/items/{id}/readings/{rid}/close`：关闭巡检缺陷。
- `GET /api/items/{id}/advisory`：当前建议版本。
- `GET /api/items/{id}/advisories`：全部建议版本（失效留档）。
- `POST /api/items/{id}/advisory/sign`：会签，`decision`为`approve`或`reject`。
- `POST /api/items/{id}/advisory/restore`：恢复通行，需提交`notice_removed: true`。

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 限行建议流程

1. **登记**：值守（sensor_operator）和工程（bridge_engineer）登记四类数据，每条数据按类别定级：传感峰值和车型轴重按与阈值/限值之比分档，巡检缺陷按等级（minor/moderate/serious/critical），桥面天气按状况（clear/rain/snow/strong_wind/ice/storm）。
2. **评估**：取全部有效数据的最高风险，给出观察（observe）、限载（load_limit）、限行（restrict）或封闭（closure）建议，触发项（达到最高等级的数据）写入建议版本。
3. **重算**：新增、复核或关闭缺陷后自动重算；结论（等级或触发项）变化时旧版本标记`superseded`失效留档，生成新会签版本，结论不变则不产生新版本。
4. **会签**：工程（bridge_engineer）与路政（traffic_authority）分别会签，双方同意才`released`；意见不一致进入`pending_dispute`待决，不能直接放行，待决中可再签直至一致；双方均拒绝则`rejected`。
5. **恢复**：全部巡检缺陷关闭且路政确认撤除通告（`notice_removed`）后，由路政执行恢复，生成`released`的正常通行版本。

数据复核与缺陷关闭由工程（bridge_engineer）执行，恢复只能由路政（traffic_authority）执行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
