# 组合接纳、资源所有权与反馈

公开入口 `serving_lab.fabric.FabricGateway(source_dir,profile_dir,clock=None,transport=None)`。
app 使用 production-stack 的 `/v1/completions`；`reload(profile_dir)` 是 async，
`cache(leases)` 原子替换缓存目录，`observe(rows)` 接收外部负载快照。
可注入具有 now_us() 的时钟以及 aiohttp 风格 request 异步上下文 transport。
正常服务用 aiohttp，CPU 验证使用确定性 transport。离线 CLI 为 build/replay。

请求要求 X-Request-Id、model、字符串 prompt、正整数 max_tokens、绝对微秒
deadline_us 和 stream=true；仅支持当前模型。非法请求 400，已作接纳决定的
ID 再次出现 409。第一笔决定保留，不能改变或释放原请求。无受支持的兼容
组合 503，有受支持组合但容量/时限不满足 429。不做隐式重试或偷偷换布局。

部署可以声明 `default_tenant`、`tenant_limits` 和 `session_affinity_ttl_us`。请求带有字符串 `tenant` 时按该租户计数；缺省时使用
`default_tenant`，未知租户是 400。`priority` 缺省为 0，必须是非负整数且不超过该租户的 `max_priority`；非法优先级是 400。
每个未结束请求同时占用一个租户 slot、完整 decode 页数和三阶段 immutable work 预算，接纳时就计入
`max_slots`/`max_pages`/`max_work_us`。阶段完成只释放资源账本，不释放租户 work 预算；终态才归还。
租户配额和 GPU/link 容量都是必要条件，任一耗尽返回 429；缓存命中只改变 prefill 坐标和阶段成本，不能减少租户页或 work 预留。
租户字段不出现在 diagnostics 的低基数节点标签中。

请求可以带字符串 `session_id`（最多 64 个字符）。同一 session 在亲和 TTL 内固定使用上一次接纳的完整三元组；
固定路径不可用时返回 429，不得偷偷迁移到另一个 decode。TTL 从接纳时刻计算，session 终止后仍保留到期时间。
带 session 的缓存页必须来自相同 session；无 session 的请求只使用没有 session 命名空间的页。

paths 是部署候选三元组。角色须依次为 prefill/link/decode；三者 layout 一致；
link source/target 与两端一致。每个组合分别验证缓存、查三个阶段的成本。
不把独立最优 prefill 和 decode 拼接，也不能默认 link 双向等价。
候选资源的 slots、decode pages 都必须能容纳完整预留。decode 页数是
ceil((prompt_tokens+max_tokens)/page_tokens)，不是未缓存 tokens。

外部 snapshots 字段 resource/owner/boot/seq/event_us/ingested_us/slots/pages/work_us，可选的 `healthy`、`draining`、`capacity_epoch`。
boot 是非负整数 generation，不是测量的 boot 字符串。每 resource/owner 按
最大 `(boot,seq)` 保留，旧序号不覆盖新序号；未来 event/ingested 不接收；
本 gateway owner 忽略。相同 generation/seq 不同 event、slots、pages 或 work
使该 owner 快照冲突失效，直到更高序号恢复。相同副本幂等。
接纳时仅汇总年龄在 `[0,snapshot_ttl_us]` 且无冲突的快照，加上本地实际所有权。`healthy=false` 或 `draining=true` 的新鲜快照
使该资源暂时不能成为新请求的路径；它仍计入容量，已经接纳的请求不受影响。状态字段也参与同序号冲突裁决，
过期状态自动失效。
不能按请求 ID 做指标标签，也不能把自己的快照与本地预留重复相加。

每阶段原始 baseline 从当前画像获取，reserved=baseline*当前该资源 feedback
factor。候选的预计 TTFT 是三个资源既有 work 与本请求三个 reserved 之和，
再加 safety_us；允许 deadline 等号。优先预计时间最小，平局按三元组 ID
字典序。检查和三个资源的预留应在同一临界区完成，包括缓存读取。

接纳立即为三个资源各预留一 slot、各自 immutable reserved work；decode
另预留完整页数。prefill 响应且 acknowledgement 有效后，释放 prefill slot/work；
transfer acknowledgement 有效后释放 link slot/work；decode 的首个非空生成
SSE text 到达后释放 decode 的首 token work，但保留 decode slot/pages。
任何阶段失败/取消或正常 decode 终态释放全部尚持有的资源，且幂等。
注释、空文本、HTTP headers、DONE 都不是首 token；支持 UTF-8/SSE 分片。
持有尚未开始的下游资源也是本地占用；不能到 transfer/decode 才补预留。

画像 reload 先校验全量 shape、部署角色/layout/axis、非负有限成本、support
与 null 的对应关系以及 as_of_us<=当前时间，非法 reload 全量拒绝且不改状态。
有效 reload 与接纳共用临界区。按资源的 role/layout/axis/service_us 是否变化
推进 epoch，并仅重置改变资源的 feedback window/factor/去重集合。
support 计数变化、文件换序、as_of 变化不构成语义 epoch；相同资源保持校准。
在途请求的 baseline、reserved、epochs、pages 均保持接纳时值。

阶段反馈来自有效 acknowledgement 的 headers；decode 只在成功终态且已见
首 token 时采集。x-service-sample 为非空身份，x-service-us 为实际阶段 service。
分母是该请求接纳时捕获的原始 baseline，不能用 reserved、客户端 TTFT 或
x-baseline-us。仅该资源 epoch 仍相等时接收，每 resource/sample 幂等。
baseline>0 方可校准；最近 feedback_window 个 ratio 的 median 截到 [1,4]。
三阶段分别校准，不能统一乘一个模型级因子。发生 reload 时仍可接收未改变
资源的旧请求反馈，但改变资源的旧反馈不得污染新 epoch。

`/fabric/diagnostics` 返回 `{nodes,decisions,requests,ttft_count,ttft_sum_us,outcomes}`。
nodes 每资源含 slots/pages/work_us/epoch/factor；work_us/factor 四舍五入 6 位。
decisions 每首次 ID 含 status，接纳时另含 path/cached_tokens/predicted_us。
requests 仅接纳请求，含 stage/outcome/held（ID 排序）/pages/baseline_us/
reserved_us/epochs/first_us；baseline_us/reserved_us 对外四舍五入 6 位，内部不要求舍入。
stage 为 prefill/transfer/decode/terminal。
outcome 为 success/error/cancelled 或 null。未到首 token 为 null。
实际终态而非接纳成功累计 outcomes；TTFT 为真实首 text 时刻减到达时刻。
字段用于外部对账，允许任意内部结构。

`/metrics` 以微秒值输出 fabric_work_us，其他数值输出 fabric_slots/pages/factor/epoch，
只带 resource 标签；fabric_ttft_seconds_count/sum 为全局首 token 样本；
fabric_terminal_total 只带 outcome 标签。所有未使用资源也应暴露零占用。

V6 增加滚动 start 配额：`tenant_limits` 可以声明 `max_starts` 与
`start_window_us`。它按接纳时刻记账，取消、前置阶段失败和 decode 断流仍消耗
一次 start；400 校验失败和容量拒绝不消耗。窗口是半开区间
`[now-start_window_us, now)`，过期记录才移除。`max_work_us` 的账单是三个
reserved 之和乘 `priority_factors[priority]`，资源 deadline 预测仍使用未加权的
reserved。相同 boot 下 capacity_epoch 下降的迟到快照必须忽略。

流成功还需要完整的 `data: [DONE]` 事件。只有非空 text 没有 DONE 的断流属于
error；heartbeat、注释、空 choices 和 DONE 都不算首 token。SSE 事件可能跨任意
字节边界，终止判定不能依赖单次 read。

V7 的 `route_specs` 为每个三元组分配稳定 `route_id`、alias 和 generation。
`topology` 事件按单调 topology_epoch 更新 enabled/draining；同一时刻的更新、
reload、cache replacement 和 arrival 仍按 workload 顺序串行裁决。draining route
不能被新请求选中，但已持有该 route 的 reservation 不能因为拓扑变更而迁移。
选择结果必须记录 route_id 和 route_generation，不能只记录资源三元组。

V7 的 reservation 具有 transaction_id、reservation_generation 和每阶段 phase
token。prefill、transfer、decode 的 ack 必须回显对应 token、transaction、phase、
route generation 和布局；错 ack 只能终止当前事务，不能释放别的 request 的资源。
租户除了 slot/page/work 外还可有 burst credit 与 failure penalty，credit 仅用于
守恒账本，不得写入低基数 metrics；失败、取消和重复终态必须幂等结算。
