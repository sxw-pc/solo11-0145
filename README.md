# 公共停车余位采证台

本地采录合成停车场地磁读数、闸机进出与临时占位许可，给出可追溯的余位发布结论。它面向停车信息发布和运营稽核，不连接真实车场、车辆身份、支付或外网。

## 运行

需要 Python 3.11+。在仓库根目录执行：

```powershell
python -m pip install . --no-build-isolation --no-deps
$env:PYTHONPATH=(Resolve-Path -LiteralPath src).Path
python -m parking init --db data/demo.sqlite
python -m parking demo --db data/demo.sqlite
python -m unittest discover -s tests -v
```

`demo` 导入三个正常 fixture，在 `2026-11-17T10:10:00Z`、总泊位 3 的场景下应报告 `published`、余位 1。重复执行同一事件不会重复计数；不同内容复用事件 ID 会拒绝整个批次。

## 三个独立入口

```powershell
python -m parking import-sensor --db data/demo.sqlite --input fixtures/parking/sensor_good.json
python -m parking import-gate --db data/demo.sqlite --input fixtures/parking/gate_good.json
python -m parking import-reserve --db data/demo.sqlite --input fixtures/parking/capacity_good.json
python -m parking report --db data/demo.sqlite --as-of 2026-11-17T10:10:00Z --total-bays 3
```

地磁子系统保存设备校准、原始读数和泊位投影；闸机子系统保存回执、匿名车次与在场净数；临占子系统保存许可、泊位集合和生效窗口。各自可先导入和查询，`report` 再从原始证据按查询时间重算，不把缓存投影当唯一真相。

## 无障碍占用核查（独立入口）

```powershell
python -m parking import-accessible --db data/demo.sqlite --input fixtures/parking/accessible_late_scan.json
python -m parking accessible-audit --db data/demo.sqlite --as-of 2026-11-20T11:05:00Z --save
python -m parking accessible-history --db data/demo.sqlite
```

`accessible-audit` 只在能证明无障碍车位占用无授权时才生成处置，不改变普通余位或停车费。它接收四类带来源的证据：

- 无障碍许可版次（`permit`）：泊位集合、有效窗口、批准/撤销、受信来源；
- 凭证与匿名 stay 的核验记录（`verification`）；
- 车位现场刷证（`bay_scan`）与人工占位观察（`manual_observation`），可带 `stay_id`。

归属规则是严格分开的：**只有**受信地磁读数构造占位时段；闸机进出时段、刷证和观察只用来建立“匿名 stay 到占位时段”的可能关系。一个 stay 不能同时占两个车位，一个占位时段不能归两个 stay；闸机只有总数基线时不编造 stay 身份。每个与证据相容的完整归属都单独判断授权，只有**每种**归属都证明该车位无有效许可，才判 `unauthorized`。许可撤销、许可版次、刷证证据或 `capacity.py` 有效限制变化都会重算并新增一个 `audit_version`；迟到的刷证或人工核验不会改写旧版本，只生成新裁定。普通车位的无关占用不会进入无障碍模型。

两份事先批准的处置合同从核查结果派生：

- **现场巡查合同**：车位级无授权占用一经证明即 `dispatch`，即使具体 stay 仍有两种归属；
- **定向通知合同**：只有在**每个**可行归属中同一个 stay 都占该无障碍车位且无许可时才 `notify`，否则 `pending_identity` / `pending_verification`。巡查通知不会伪装成定向通知。

原因码区分不同证据状态，例如 `permit_expired`、`permit_revoked`、`permit_untrusted_source`、`credential_use_outside_occupancy`、`credential_shared_by_stays`、`valid_permit_for_bay_unbound`、`no_stay_identity_for_occupied_bay`、`duplicate_source_ignored`、`untrusted_device`、`accessible_order_revoked`。`no_stay_identity_for_occupied_bay` 表示车位级许可账已证明无有效许可，因此巡查合同仍派单，但定向合同因车主不明保持 `pending_identity`。可运行样例见 `fixtures/parking/accessible_*.json` 与 `tests/test_accessible.py`。

`docs/initial-requirements.md` 列出的未来能力仍未实现，本版结论不得被当成那些能力。

## 业务结论

- 设备失准、归属错误或心跳过期的地磁不证明泊位空闲。迟到旧读数不能覆盖新状态。
- 闸机先采录场内车辆基线，再按匿名车次配对。孤立出场不会减少在场车辆；跨夜车次把真实进出区间同时保存在 `vehicle_stay` 和报告；重复回执不重复计数。
- 批准的临占许可才扣可售容量。相交许可按泊位并集只扣一次，优先级最高的许可承担该泊位的现场处置责任；撤销恢复容量但不会清理占用差额。
- 地磁覆盖率按 `total_bays` 对照已登记泊位；未覆盖泊位计入未知。地磁不足而闸机基线及车次账完整时给保守上限；无基线、或孤立出场同时存在时输出 `unknown`，绝不猜测具体数。两账已知但不一致时冻结发布。无障碍车位是否被无授权占用**不**参与这条余位结论。
- 即使有孤立出场，闸机已确认的在场车辆仍构成下界。若该下界超过临占许可扣除后的可用容量，报告给出 `capacity_breach` 并将超容处置记录保存在 `capacity_incident`；不能把孤立出场虚减成普通未知。

`gate_capacity_breach.json` 与 `capacity_breach.json` 组成独立反例：仅导入前者时，孤立出场与地磁缺口令余位 `unknown`；再导入后者，临占把可用容量降到 1，而闸机已确认至少 2 辆在场，结论变为需运营方调查的 `capacity_breach`，并持久化超容事件。

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。
