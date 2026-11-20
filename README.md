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

## 可复现反例

在正常演示基础上，分别导入 `sensor_late.json`、`gate_orphan.json`、`capacity_overlap.json`。第一项不改变新占用状态；第二项冻结发布；第三项仍只扣一个泊位，但消防许可取得该泊位处置优先级。测试另覆盖失效设备、心跳超时、临占撤销、跨夜计费区间、批次回滚和两账同时不完整。

数据仅为合成数据。初始化为幂等建表，表结构见 `src/parking/store.py`；本版未引入外部迁移工具，新增结构时应提供 SQL 版本迁移而非删除旧账。未来扩展边界见 `docs/initial-requirements.md`。

## 离线停车费裁定

财务裁定与余位发布是两套独立结论：余位发布写 `availability_release`，费用裁定写 `billing_decision`、`bill` 与 `stay_ledger_entry`，互不触发。闸机事件和已出账单均不可改写，新证据只追加版次，后到的校时通过新裁定引用旧裁定、只结算余额差额。

### 校时相关性

校时证据描述的是**一台闸机**在有效时段内共同的时间偏移区间，不是给每条事件各抽一次随机偏移。同一闸机的进、出读数共用同一个偏移变量，因此同闸机车次的真实时长与偏移无关；两台不同闸机的偏移相互独立，按「闸机 × 校时版次」分组后对全部可行偏移组合逐一枚举计算，绝不把两个独立时间区间的端点直接相减。重叠的校时版次取区间交集，交集为空判为矛盾证据并拒绝收费；校时窗口未覆盖读数则拒绝裁定。

### 费率与合同

费率为整数分，含免费宽限秒、跨档起算、开放末档、每日封顶和 UTC 跨零点按天分段。两种事先批准的付款合同：

- `exact` 精确出账：仅当全部可行组合给出同一金额时开正式账单，否则 `pending_review` 且不出任何账单；
- `guaranteed` 保底预收：可先收所有可行组合共同保证的最低额（凭证类型为 `prepayment`，绝不伪装成正式账单），差额待核；共同最低额为 0 时即「待核零收」。

校时收窄后重新裁定：保底合同只补收或退回与当前余额的差额；已出正式账单被可信新证据推翻时，原账单保留，新增 `correction` 账单并通过 `supersedes_bill`/`supersedes_decision` 引用旧版。无出场、孤儿出场、重复进出、缺闸机身份等异常车次直接 `rejected`，永不收费。

### CLI

```powershell
python -m parking import-gate --db data/fee.sqlite --input fixtures/parking/billing_gate.json
python -m parking import-calibration --db data/fee.sqlite --input fixtures/parking/billing_calibration_wide.json
python -m parking import-tariff --db data/fee.sqlite --input fixtures/parking/billing_tariff.json
python -m parking import-contract --db data/fee.sqlite --input fixtures/parking/billing_contracts.json
python -m parking adjudicate --db data/fee.sqlite --stay fee-stay-1 --contract CONTRACT-EXACT
python -m parking adjudicate --db data/fee.sqlite --all
python -m parking import-calibration --db data/fee.sqlite --input fixtures/parking/billing_calibration_narrow.json
python -m parking adjudicate --db data/fee.sqlite --stay fee-stay-2 --contract CONTRACT-GUARANTEED
python -m parking stay-report --db data/fee.sqlite --stay fee-stay-2
python -m parking billing-report --db data/fee.sqlite
```

可运行样例（费率：60 分钟内 200 分，超过 60 到 120 分钟 400 分，每日封顶 500 分）：

- `fee-stay-1` 同一闸机 10:00 进、11:00 出，共同偏移 0–5 分钟：真实时长恒为 60 分钟，两个合同都出 200 分正式账单；逐事件独立扩张会错误得到 55–65 分钟。
- `fee-stay-2` 进、出闸机各自独立偏移 0–5 分钟：真实时长 55–65 分钟，精确合同待核零收、保底合同预收 200 分；迟到校时把进场偏移确认为 0、出场确认为 +5 分钟后，真实 65 分钟应收 400 分，保底合同只再补 200 分。
- `fee-stay-night` 21:00 进、次日 00:20 出：跨零点按天分段，第一天原始档费 800 分被封顶为 500，第二天 200，合计 700 分；简单按时长乘单价只会得到 800 分。
- `fee-stay-open` 与 `fee-stay-orphan` 验证无出场和孤儿出场被拒绝且不产生任何收款。

`stay-report` 给出每笔车次的闸机事件、覆盖读数的校时版次、全部裁定版次链、账单与收款流水；每条裁定记录闸机事件、校时、费率和合同的版本引用及证据指纹，同事件 ID 异正文冲突、重放幂等。
