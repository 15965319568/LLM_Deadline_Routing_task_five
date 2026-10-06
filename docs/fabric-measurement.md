# 阶段测量与证据契约

`fabric.json` 是部署事实，`phase-exports.json` 是原始输入目录。所有资源
以 resource ID 区分；相同模型不能合并不同角色、KV layout 或物理资源。
axis 的单位：prefill 为未缓存 Unicode codepoint 数，link 为完整 prompt KV
字节数，decode 为完整 prompt codepoint 数。每 token 的字节数和 page_tokens
来自部署。decode 的 max_tokens 仅影响预留页数。一个中文字符是一个 token。

measurement 支持 `phase/1`（duration 毫秒）和 `phase/2`（duration 微秒）。
CSV、UTF-8 BOM、JSONL 与 gzip 是同等输入。空行忽略，坏 JSON/非对象占据其
实际行号并报告 invalid。来源坐标为相对文件路径加 `#数据行号`，CSV 不含表头。
普通数字的字符串与 JSON 数字可等价；布尔、负数、非有限值和非整数计数字段非法。
同 identity 的拼写差异除数字转字符串外不做猜测修正。

先按 ingested_us 裁出当前可见记录，再处理 measurement identity
`(resource,sample_id,attempt)`。除 ingested_us 外各字段一致的重复副本只取
字典序最小的来源；其他副本为 duplicate。可见副本有冲突时全部 conflict。
真实 attempt 不合并。不可见副本为 deferred，不能提前制造冲突。
必要 identity/可见时间非法为 invalid；身份有效但资格证据不满足为 excluded。

样本须为 steady、ok，与部署角色和 layout 一致。trace 按 trace_id 去重和
冲突裁决；resource、sample_id、attempt 须与测量一致。trace 提供 producer、
boot、start_tick、end_tick。不能用不同 boot 的锚点互补，不能直接相减 tick
当微秒，不能用客户端 TTFT 代替 service。

anchors 按 `(producer,boot,tick)` 身份及上述可见/重复规则处理。校准点必须
在当前截止前已可见且 global_us <= 截止。对同 boot 的相邻可用锚点线性换算
tick 到 global_us；端点允许等号，不外推。global_us 必须严格递增。
有冲突的 tick 不能删除后跨过去扩大插值区间：包含该冲突 tick 的区间不可用。
start/end 均须有合法括号，start < end <= 截止；规范化 duration 与此跨度差异
不超过 duration_tolerance_us。有效成本使用时钟跨度，而非 reported duration。

分页证明按 lease_id 去重和冲突裁决，ingested_us 不能超过截止。valid_from_us
和 expires_us 是 global 微秒，区间左闭右开。resource、layout 必须一致。
page_index 从 0 开始，每页 tokens 是该页的完整 codepoint 文本。只累计从第 0
页开始连续、完整且与 prompt 相同的页。同一页出现不同的有效 tokens 即停止；
不能越过缺页，不能计入最后不满一页的尾部，不能沿用其他资源的缓存。
离线按服务 start 时刻验证 lease；线上按接纳时刻验证。prefill 的坐标为
prompt tokens 减已证明缓存；link 始终传输完整 prompt KV，decode 用完整 tokens。
所有角色的 cached_tokens 申报都须与本资源实际证明一致。坐标申报须核对重建值。

仅 cohort=baseline 的合格样本参与画像。每个部署 axis 节点独立计算 median
和 support，少于 minimum_samples 的节点成本为 null，不能借邻居/其他资源补齐。
中间查询只在相邻两节点都非 null 时线性插值；节点查询只需本节点。不能外推。
baseline 的慢成功、正常冷缓存都可保留；compile/warmup、失败和缺证据不贡献成本。
cohort=recent 的合格样本按 `(event_us,sample_id,attempt)` 取最后 drift_window 个；
对每个可支持样本算 service/本资源同坐标 baseline。数量不足则 insufficient_data；
否则 median 为 factor，严格超过 drift_threshold 为 drifting，其余 stable。
每个资源都必须有漂移条目；漂移报告不直接改变运行时 feedback factor。

build 必须生成以下 JSON（字段稳定，浮点仅为 JSON 表达）：

- `fabric-profiles.json`: `{as_of_us,resources:{id:{role,layout,axis,service_us,support}}}`。
- `phase-ledger.json`: `{rows:[{source,disposition}],samples:[{resource,sample_id,attempt,coordinate,service_us,event_us,cohort}]}`。rows 按 source 排序，samples 按 resource/sample_id/attempt 排序。
- `phase-audit.json`: `{input_records,qualified_samples,dispositions:{出现的状态:数量}}`。
- `phase-drift.json`: `{resource:{state,factor,samples}}`，证据不足 factor=null。

`capacity-notebook.json` 是旧 combined-TTFT 汇总，缺失阶段和布局资格，不能作为
新画像输入。数据没有预先标注错误原因，必须依据来源语义判断。
