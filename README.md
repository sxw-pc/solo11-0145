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

## 业务结论

- 设备失准、归属错误或心跳过期的地磁不证明泊位空闲。迟到旧读数不能覆盖新状态。
- 闸机先采录场内车辆基线，再按匿名车次配对。孤立出场不会减少在场车辆；跨夜车次把真实进出区间同时保存在 `vehicle_stay` 和报告；重复回执不重复计数。
- 批准的临占许可才扣可售容量。相交许可按泊位并集只扣一次，优先级最高的许可承担该泊位的现场处置责任；撤销恢复容量但不会清理占用差额。
- 地磁覆盖率按 `total_bays` 对照已登记泊位；未覆盖泊位计入未知。地磁不足而闸机基线及车次账完整时给保守上限；无基线、或孤立出场同时存在时输出 `unknown`，绝不猜测具体数。两账已知但不一致时冻结发布。
- 即使有孤立出场，闸机已确认的在场车辆仍构成下界。若该下界超过临占许可扣除后的可用容量，报告给出 `capacity_breach` 并将超容处置记录保存在 `capacity_incident`；不能把孤立出场虚减成普通未知。

`gate_capacity_breach.json` 与 `capacity_breach.json` 组成独立反例：仅导入前者时，孤立出场与地磁缺口令余位 `unknown`；再导入后者，临占把可用容量降到 1，而闸机已确认至少 2 辆在场，结论变为需运营方调查的 `capacity_breach`，并持久化超容事件。

## 现场交接许可（双合同裁决）

临占订单在同一车位时间窗重叠时，`snapshot` 仍按泊位并集只扣一次公共容量、按 priority 给 `owner_by_bay`；但排序结果不是交接证据。`handover_snapshot` 另对每个（订单，车位）按查询时刻重裁现场入场状态：`admittable`（可入场）、`awaiting_handover`（待交接）、`under_investigation`（地磁待查）、`preempted`（被高优先级压过）、`conflict`（同优先级无交接凭证冲突）。

```powershell
python -m parking import-handover --db data/demo.sqlite --input fixtures/parking/handover_override.json
python -m parking handover --db data/demo.sqlite --as-of 2026-11-20T12:05:00Z
```

- 每份交接文书（`release` 交还、`takeover` 接管、`revocation` 撤销、`emergency_override` 紧急越级）都绑定唯一车位、前后订单 ID、订单来源版本/越级版本、时刻、两方身份与两方签字。车位不符或引用旧版订单的签字在裁决时被拒绝并列入 `rejected_documents`；越级版本重放到别的（车位，消防订单）对在导入时直接拒绝。
- 普通交接合同：前使用方交还 + 后使用方接管 + 车位可信空闲，才签 `admittable`。应急越级合同：高优先级消防订单有效、独立越级文书已签且车位可信空闲时，可不等原使用方签交还即准入，同时把被压订单未完成的交还责任记入 `emergency_obligation` 台账，直至该订单自己补签交还才销账。
- 两合同都不准占用中或证据未知的车位实际入场；未知地磁只给 `under_investigation`，不看闸机总数推断单车位腾空。同一份消防订单加越级文书下，普通合同仍 `awaiting_handover`、应急合同可 `admittable`，差异在每条 claim 的两个合同字段中可见。
- 低优先级订单被压期间为 `preempted`；高优先级订单结束不自动恢复旧方——旧订单仍须在有效窗口内且具备新一轮交还/接管证据。同优先级冲突至少两方且无交接链才成立，绝不按 `order_id` 字典序选胜者；订单撤销、新高优先级订单迟到、文书撤销与重放均按事件时刻从只追加版次重裁，历史签收与裁决版次保留在 `handover_revision`、`handover_decision_revision`。

`handover_orders.json`、`handover_sensor.json`、`handover_override.json`、`handover_return.json`、`handover_discharge.json` 组成可运行反例（A 车位施工单 C 09:00–18:00 priority 1，消防单 F 12:00–14:00 priority 10，同优先级 G 13:00–15:00）：12:05 地磁空闲且有越级文书时普通合同待交接、应急合同可入场；12:06 地磁占用时两合同均不放行；13:05 F/G 同报冲突；F、G 结束后 C 无新文书只能等待，迟到的交还/接管齐备后才恢复；C 的越级交还责任只在其本人补签后销账。

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。
