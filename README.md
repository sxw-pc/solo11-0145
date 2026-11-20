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

## 离线维修诊断与两种出车合同

同一网关下多只地磁同时失联，不等于同时坏了几只探头。`maintenance` 依据设备拓扑把失联症状归因到上游节点，并给出两条互相独立的出车结论；它只解释原因、决定带什么备件，**从不把 unknown 车位改成空闲，也不改动地磁原始读数和既有泊位绑定**。

```powershell
python -m parking import-topology  --db data/demo.sqlite --input fixtures/parking/topology_abc.json
python -m parking import-frequency --db data/demo.sqlite --input fixtures/parking/frequencies_gk.json
python -m parking import-signal    --db data/demo.sqlite --input fixtures/parking/signal_gateway_ok.json
python -m parking diagnose --db data/demo.sqlite --as-of 2026-11-17T10:10:00Z --timeout-minutes 10 --vehicle-capacity 1
python -m parking report   --db data/demo.sqlite --as-of 2026-11-17T10:10:00Z --total-bays 3 --timeout-minutes 10 --diagnose --vehicle-capacity 1
python -m parking history  --db data/demo.sqlite
```

**输入。** 拓扑给出每只地磁到控制板、网关、电源的依赖边（`child_id` 依赖 `parent_id`），必须无环，每个节点带备件编号 `spare_sku`。自检/电压证据带设备、事件时刻、采集时刻、来源摘要和可靠性级别（`high|medium|low`）；电压只证明被测设备本身，自检或在线心跳才证明其上游正在转发。故障频率是单独保存的情景先验，带来源与事件时刻。

**解释。** 每个失联车位先由 `sensor.bay_state` 判为 `unknown`（心跳超时或设备不可信）。一个故障解释是一个非空节点集合：它能覆盖每只失联地磁（集合成员即该设备，或位于其上游祖先链），且不与任何“仍在工作”的独立证据矛盾（在线心跳、通过的自检）。最小性按集合包含判定，在预登记最大故障数内**穷举全部**最小解释，不因行序或零件数把某一解释当成唯一真相。上游单点故障自然能覆盖多只下游失联；单只地磁故障无法覆盖其兄弟。即使某解释覆盖全部症状，只要被独立证据否定，也会连同否定它的证据 ID 记入 `eliminated`——例如另一控制板下的地磁仍在线时，整网关故障被排除，而不是按失联个数投票。

**两种出车合同**（同一批失联数据、同一车载容量，可给出不同业务决定）：

- **保证修复**：携带的备件多重集必须覆盖**每一个**仍可行的最小解释（每个 SKU 取各解释所需数量的最大值）。若超出车载容量，明确报 `cannot_guarantee_one_trip`，并列出所需备件。
- **风险预算**：用独立保存的故障频率作为情景先验，在容量内选覆盖概率达到登记门槛的组合，公开未被修复的情景及其概率。门槛是最低覆盖概率，不是“剩余风险上限”。

频率只是先验，**从不**构成现场故障已证实；`confirmed` 只由针对该节点的第一手失败证据决定。携带某备件也不会把当前地磁状态改为健康——两个合同都不改变任何 `bay_state`。

**迟到证据与版次。** 诊断的身份是其完整证据状态而非查询时间：同一 `as_of` 下新到自检会生成新 `revision` 并保留旧版次及其解释，旧概率不会被复用（剩余情景重新归一化）。同事件 ID 异正文被拒绝整批导入，但冲突正文仍存入 `evidence_conflict` 供审计。SQLite 持久化拓扑、最小解释、失联原因与两次出车决定。

**可复现反例。** A、B、C 经控制板 K 连到网关 G，09:55 均有读数、之后无心跳，10:10 时以 10 分钟心跳时限诊断：最小解释只有 `G` 与 `K`，单只探头故障无法解释三只同时失联。频率 G=0.8、K=0.2，车载 1 件，门槛 0.75：保证修复报“无法保证一趟”，风险预算可先带 `G`，并标明 `K` 的 0.2 剩余风险；三个 `bay_state` 仍是 `unknown`。随后导入 `signal_gateway_ok.json`（G 仍能转发另一控制板数据、K 无输出）后，`G` 解释被排除，两合同改带 `K`，且不复用原概率。`sensor_online_d.json` + `topology_with_d.json` 另构造 D 经 K2 接入同一 G 且在线的情形，验证整 G 故障被排除而非按失联数投票。

## 业务结论

- 设备失准、归属错误或心跳过期的地磁不证明泊位空闲。迟到旧读数不能覆盖新状态。
- 闸机先采录场内车辆基线，再按匿名车次配对。孤立出场不会减少在场车辆；跨夜车次把真实进出区间同时保存在 `vehicle_stay` 和报告；重复回执不重复计数。
- 批准的临占许可才扣可售容量。相交许可按泊位并集只扣一次，优先级最高的许可承担该泊位的现场处置责任；撤销恢复容量但不会清理占用差额。
- 地磁覆盖率按 `total_bays` 对照已登记泊位；未覆盖泊位计入未知。地磁不足而闸机基线及车次账完整时给保守上限；无基线、或孤立出场同时存在时输出 `unknown`，绝不猜测具体数。两账已知但不一致时冻结发布。
- 即使有孤立出场，闸机已确认的在场车辆仍构成下界。若该下界超过临占许可扣除后的可用容量，报告给出 `capacity_breach` 并将超容处置记录保存在 `capacity_incident`；不能把孤立出场虚减成普通未知。

`gate_capacity_breach.json` 与 `capacity_breach.json` 组成独立反例：仅导入前者时，孤立出场与地磁缺口令余位 `unknown`；再导入后者，临占把可用容量降到 1，而闸机已确认至少 2 辆在场，结论变为需运营方调查的 `capacity_breach`，并持久化超容事件。

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。

## 运行测试

```powershell
python -m pytest -q
```

`tests/test_parking.py` 覆盖三账与余位结论；`tests/test_maintenance.py` 覆盖维修拓扑、最小解释穷举、两种出车合同、迟到自检改版、事件冲突留存，以及“在线下游设备排除整网关故障”的反例。
