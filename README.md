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

## 现场核证与设备绑定修订

设备换位安装而登记未改时，占用总数仍可能与闸机一致，逐车位状态却正好写反。`verify` 子系统把设备与物理车位看成一对一关系，收录两类现场证据：受控占空试验（时间窗、现场确认人、独立见证人、该窗内设备原始读数与序号）和安装交接单（签发方、设备序号到物理车位的绑定）。读数超出试验时间窗、设备故障或超过校准有效期的，不能成为绑定证据；执行者与见证人同人的试验无效。

```powershell
python -m parking import-verify --db data/demo.sqlite --input fixtures/parking/verify_trials_two_crews.json
python -m parking import-verify --db data/demo.sqlite --input fixtures/parking/handover_correct.json
python -m parking verify-status --db data/demo.sqlite --as-of 2026-11-17T12:30:00Z
python -m parking verify-issue --db data/demo.sqlite --contract dual_trial --bays A,B --effective-at 2026-11-17T12:00:00Z
python -m parking verify-issue --db data/demo.sqlite --contract inspection_cosign --bays A,B --effective-at 2026-11-17T12:00:00Z
```

核证不逐条读数配对，而是枚举所有同时满足全部有效试验的一对一绑定：缺少试验、试验互相矛盾、多设备争同一车位或一设备可对应多车位时都拒绝签发，绝不随意挑一套映射。两份事先批准的合同：

- `dual_trial`（独立双试）：待换位每个车位至少两次由不同执行者和不同见证人完成的有效试验，且所有可行绑定完全相同。
- `inspection_cosign`（巡检联签）：一次受控试验得出唯一映射，且一份在试验时间窗之前独立签发的安装交接单给出同一映射；交接单与试验相反时拒绝，不按多数票强行改绑。

签发只为证据唯一的那组车位追加新的 `sensor_revision`（生效时刻为 `--effective-at`），旧版次全部保留；`verify-status` 与 `report` 的 `binding` 段给出每个车位是否已证实、候选映射、原因和绑定版次。逐位查询按设备身份和查询时刻有效的绑定重投影原始读数，因此更早 `as_of` 的报告仍按旧版，修订后的报告据新绑定纠正逐车位状态。晚到的冲突试验、设备失效或同 ID 异正文都会保留证据并令重裁拒绝，已签发车位转为 `contested`。

## 业务结论

- 设备失准、归属错误或心跳过期的地磁不证明泊位空闲。迟到旧读数不能覆盖新状态。
- 闸机先采录场内车辆基线，再按匿名车次配对。孤立出场不会减少在场车辆；跨夜车次把真实进出区间同时保存在 `vehicle_stay` 和报告；重复回执不重复计数。
- 批准的临占许可才扣可售容量。相交许可按泊位并集只扣一次，优先级最高的许可承担该泊位的现场处置责任；撤销恢复容量但不会清理占用差额。
- 地磁覆盖率按 `total_bays` 对照已登记泊位；未覆盖泊位计入未知。地磁不足而闸机基线及车次账完整时给保守上限；无基线、或孤立出场同时存在时输出 `unknown`，绝不猜测具体数。两账已知但不一致时冻结发布。
- 即使有孤立出场，闸机已确认的在场车辆仍构成下界。若该下界超过临占许可扣除后的可用容量，报告给出 `capacity_breach` 并将超容处置记录保存在 `capacity_incident`；不能把孤立出场虚减成普通未知。

`gate_capacity_breach.json` 与 `capacity_breach.json` 组成独立反例：仅导入前者时，孤立出场与地磁缺口令余位 `unknown`；再导入后者，临占把可用容量降到 1，而闸机已确认至少 2 辆在场，结论变为需运营方调查的 `capacity_breach`，并持久化超容事件。

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

`sensor_swapped.json` 是绑定反例：d1 登记在 A、d2 登记在 B，而现场确认 10:00 只有 A 被占、11:00 只有 B 被占，读数始终与登记相反。闸机在场数两次都为 1，总量核对均通过并 `published`，逐车位状态却写反。导入 `verify_trials_same_executor.json` 后独立双试合同因执行者相同而待补证；再有 `handover_correct.json` 时巡检联签合同可签发 d1→B、d2→A 并纠正后续逐位状态；补足 `verify_trials_two_crews.json` 的不同人员第二组试验后，独立双试合同同样可签发。`verify_trial_conflicting.json` 与 `handover_wrong.json` 分别演示晚到冲突证据令签发重裁拒绝、交接单与试验相反时不得按多数票改绑。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。
