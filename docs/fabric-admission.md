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

部署可以声明 `default_tenant` 和 `tenant_limits`。请求带有字符串 `tenant` 时按该租户计数；缺省时使用
`default_tenant`，未知租户是 400。每个未结束请求同时占用一个租户 slot 和完整 decode 页数，接纳时就计入
`max_slots`/`max_pages`。租户配额和 GPU/link 容量都是必要条件，任一耗尽返回 429；缓存命中只改变
prefill 坐标和阶段成本，不能减少租户页预留。租户字段不出现在 diagnostics 的低基数节点标签中。

paths 是部署候选三元组。角色须依次为 prefill/link/decode；三者 layout 一致；
link source/target 与两端一致。每个组合分别验证缓存、查三个阶段的成本。
不把独立最优 prefill 和 decode 拼接，也不能默认 link 双向等价。
候选资源的 slots、decode pages 都必须能容纳完整预留。decode 页数是
ceil((prompt_tokens+max_tokens)/page_tokens)，不是未缓存 tokens。

部署可以声明 `route_quarantine_us`。prefill、transfer 或 decode 的后端故障会把
已经选定的完整三元组加入 `[now, now+route_quarantine_us)` 的熔断窗口；取消不触发
熔断，窗口过期后才可重新尝试。熔断是路径级状态，不能只替换失败的一个端点，且
session 亲和仍然优先：被固定路径熔断时返回 429，不得迁移到别的 decode。诊断中的
`quarantined_paths` 以 `p/link/d` 键记录绝对到期时间。

外部 snapshots 字段 resource/owner/boot/seq/event_us/ingested_us/slots/pages/work_us。
boot 是非负整数 generation，不是测量的 boot 字符串。每 resource/owner 按
最大 `(boot,seq)` 保留，旧序号不覆盖新序号；未来 event/ingested 不接收；
本 gateway owner 忽略。相同 generation/seq 不同 event、slots、pages 或 work
使该 owner 快照冲突失效，直到更高序号恢复。相同副本幂等。
接纳时仅汇总年龄在 `[0,snapshot_ttl_us]` 且无冲突的快照，加上本地实际所有权。
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
同时返回 `quarantined_paths`；它是路径到绝对到期微秒的映射，已过期路径不出现。
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

部署身份、兼容选择和滚动换代同时遵守 `fabric-deployment.md`。JSON 对象允许附加审计字段，数值比较容差为 1e-5；要求排序的数组仍按契约排序。

新 speculative 解码同时遵守 fabric-speculation.md；其生成 token、窗口页与终态规则适用于 decode_mode="speculative"，普通 SSE 保持本文件语义。
