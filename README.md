# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/assessment.py`：评估。四类登记数据各自映射风险等级，按最高风险给出观察、限载、限行或封闭，并输出触发项。
- `src/repository.py`：存储。SQLite建表、事务、建议版本控制（失效留档）和审计链。
- `src/service.py`：权限检查、用例编排、重算与会签流转、并发控制和审计。
- `src/http_api.py`：入口。JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、建议台和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`，响应附带当前有效建议版本
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/readings`，登记传感峰值、巡检缺陷、车型轴重或桥面天气，登记后自动重算
- `GET /api/items/{id}/readings`
- `POST /api/items/{id}/readings/{rid}/review`，复核数据并重算
- `POST /api/items/{id}/readings/{rid}/close`，关闭巡检缺陷并重算
- `GET /api/items/{id}/recommendations`，建议版本历史（含已失效留档）
- `POST /api/items/{id}/recommendations/{rid}/sign`，工程或路政会签，提交`level`
- `POST /api/items/{id}/recommendations/{rid}/release`，会签一致后放行建议
- `POST /api/items/{id}/notice`，路政登记交通通告
- `POST /api/items/{id}/withdraw-notice`，路政确认撤除通告
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 限行建议台规则

- 传感峰值（sensor_operator）、巡检缺陷（bridge_engineer）、车型轴重（traffic_authority）、桥面天气（sensor_operator）分角色登记，按类别校验结构字段。
- 每次新增或复核数据后重算建议：取四类数据中的最高风险等级，触发项写入建议版本；旧版本置为`superseded`失效留档，版本号递增。
- 建议版本状态：`pending`待会签 → 工程与路政分别会签，意见一致为`effective`，不一致为`disputed`待决；待决或未会签不能直接放行，放行后为`released`。
- 恢复（restored）须满足：未关闭事项与未关闭缺陷均为零，且路政已确认撤除通告。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
