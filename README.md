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
pytest -q
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

## 业务结论

- 设备失准、归属错误或心跳过期的地磁不证明泊位空闲。迟到旧读数不能覆盖新状态。
- 闸机先采录场内车辆基线，再按匿名车次配对。孤立出场不会减少在场车辆；跨夜车次把真实进出区间同时保存在 `vehicle_stay` 和报告；重复回执不重复计数。
- 批准的临占许可才扣可售容量。相交许可按泊位并集只扣一次，优先级最高的许可承担该泊位的现场处置责任；撤销恢复容量但不会清理占用差额。
- 地磁覆盖率按 `total_bays` 对照已登记泊位；未覆盖泊位计入未知。地磁不足而闸机基线及车次账完整时给保守上限；无基线、或孤立出场同时存在时输出 `unknown`，绝不猜测具体数。两账已知但不一致时冻结发布。
- 即使有孤立出场，闸机已确认的在场车辆仍构成下界。若该下界超过临占许可扣除后的可用容量，报告给出 `capacity_breach` 并将超容处置记录保存在 `capacity_incident`；不能把孤立出场虚减成普通未知。

## 充电可用裁定

`charging` 子系统在普通余位之外独立采证与发布，普通余位含义不变。三条新证据账：充电设备（枪口、可连接车位、支持的充电类型与功率、所在回路及回路额定功率）、独立故障/恢复事件（按发生时间与来源身份留存，晚到的旧事件不覆盖新状态）、充电会话（车位、枪口、类型、实际占用功率、开始结束证据；入库前核对车位、枪口与回路，重复上报幂等，同号异内容整批冲突回滚）。

```powershell
python -m parking import-charging --db data/demo.sqlite --input fixtures/parking/charge_equipment.json
python -m parking charge-report --db data/demo.sqlite --as-of 2026-11-17T10:10:00Z --total-bays 3 --charging-type dc --min-power-kw 7 --contract single
python -m parking charge-report --db data/demo.sqlite --as-of 2026-11-17T10:10:00Z --total-bays 3 --charging-type dc --min-power-kw 7 --contract fleet
```

调用方必须显式选择合同，两者发布字段与证据分开保存（`charging_release` 记录版次）：

- `single`（单车寻找）：回答哪些车位各自可用，发布 `candidates` 与 `candidate_count`。共享回路不阻止多个车位分别成为候选，但候选数绝不是同时可接待车辆数。
- `fleet`（车队分流）：回答现在最多同时接待几辆，发布 `max_vehicles` 与对应 `assignment`（车位与枪口）。最大组合按车位、枪口、回路容量以求解器（最大流）求出，不逐车位计数相加，也不按第一条枪口记录贪心。

裁定规则：只用可信地磁判空、未被有效临占扣除、枪口健康且空闲的组合；运行中的会话先扣其回路功率；一枪不接两车、一车位不占两枪、回路合计不超额。普通余位因孤儿出场、重复凭证、容量冲突或地磁闸机不一致而冻结（含 `capacity_breach`）时，充电数字一并 `withheld`；相关车位地磁未知时输出 `unknown` 并给理由，绝不静默当作零；只有闸机总数而不知道具体空车位时同样不猜测。每次裁定把引用的地磁、闸机、临占、枪口、会话证据存入 `charging_release.evidence`，迟到故障或会话可对同一时刻重算并留下前后发布记录。

可运行反例（fixture 前缀 `charge_`）：A、B 可信空闲各有 7 千瓦枪口、共享额定 7 千瓦回路 L1，普通余位为 2；`single` 候选 A、B 两处，`fleet` 同时只能接待 1 辆。C 车位以 7 千瓦在 L1 充电时车队余量降为 0，会话结束后恢复 1。再加只能接 A、独立 7 千瓦回路 L2 的枪口 gD 后，最大组合为 A←gD、B←gB 共 2 辆（贪心先占 gA 只得 1 辆）。对 gA 报故障、对 B 下临占、补报更早的恢复、重放同一事件分别由 `tests/test_charging.py` 验证。

`gate_capacity_breach.json` 与 `capacity_breach.json` 组成独立反例：仅导入前者时，孤立出场与地磁缺口令余位 `unknown`；再导入后者，临占把可用容量降到 1，而闸机已确认至少 2 辆在场，结论变为需运营方调查的 `capacity_breach`，并持久化超容事件。

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。
