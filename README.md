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

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。
