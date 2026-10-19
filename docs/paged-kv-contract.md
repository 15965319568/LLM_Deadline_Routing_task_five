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

begin 使用整表 kv_table，或本文件“跨窗口逻辑视图”中的 kv_delta，恰好一种。kv_table 为非空列表，每项 `{position,page_id,version,slot}`：position 是非负绝对逻辑位置且全表唯一；page_id 是非空字符串；version 为正整数；slot 在 0..page_size-1。多个逻辑位置可以指向同一物理页/version/slot：它们分别参与计算，但只占一份物理页。表顺序不代表逻辑位置。

query_positions 的键恰好是节点 0..k 的标准十进制字符串，值为非负绝对位置；位置可以不连续，不能从 slot、到达顺序或节点 ID 推算。

未声明 encoding 的整页 page 事件含 page_id、version、base_version：

- base_version=0 为完整页，含 k/v 十六进制字符串与 k_scale、k_zero、v_scale、v_zero。元素顺序为 slot→KV head→dimension，每元素 uint4，两个元素一个 byte，先后由 nibble_order 指定。数据须恰好覆盖 page_size×kv_heads×head_dim 个元素，不接受额外空白。scale/zero 各有 kv_heads×head_dim 项，按 head/dimension 广播到所有 slot；scale 正有限，zero 为 0..15 整数。
- base_version>0 为差分页，仅含 patches，不能同时含完整页的张量或量化参数。父页必须是同 page_id 的已提交旧版本，且 base_version<version。不能继承本窗口尚未提交或已回收的页。量化参数继承父页，父页保持不变。
- patch 为 `{tensor,index,value}`，tensor 为 k/v，index 为扁平元素下标，value 为 0..15。空 patches 合法；同 index/value 重复幂等，冲突值非法。

以下整页规则与“分片传输记录”共同生效；整页不声明 encoding。page 可乱序到达。同 (page_id,version) 按展开后的量化元素及参数比较：相同幂等，冲突立即使窗口失败；完整页与等价差分页、patch 顺序及审计字段不改变语义。未被表引用的预取页仍需校验。其他版本不能替代表中指定版本。

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


## 分片传输记录

新的 worker 仍发送 kind="page" 事件，使用 encoding="kv-fragments/v1"。所有记录都带 page_id、version 及通常的请求/部署/window 身份；物理身份仍为 (page_id,version)。encoding 的其他值非法。记录的两种形式为：

- record="manifest"：base_version 和 segments；segments 为 `{id,revision}` 列表，精确选择这个物理版本的片段集合。id 为非空字符串，revision 为正整数，相同二元组不能重复，列表顺序无意义。base_version=0 时还带原整页协议的 k_scale/k_zero/v_scale/v_zero；完整页元素由 span 提供。base_version>0 时不带这些量化参数，由父页继承。
- record="span"：id、revision、tensor、offset、count、data。tensor 为 k/v；offset 为扁平量化元素的零起始下标，count 为正整数，范围不能超出一页。data 为十六进制 uint4 字符串，每字节两个元素，nibble_order 沿用冻结资产；长度恰好为 `2*ceil(count/2)` 个字符。count 为奇数时最后一个未使用半字节必须为零。offset/count 的单位是元素，不是字节。片段可跨 slot/head 边界。

manifest 和 span 可以任意顺序到达。所有记录都先检查类型、数值范围和编码合法性，包括最终未被选中的记录。附加审计字段不参与语义比较；其余未列字段不产生额外含义。整数不接受 bool。

manifest 对同一物理版本唯一：按 base_version、片段选择集合及量化参数比较，等价重传幂等，矛盾 manifest 在收到时失败。span 按 (id,revision) 关联，按解码后的 tensor/offset/元素比较；等价重传幂等。只有 manifest 选中的每个身份必须恰好对应一种内容。未选中的旧版、新版及其内容冲突不决定页内容；不能以到达次序或最大 revision 代替选择清单。选中身份缺失或存在不同内容时，窗口在 commit 校验中失败。

选中的片段允许相互覆盖，同一 tensor/index 的值必须一致。完整页的 k 和 v 都必须完全覆盖；差分页只覆盖变更元素，可以选择空集合。片段顺序与切分方式不改变展开结果。

分片 manifest 的父页必须同 page_id、base_version<version，可以是仍驻留的已提交版本，也可以是本窗口的有效整页/分片版本。父页可以晚到；在 commit 之前，不完整的依赖可以暂存。commit 时必须解析所有已出现的分片版本，包括未被逻辑表引用的预取和中间版本；缺 manifest、缺父页、缺选中片段或内容冲突均失败。已回收版本不可作为父页。展开时父页保持不变。旧整页 delta 的父页仍限于已提交版本，不扩大其既有规则。

同一物理身份同时出现整页与分片表示时，展开后的量化元素及参数必须相等。表示不同不构成新的物理页。成功提交仅保留当前逻辑视图引用的版本；解析子页所需的中间父页并不因此永久驻留。

容量口径：首次收到某物理身份的合法记录，即持有一页工作区，不等待 manifest、片段齐全或数值计算。重复、分片数和传输 revision 不增加物理页数。总持有集合是已提交缓存与本窗口出现的物理身份的并集。resident_pages 是上次提交保留的版本数；staged_pages 是本窗口已出现且不在缓存中的版本数，包括未完成的上传。预算不足时按给定服务边界失败；失败/取消的未完成传输也必须释放。成功提交、错误与终态规则仍以本文件前述要求为准。

## 跨窗口逻辑视图

begin 可以用 kv_delta 代替 kv_table。kv_delta 为 `{base_window,remove,put}`；base_window 是零起始、最近一次成功提交的窗口编号，首窗口不可使用 delta。remove 是互不重复的非负逻辑位置，必须存在于该已提交视图；put 是原 kv_table 行格式，位置互不重复。基于已提交视图先执行 remove，再按 position 新增或替换 put；同一位置在 remove 和 put 中出现合法。结果必须非空。一次 begin 不能同时给整表和 delta。

逻辑视图仅在该窗口成功输出时提交。物理页缓存和逻辑视图是不同对象：逻辑位置变化可不传新页，多个位置可指向同一物理 slot，页仍只计一次容量，每个逻辑位置独立参与 RoPE、掩码、ALiBi 和归一化。表只给出映射，不能凭一个更高物理 version 自动替换指定版本。错误或取消不发布半张视图，也不更改先前已提交统计与输出。
