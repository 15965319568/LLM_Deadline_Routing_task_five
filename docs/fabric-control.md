# 控制策略、发布单元与租户信用

本契约与另外三个 fabric 契约同时生效。fabric.json/tenant_limits 是 deployment
revision 0 基线；控制导出是原始证据。每次 build 必须从该基线重算，迟到冲突
可以撤销此前生效的 revision，并使其后继变成 orphan。

## 控制证据

control/manifest.json 的 schema 为 tenant-control/v1；sources 是相对输入根目录
的文件名到文件字节 SHA-256 的映射。只读取清单文件，禁止路径逃逸。校验失败
拒绝整次 build。支持 UTF-8 BOM CSV、JSONL、gzip。source 身份为
`相对文件名#从1开始的数据行号`，CSV 表头不计，JSONL 空白行不计，损坏行仍计。

每行含 schema/tenant/revision/parent/effective_us/ingested_us/authority/state/patch/
signature。patch 是 JSON 对象编码成的字符串。signature 的算法：删除 signature
自身，其余字段逐值 str(value)，再 json.dumps(ensure_ascii=False,sort_keys=True,
separators=(',',':'))，取 UTF-8 SHA-256 十六进制。先验签，再归一化 patch。

revision/parent/effective_us/ingested_us 是非负整数，revision>parent。拒绝 bool、
NaN、无穷；等价数字字符串可解析。tenant 必须存在于部署。patch 非空且只能含：
整数项 max_slots/max_pages/max_priority/max_starts/start_window_us；非负有限数值项
max_work_us/burst_credit_us/burst_refill_us/failure_penalty_us。

逐行 disposition 按优先级裁决：

1. 字段、类型、签名或 patch 非法为 invalid，即使它来自未来。
2. ingested_us 大于 cutoff 为 deferred。
3. authority 不是 controller 或 state 不是 enacted 为 excluded。
4. effective_us 大于 cutoff 为 pending。
5. 剩余按 (tenant,revision) 分组，归一化 parent/effective_us/patch 不一致则整组
   conflict；导出时间差异不算冲突。不能多数票或最后到达胜出。
6. 同义组按 source 字典序选一行，其余 duplicate。
7. 每 tenant 从 revision 0 按 revision 升序重放；获选行 parent 不等于当前
   applied revision 则 orphan，否则 applied，合并 patch 并推进 revision。
   允许跳号；conflict 不推进链。

build 生成 tenant-policies.json：`{as_of_us,tenants:{tenant:{revision,limits}}}`，
包含全部部署租户；tenant-policy-ledger.json：`{rows:[{source,disposition}]}`，
覆盖全部原始记录并按 source 排序。被修改的数值项用 JSON 浮点数，整数项用整数；
未修改的基线值保持 fabric.json 表示。这是 bundle 摘要的数值表示契约。

## 原子发布

bundle-manifest.json 为 `{schema:"profile-bundle/v1",as_of_us,artifact_order,artifacts:{名称:摘要},bundle_id}`。
artifacts 恰好包含 fabric-profiles/phase-ledger/phase-audit/phase-drift/tenant-policies/
tenant-policy-ledger 六项，无扩展名。摘要是产物解析成 JSON 后使用
json.dumps(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
得到的 UTF-8 SHA-256。`artifact_order` 固定为上述六项，`bundle_id` 是 canonical
`{schema,as_of_us,artifacts}` 的 SHA-256。phase service_us/event_us、画像非空成本、漂移 factor 用浮点；
support、coordinate、attempt、as_of_us 用整数，空值用 null。
manifest、fabric-profiles、tenant-policies 的 cutoff 必须一致。

Gateway 初始化和 bundle prepare 验证摘要、完整集合、cutoff 和所有画像/策略 shape。
在线发布的 prepare/commit/abort 栅栏见 `docs/fabric-deployment.md`。
非法发布全量拒绝，epoch、反馈、策略、余额、在途请求均不改变。合法发布与
admission 同锁提交。策略从 deployment 基线重算，不能用已经变更的 live limits
作为基线；租户 revision 回退不自动重置未变资源的 feedback epoch。

## 信用账本

L 为当前 max_work_us，W 为全部未终态请求 immutable charged_work_us，C 为新请求
三个 reserved_us 之和乘 priority_factor。新信用预留为
`max(0,W+C-L)-max(0,W-L)`，不是总 overage 或阶段实际占用。
先补齐信用再比较 balance；不足则拒绝，负债时即使增量为零也拒绝。slot/page/
start、资源容量、deadline 仍必须满足。拒绝不消耗 start 或信用。

cap/refill/penalty 优先使用当前 tenant policy，缺字段回退部署全局同名字段，再
缺省为零。首次建账 balance=cap、initial_us=cap、updated_us=当前时间。
每次更新增加 `elapsed/burst_refill_window_us*burst_refill_us`，全记 refill_us；
余额超过 cap 的部分全记 clipped_us，余额保留至 cap；负余额不能抹零。
reload 先按旧 policy 补齐至当前时间，再切新 policy 并按新 cap 裁剪。
检查、接纳、结算、diagnostics 共用更新逻辑，同刻不能重复补充。

请求固定接纳时 policy_revision/failure_penalty_us/burst_reserved_us/charged_work_us，
reload 不重定价。在接纳时扣 balance 并增加 reserved_us/active_reserved_us；成功
退固定预留并增加 refunds_us，超 cap 记 clipped_us；失败/取消不退款，另扣固定旧
罚金并增加 penalties_us。每种终态都减少 active_reserved_us，重复终态幂等。
阶段释放不归还租户 immutable work。

diagnostics 额外返回 tenant_policies 与 tenant_ledgers。ledger 含 initial_us/balance/
updated_us/reserved_us/active_reserved_us/penalties_us/refunds_us/refill_us/clipped_us。
金额对外舍入 6 位，内部不要求舍入。每个检查点应满足：
`balance = initial_us + refill_us - reserved_us + refunds_us - penalties_us - clipped_us`。
requests 另含 tenant/policy_revision/failure_penalty_us/charged_work_us/burst_reserved_us/
settlement_us。未终态 settlement_us=null，成功为固定预留，失败/取消为固定预留加
旧罚金。tenant 不进入 Prometheus 标签或伪造 resource。
