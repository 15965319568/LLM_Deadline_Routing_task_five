# V2 服务画像与线上契约

## 产物接口

`python -m serving_lab build --input DIR --output DIR --as-of INTEGER`；亦提供
`serving_lab.build_profiles(input_dir, output_dir, as_of_us)`，返回下列四个文件的
stem→JSON 对象字典。所有时间单位为微秒，cutoff 边界包含，输出须为合法有限 JSON。
每次 build 自原始输入生成，无需预先构建旧产物。

- sample-ledger.json：`rows:[{source,disposition}]` 按 source 字符串排序；
  `samples` 按 `(endpoint_id,identity)` 排序。每条样本含 identity（三元数组）、
  endpoint_id、purpose、tokens（未缓存）、decodes、service_us、ttft_us、ended_us、
  available_us。source 只在 rows 中出现。所有物理测量记录有且只有一种归属：
  accepted/excluded/duplicate/deferred/conflict。duplicate 可对应不合格的代表。
- profiles.json：`{as_of_us,targets:[...]}`，target 顺序与 deployment 相同。
  每项包含 endpoint_id、fingerprint（六项部署值原样）、status、counts（二维）、
  surface:`{tokens,decodes,service_us}`。axis 来自 profile_policy，行 decode、列 token。
  每格只用 baseline 且工作量恰好等于坐标的合格样本，成本为中位数（偶数取中间两者
  平均）。count 不足 min_samples 的格值为 null。任一格不足则整个 target 状态
  insufficient_data，否则 ready。合格但不在格点的记录仍留在样本账，不强塞最近格。
- drift.json：endpoint ID→`{state,count,ratio,sample_ids}`。仅 ready target，取 recent
  中工作量在完整 axis 覆盖范围内、插值基准成本 >0 的样本，按 `(ended_us,identity)`
  排序取最后 recent_window 条。逐样本 service/插值基准的算术均值是 ratio，输出
  四舍六位小数；sample_ids 为该窗口的执行身份数组。count 不够即 insufficient_data；
  足够后严格高于 drift_upper 为 slow，严格低于 drift_lower 为 fast，否则 normal。
  空窗口 ratio=null。TTFT、流量长度占比变化不直接当作服务漂移。
- audit.json：`{as_of_us,input_records,dispositions,qualified_samples}`。
  dispositions 固定五种归属的整数计数（含零）；总和等于 input_records。
  qualified_samples 等于 samples 长度，多 target 共享条件时可大于 accepted 数。

输入 axis 从零严格递增，至少两点；矩形网格双线性插值。不完整表不能插值，也不
跨范围外推。最近的 summary 或生成文件时间不能覆盖这些训练/覆盖要求。

## 生产接口

`serving_lab.ServingGateway(source_dir, profile_dir, clock=None, transport=None, tokenizers=None)`
加载 deployment.yaml 和生成的 profiles.json，使用 production-stack 的 main_router
与 request_service completions 转发实现。保留 `observe(snapshots)`、`cache(claims,available)`、
`state(endpoint_id,healthy,draining)`、`terminal(request_id,success=False)`、`inspect()`、
`summary(start_us,end_us)` 及 FastAPI `.app`。新增 `await reload(profile_dir)`。
所有 build 产物可离线准备，但 gateway 不能消费 as_of_us > 当前时钟的画像。
各 artifact 的 fingerprint 必须与 deployment 一致；无效 reload 应整体失败且不改变
任何线上 target。加载后改源文件不会自动替换在用画像，必须调用 reload。

请求使用 `/v1/completions`、stream=true、绝对整数 deadline_us 和唯一 X-Request-Id。
本题在途不改变 endpoint 集合或部署 fingerprint。截止时间含边界。使用当前健康、
非 draining、画像 ready、观测可用且工作量在覆盖范围内的 target。相同 tokenizer
可共享一次本地编码；caller prompt_tokens 不权威。

负载 snapshot：endpoint_id、observed_at_us、非负整数 sample_seq、by_owner。
owner 行包含非负有限 backlog_us 和非负整数 decode_count。拒绝非法或未来观测，
只有严格递增序号能替换原记录；age 在 [0,max_age_us] 可用。排除本 owner 后的外部
backlog/decode，再分别加本地 prefill 预留/decode。缓存 API 的 claim namespace 是
endpoint_id、model_revision、tokenizer；仅有效期内且 prefix 匹配的最大 matched_tokens
有效，目录不可用即 miss。缓存 scope 与离线 proof 的身份规则不要混为一谈。

预测绝对首 token 时刻 = 当前时钟 + 外部 backlog + 本地 prefill 预留 + 校准倍率 ×
画像插值成本 + target.transport_us；最终正数按 half-up 取整数。先选 deadline 可行
者，再按 `(预测时刻, endpoint_id)` 取最小。没有可适用依据返回 503；存在依据但
无 deadline 可行者返回 429；均不得向后端派发。接纳检查到预留必须是同一原子过程。
普通 legacy 路由不受新增覆盖限制影响。

每个 endpoint 的 profile epoch 初始为 1。usable 状态、fingerprint 或表/axis 的
语义变化才递增；仅 counts、as_of 或文件路径变化不能清空校准历史。变更清空该端点
在线校准历史、去重集合，倍率恢复 1；未变端点保持不动。reload 与接纳串行化，
一次接纳不能混用两份画像。已接纳请求保留当时 epoch、**未乘倍率的插值基准**、
原始已乘倍率的预留额；更新不能重算旧请求的预留。

只有完整解析到含非空 generated text 的 SSE data 事件才从 prefill 转 decode；
heartbeat、空 text、角色事件及 DONE 不算首 token。UTF-8 和 SSE 可跨 chunk。
首 token 清除此请求的原预留，增加一个 decode；完成、后端异常、取消（首 token
前后）均精确释放，重复终态操作幂等。旧 epoch 请求仍记真实 TTFT/终态，但其反馈
不能校准新 epoch。

当前 epoch 的有效服务反馈来自后端 x-service-sample 和 x-service-us，按
`(endpoint,sample-id)` 去重。baseline 使用接纳时未乘倍率的插值成本，忽略历史
x-baseline-us。baseline 必须 >0，service 非负有限。倍率更新为
clip((1-alpha)*old + alpha*service/baseline,lower,upper)。窗口按有效到达顺序取最后
window_samples 个原始比值，用 calibration 阈值判线上漂移，样本不足为 insufficient_data。
离线 drift 和线上反馈窗口是不同对象，不将近期离线均值直接写入线上倍率。

## 可观测性

`GET /deadline/diagnostics` 与 inspect 返回 `{nodes,decisions}`。nodes 每 target 含
reserved_us、decoding、correction、drift、age_us（不可用 -1）、epoch、profile_status；
浮点容量/倍率输出六位。decisions 为接纳顺序的 `{request_id,arrival_us,status,endpoint_id,
predicted_first_us}`，拒绝的后两项为 null。信息必须来自实际服务状态。

summary 的窗口按 arrival_us 左闭右开：offered 为全部决策，accepted 为接纳数，
rejected 为非 200；completed 为成功结束且有首 token；deadline_missed 为有终态且
无首 token/首 token 超时；goodput_per_s 为成功且首 token 不超时的数量除窗口秒数；
ttft_p95_us 对实际首 token 延迟取 nearest-rank（无首 token 为 null）。后取消仍保留
已发生的 TTFT，但不计 completed/goodput。

`/metrics` 保持原 deadline 指标的秒/微秒单位；TTFT 只计 generated token，容量和
倍率对应实际 nodes，不能创建 request_id/prompt 等无界标签。允许标签仅
endpoint/model/result/state/le。旧 `docs/monitoring-contract.md` 的通用指标定义仍适用。
