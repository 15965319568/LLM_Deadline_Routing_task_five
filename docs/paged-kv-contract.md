# 量化分页上下文契约

本文件与 `paged-service-boundary.md` 共同规定本题范围。输入为既有生成器消费的分页 target/draft 查询；通用采样器、网络封帧和文本输出已提供。分页路径须把这些输入解释为正确的 logits，并维持正确的物理页生命周期。线性草稿是调用上下文，不要求实现独立 speculative 算法；树状草稿和语法编译不在本次范围内。

## 资产和输入身份

每个 decode 资产的 `paged_attention` 包含：

- revision：正整数；head_dim：2..16 的偶数；kv_heads：1..8。
- head_map：长度 1..16，元素为 0..kv_heads-1，按全局 query head 指明 KV head，允许不连续分组。
- page_size：1..16；window_size：正整数；sink_tokens：非负整数。
- rope_theta：有限且大于 1；rope_scale：正有限数；nibble_order：low-first 或 high-first。
- alibi_slopes：每个全局 query head 一个有限数。
- ranks：每项含 query_heads（非空全局 head ID 列表）、正有限 scale、0..255 的 zero_point。所有 rank 合起来恰好覆盖全部 query head 一次；局部顺序任意。
- projection：词表大小 × (query head 数 × head_dim) 的 uint8 矩阵；projection_scale、projection_zero、bias 按 token 提供正有限 scale、0..255 zero 和有限 bias。

资产校验与接纳快照加载已提供。所有整数不接受 bool。一次请求使用接纳时的完整资产；后续 reload 不能改变在途页的布局解释、投影或 revision。

后端每条 JSON 事件携带 request_id、deployment_id、generation、tokenizer_revision、window、kind。身份与请求快照匹配，window 从 0 连续增长。既有帧处理器负责封帧和身份校验；分页组件仍须拒绝坏页/坏 query。允许附加审计字段。

## 窗口与量化页

线性窗口的 begin 含 position（此前已提交 token 数）、proposal（k 个 token）、uniforms（k+1 个数）、kv_table、query_positions。k 在 1..8。begin/commit/DONE 的调度与草稿资源预留由已有调用方提供。

kv_table 为非空列表，每项 `{position,page_id,version,slot}`：position 是非负绝对逻辑位置且全表唯一；page_id 是非空字符串；version 为正整数；slot 在 0..page_size-1。多个逻辑位置可以指向同一物理页/version/slot：它们分别参与计算，但只占一份物理页。表顺序不代表逻辑位置。

query_positions 的键恰好是节点 0..k 的标准十进制字符串，值为非负绝对位置；位置可以不连续，不能从 slot、到达顺序或节点 ID 推算。

page 事件含 page_id、version、base_version：

- base_version=0 为完整页，含 k/v 十六进制字符串与 k_scale、k_zero、v_scale、v_zero。元素顺序为 slot→KV head→dimension，每元素 uint4，两个元素一个 byte，先后由 nibble_order 指定。数据须恰好覆盖 page_size×kv_heads×head_dim 个元素，不接受额外空白。scale/zero 各有 kv_heads×head_dim 项，按 head/dimension 广播到所有 slot；scale 正有限，zero 为 0..15 整数。
- base_version>0 为差分页，仅含 patches，不能同时含完整页的张量或量化参数。父页必须是同 page_id 的已提交旧版本，且 base_version<version。不能继承本窗口尚未提交或已回收的页。量化参数继承父页，父页保持不变。
- patch 为 `{tensor,index,value}`，tensor 为 k/v，index 为扁平元素下标，value 为 0..15。空 patches 合法；同 index/value 重复幂等，冲突值非法。

page 可乱序到达。同 (page_id,version) 按展开后的量化元素及参数比较：相同幂等，冲突立即使窗口失败；完整页与等价差分页、patch 顺序及审计字段不改变语义。未被表引用的预取页仍需校验。其他版本不能替代表中指定版本。

query 事件含 rank、node_ids、target、draft_node_ids、draft。target 的 shape 为 (k+1)×该 rank head 数×head_dim，元素 uint8，行与 node_ids 对应，节点恰好覆盖 0..k 一次。draft 对应 0..k-1，shape 为 k×该 rank head 数×head_dim。各 rank 的 target/draft 行可以独立重排。相同 rank 按节点映射后的语义比较，重传幂等、冲突失败；非法 rank、缺失/重复节点、错误 shape/type/value 均失败。

commit 前需要全部 query rank 和 kv_table 引用的全部页版本。所有声明 query 行均须有合法因果上下文，不能只计算最终选中的 token 所需行。失败窗口不改变先前成功提交。

## 数值语义

1. query 按所属 rank 的 `(q-zero_point)*scale` 解码。K/V 按页的 head/dimension 参数解码。
2. Q 使用 query_positions 的位置，K 使用 kv_table.position。相邻维度 (2j,2j+1) 的旋转角为 `position / rope_scale / rope_theta**(2j/head_dim)`，结果为 `(a*cosθ-b*sinθ, a*sinθ+b*cosθ)`；V 不旋转。
3. key_position 必须不大于 query_position，且满足 `key_position >= query_position-window_size+1` 或 `key_position < sink_tokens`。sink 仍受因果约束，不重复计入。
4. 每个 query head 经 head_map 选 KV head。分数为 `dot(Q,K)/sqrt(head_dim) + alibi_slopes[head]*(key_position-query_position)`。掩码后对全部有效逻辑 key 统一稳定 softmax，对 V 加权；物理页边界不划分归一化集合。
5. 按全局 query head 顺序拼接输出，按 token 反量化 projection 后点积，再加 bias。所有 rank 都贡献到同一全局 logit。
6. 输出给已有采样器的词表分片使用资产的 shards（与 query ranks 不同）。每个 token 编码为 `round(logit/scale+zero_point)`，半整数取偶，再裁剪到 0..255；token_ids 决定局部列顺序。target 行按节点 0..k，draft 行按 0..k-1。

既有消费者完成 target/draft 接受/替换、token 和文本生成。本题不增加其功能要求。可采用向量化或不同算法组织；最终 logits 的等价影响、token 和文本必须正确。样本避开不稳定采样边界；浮点容差为 1e-5。

## 跨窗口所有权与提交

每个不同 (page_id,version) 占一工作区页，额外于已有 prompt+max_tokens 预留和草稿 scratch。别名、幂等重传不重复计费。缓存页维持占用；新页第一次有效到达时增加占用，新旧版本在提交前同时计费。资源容量与同租户 max_pages 都包括工作区；不足时窗口失败，不能透支。工作区不增加 slots/work。

先完整验证并计算，只有已有消费者成功提交该窗口的 token/text，才发布本窗口的新页缓存。成功后仅保留本次 kv_table 引用的物理版本，回收其他缓存与预取页。失败不发布部分页或本窗口输出；之前提交的前缀保留。

成功终态、error、cancel 均清理工作区且只释放一次。流未结束时，其他请求必须看到真实占用；取消后到达的旧帧不能复活窗口。调用方提供容量控制，分页路径负责准确提交持有页数及发布/清理时机。

`/fabric/diagnostics` 的 paged_attention 按已接纳分页 request_id 提供：`revision,resident_pages,staged_pages,committed_windows,query_rows,last_positions`。接纳时计数为 0，last_positions=[]。revision 为冻结资产版本；resident_pages 为上次提交留下的物理版本数；staged_pages 为本窗口新增且不在缓存中的物理版本数。成功提交 staged 归零，committed_windows 加 1，query_rows 增加本窗口 target+draft 行数，last_positions 为查询绝对位置升序去重。失败窗口不累计提交统计。终态 resident/staged 归零，其他字段保留最后成功提交。允许额外字段。
