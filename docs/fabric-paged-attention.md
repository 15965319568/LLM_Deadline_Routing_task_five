# 分页量化注意力与跨窗口 KV 工作区

本契约与其余六份 fabric 文档同时生效。生产服务还接收 `decode_kernel="paged-gqa"`
的 speculative 请求：后端交付量化 KV 页和分片 query，网关须完成注意力、输出投影、
logits 再量化，再执行原有 target/draft 或树状采样。`decode_kernel` 省略或 `"logits"`
保持旧协议；显式 null、未知值，或在非 speculative 请求中使用此字段，派发前返回400。
可用路径无 paged_attention 资产时返回503；原接纳、deadline、部署和租户规则同时适用。

## 部署资产与快照

decode-assets.json 的每个部署可增加 `paged_attention` 对象：

- revision：正整数；head_dim：2..16的偶数；kv_heads：1..8；head_map：长度1..16的
  整数列表，每项在0..kv_heads-1，按全局 query head 给出其 KV head，允许不连续分组。
- page_size：每物理页的slot数，1..16；window_size：正整数；sink_tokens：非负整数。
- rope_theta：有限数且>1；rope_scale：有限正数；nibble_order：low-first或high-first。
- alibi_slopes：每个全局query head一个有限数。
- ranks：非空数组；每项query_heads为非空全局head ID列表，另含有限正scale和0..255整数
  zero_point。所有rank的head ID合起来必须恰好覆盖全部query head一次，局部顺序任意。
- projection：词表大小×(query head数×head_dim)的uint8矩阵；projection_scale和
  projection_zero按token分别给出正有限scale、0..255整数zero；bias按token给出有限数。

整数不接受bool。所有shape、覆盖、范围和有限性在初始化/reload时校验，非法更新整体拒绝。
这些资产沿原decode部署身份读取；只读六份基础build/grammar产物即可，不增加build文件。
有效资产变化属于decode语义变化，按原规则更新epoch、清空反馈；同次多个变化只加一次。
只增加审计字段不算变化。在途请求冻结全部资产，包括projection与revision，reload不能替换。

## 后端窗口

普通SSE封帧、身份fence、window连续性、begin position、线性proposal/树、uniforms、
commit和DONE沿旧契约。paged-gqa窗口以page/query事件代替shard，不能混用logit shard。
一个请求可在窗口之间切换线性和树状草稿，后端KV/query行序不是逻辑位置顺序。
所有事件仍携带request_id、deployment_id、generation、tokenizer_revision、window。

begin额外包含：

- kv_table：非空列表，每项{position,page_id,version,slot}。position为非负整数且全表唯一；
  page_id为非空字符串；version为正整数；slot在0..page_size-1。可以多个逻辑position
  指向同一物理page/version/slot；这种别名只存一份物理页，分别参与各自逻辑位置的计算。
- query_positions：对象，键是query节点ID的标准十进制字符串，值是非负绝对位置。
  线性窗口的节点恰好0..k（k是proposal长度），树窗口为根0及全部树节点。位置可以不连续，
  不从slot、数组行号、proposal位置或到达时钟推算。

page事件：page_id、version、base_version。

- base_version=0：完整页，含k、v十六进制字符串，以及k_scale、k_zero、v_scale、v_zero。
  扁平元素顺序为slot→KV head→dimension。每元素uint4，两个元素一个byte，按nibble_order
  指定低/高半字节先后。编码须恰好容纳page_size×kv_heads×head_dim个元素，不接收额外空白。
  scale/zero数组长度为kv_heads×head_dim；按head/dimension广播到所有slot。scale正有限，zero为0..15整数。
- base_version>0：同page_id的已提交旧版本，且base_version<version；只含patches列表，不能
  同时携带k/v或量化参数。patch为{tensor:"k"或"v",index,value}，index是上述扁平索引，
  value为0..15整数；量化参数继承父页。空patch合法。相同index/value重传幂等，冲突值失败。
  不能基于本窗口尚未提交的页，不能引用已被回收的旧版本。

page在窗口内任意顺序到达。相同(page_id,version)按**展开后的量化元素及量化参数**比较：
内容相同幂等，冲突立即失败；完整页/差分页表示等价、patch顺序和审计字段不影响比较。
未被表引用的预取页仍须完整校验，但不会在commit后留存。其他新版本不会默默替代表中指定版本。

query事件含rank、node_ids、target。node_ids恰好覆盖全部query节点一次，target为
节点数×该rank head数×head_dim的uint8张量，行与node_ids对应。
线性窗口还必须有draft_node_ids及draft，其ID恰好为0..k-1，shape相应；树不要求draft。
各rank可以独立排列节点和draft节点，同rank相同语义的重传幂等，冲突立即失败。
未知rank、重复/缺失节点、错误shape/type/value均失败，未走到的树分支也必须校验。

commit前必须有全部query rank，以及表中指定的全部页版本（可来自本窗口或上窗口缓存）。
所有声明query行都计算，任一行没有合法因果上下文则窗口失败。旧窗口已输出的结果仍保留。

## 数值定义

1. query按所属rank `(q-zero_point)*scale` 解码。K/V按页的head/dimension通道参数解码。
2. Q使用query_positions的绝对位置，K使用kv_table.position；对相邻维度(2j,2j+1)
   旋转，角度为`position / rope_scale / rope_theta**(2j/head_dim)`。
   旋转结果为`(a*cosθ-b*sinθ, a*sinθ+b*cosθ)`；V不旋转。
3. 只保留key_position<=query_position，且满足key_position>=query_position-window_size+1
   或key_position<sink_tokens的条目。sink仍受因果约束。不能按物理页顺序截窗口或重复加入sink。
4. 每个query head经head_map选择KV head。分数为`dot(Q,K)/sqrt(head_dim)`加
   `alibi_slopes[head]*(key_position-query_position)`。对**全部有效逻辑key**统一稳定softmax，
   得到V的加权和；不能对每个页分别softmax后相加/平均。掩码发生在归一化之前。
5. 按全局query head顺序拼接各head输出，按token应用反量化projection后点积，再加bias。
   这包含所有rank的贡献；不能按query rank输出局部词表概率，也不能按rank到达顺序拼接。
6. 将global logit编码到原asset.shards：`round(logit/scale+zero_point)`，最近整数且正好
   半整数取偶数，再裁剪到0..255。这里的scale/zero属于原**词表分片**，与query rank不同。
   得到旧shard等价的target/draft矩阵后，执行既有采样、历史惩罚、DFA、stop和UTF-8语义。

采用不同内核、向量化、分块归一化或内部组织均可；最终token和文本必须一致。
验收样本避开不可稳定判定的浮点采样边界；原浮点容差1e-5保持有效。

## 工作区、原子性与持续接纳

页解码工作区是原prompt+max_tokens KV预留及proposal/tree scratch之外的额外decode页占用。
每个不同(page_id,version)占1工作区页，逻辑别名和幂等重传不重复计费。缓存页保持占用；
本窗口新页在首次有效page事件时原子增加占用，复制后写的新版本与旧版本在commit前同时计费。
decode资源pages和同租户max_pages都必须包含这些占用；不足则该窗口error，不能透支。
工作区不增加slots/work_us；原阶段释放、租户在途账单、外部有效占用及草稿scratch仍独立生效。

commit与采样共用原子边界：先完整验证并计算，只有窗口采样/文本提交成功才发布新页缓存。
缓存只保留本次kv_table引用的物理版本；其他缓存和预取页立即释放。窗口失败不发布部分页、
token、语法或历史；以前的提交计数和输出保留。error/cancel/成功终态均清理全部工作区，
无论发生在page、query、commit还是DONE阶段。流尚未结束时新请求须看到准确的占用。

diagnostics的paged_attention按本模式已接纳request_id提供：
`{revision,resident_pages,staged_pages,committed_windows,query_rows,last_positions}`。
接纳时计数0、last_positions=[]，revision为冻结资产revision。resident_pages为上次commit
留下的物理版本数；staged_pages为本窗口新增且不在缓存中的版本数。成功commit后staged归零，
committed_windows加1，query_rows累加本窗口target行数加draft行数（树仅target），
last_positions为本窗口所有query绝对位置升序去重。失败窗口不累计这些提交统计。
全部终态resident/staged为0，其余保留最后成功提交。允许附加审计字段。
原fabric_pages和连续节点/租户诊断包含工作区占用；其他指标与replay按原规范对账。

## 调查入口

`captures/paged-7`同时保留重传记录、别名映射、差分页、乱序head/query和完整推理流，
部署与语法原始证据延续三类既有输入。工作负载含跨窗口复用、发布期间在途快照、取消、
资源紧张和坏帧；应结合阶段checkpoint定位偏差。pilot中的均值预览仅为历史工具，不是验收答案。
公开示例与测试用于定位，不提供私有输入；私有验收不固定内部函数、状态容器或内核路径。
