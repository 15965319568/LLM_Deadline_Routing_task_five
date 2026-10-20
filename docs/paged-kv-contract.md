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

未使用共享前缀时，每个请求内不同 (page_id,version) 占一工作区页，额外于已有 prompt+max_tokens 预留和草稿 scratch。别名、幂等重传不重复计费。缓存页维持占用；新页第一次有效到达时增加占用，新旧版本在提交前同时计费。资源容量与同租户 max_pages 都包括工作区；不足时窗口失败，不能透支。工作区不增加 slots/work。

先完整验证并计算，只有已有消费者成功提交该窗口的 token/text，才发布本窗口的新页缓存。成功后仅保留本次 kv_table 与显式 kv_keep 引用的物理版本，回收其他缓存与预取页。失败不发布部分页或本窗口输出；之前提交的前缀保留。

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

同一物理身份同时出现整页与分片表示时，展开后的量化元素及参数必须相等。表示不同不构成新的物理页。成功提交仅保留当前逻辑视图与显式 kv_keep 引用的版本；解析子页所需的中间父页并不因此永久驻留。

容量口径：首次收到某物理身份的合法记录，即持有一页工作区，不等待 manifest、片段齐全或数值计算。重复、分片数和传输 revision 不增加物理页数。总持有集合是已提交缓存与本窗口出现的物理身份的并集。resident_pages 是上次提交保留的版本数；staged_pages 是本窗口已出现且不在缓存中的版本数，包括未完成的上传。预算不足时按给定服务边界失败；失败/取消的未完成传输也必须释放。成功提交、错误与终态规则仍以本文件前述要求为准。

## 跨窗口逻辑视图

begin 可以用 kv_delta 代替 kv_table。kv_delta 为 `{base_window,remove,put}`；base_window 是零起始、最近一次成功提交的窗口编号，首窗口不可使用 delta。remove 是互不重复的非负逻辑位置，必须存在于该已提交视图；put 是原 kv_table 行格式，位置互不重复。基于已提交视图先执行 remove，再按 position 新增或替换 put；同一位置在 remove 和 put 中出现合法。结果必须非空。一次 begin 不能同时给整表和 delta。

逻辑视图仅在该窗口成功输出时提交。物理页缓存和逻辑视图是不同对象：逻辑位置变化可不传新页，多个位置可指向同一物理 slot，页仍只计一次容量，每个逻辑位置独立参与 RoPE、掩码、ALiBi 和归一化。表只给出映射，不能凭一个更高物理 version 自动替换指定版本。错误或取消不发布半张视图，也不更改先前已提交统计与输出。

## 共享前缀：请求命名空间与物理对象

共享前缀复用的是已提交的不可变 KV 图像。各请求的 page_id/version 仅在该请求内命名：两个请求独立上传同名、同值的页，仍是两个物理对象，不能按名称或内容自动去重。以下导入和后文读取回执是跨请求共享物理对象的方式。此前生命周期规则中的物理身份，在存在导入时按本节解释。

begin 可附加 `kv_imports` 列表（缺省为空），每项为：

`{request_id, window, page_id, version, as_page_id, as_version}`

request_id 为源请求；window 是源请求**当前最近成功提交**的零起始窗口号；page_id/version 指向源请求仍保留的页；as_page_id/as_version 是接收请求中的名称。字符串非空，version/as_version 为正整数，window 为非负整数，不接受 bool。源请求不能是自己，须仍在服务中；未提交的暂存页、已回收页、已结束请求及错误的源窗口号都不能导入。即使某旧页还在，也不能用旧的源窗口号获取历史快照。

导入双方必须有相同 tenant、decode 资源、接纳时 deployment_id/generation/tokenizer_revision 及完整 paged_attention 资产。reload 后的比较依据各自接纳快照，不是此刻全局配置。以上任一不匹配则窗口失败。导入不是可选缓存命中提示，不允许静默转成空页或读取未来帧。

一次 begin 的所有导入共同成功或共同失败。除下文声明的 replace_window 替换外，接收方名称不能与自己的驻留名称冲突，列表内接收方名称也不能重复；两个不同接收方名称指向同一源物理对象合法。导入成功时即固定图像及物理对象，不等 query/commit。源请求随后提交、回收、错误或取消，不影响已经固定的读者。只要读者仍持有该页，它可以在自己的成功提交之后作为另一请求的来源；来源中的名称和原始创建者不同不改变物理身份。

导入页可以进入本窗口页表，也可以作为分片/压实依赖。它不因此成为“本请求先前已提交页”；旧整页 delta 的父页限制保持不变。同一接收方名称收到整页或分片重传时，仍须与导入图像相等，不能覆盖共享对象。生成一个新的本地版本是新的物理对象，任何更新都不能修改其他版本或其他读者持有的图像。

资源页数与同租户 max_pages 按所有存活读者共同持有的**不同物理对象并集**计费，加上既有请求预留和 scratch；导入不复制存储，不增加 slots/work。单个读者引用一个对象多次也只占一次。创建者退出不是回收条件；最后一个读者释放才回收。预取、未完成目的页、压实中间页在存在期间也计费，即使未参与最终输出。

成功提交后，接收方只保留该逻辑视图与显式 kv_keep 引用的名称与物理对象；其余导入、上传及中间结果释放。先完成数值与输出提交，再更换可供其他请求获取的视图。失败不发布半张可借用视图；终态清理仅解除该读者的持有，不能影响其他读者。后来的非法/旧帧不复活对象。

诊断 resident_pages 是该请求最近提交视图保留的不同物理对象数；staged_pages 是本窗口持有且未被其驻留集合包含的不同对象数。导入别名和原名称可使本地名称数大于这个计数。不同请求的 resident/staged 相加也可能大于池中的物理页数。其余统计沿用前文。

## 共享 KV 压实记录

worker 可用 `encoding="kv-fragments/v2"` 描述压实后的目的页。沿用 v1 的 manifest、span、选择身份、重传、参数、孔洞及重叠规则；v2 manifest 的 base_version 必须为 0，并给出目的页全部量化参数。物理对象不是按版本号大小排序的全局时间线。一个窗口内同一目的名称只能采用一种分片 encoding；可以另有与其语义相同的整页表示。

v2 另有 `record="gather"`，与 span 共用 `(id,revision)` 选择空间：

`{id, revision, tensor, offset, count, source:{page_id,version,offset,stride}}`

目的 tensor 为 k/v，offset/count 以扁平元素为单位，连续覆盖目的区间；count 为正整数。source 引用接收请求命名空间中的一个精确页版本，并从同名 tensor 读取 `source.offset+j*source.stride`（j 从 0 到 count-1）。stride 为非零整数，可为负数；所有源/目的下标都必须在页内，类型与整数限制沿用 v1。gather 可跨 slot/head 边界。未选中记录仍要通过类型、范围等语法校验，但不要求它的源实际存在，不追踪其依赖。

gather 的语义是保持源元素的反量化数值，再按目的位置的量化参数编码为 uint4。源和目的的 scale/zero 分别由各自扁平下标的 head/dimension 通道决定，不能按目的通道解释源值。目的编码采用最接近整数、恰好半整数取偶，裁剪到 0..15。和 literal span 重叠时比较最终目的编码以及后文定义的 after 依赖。压实图像仍存储未旋转 K/V；逻辑位置的 RoPE、掩码和 ALiBi 继续由读取视图决定。

选中 gather 可依赖已提交页、本窗口导入页、整页或分片目的页，源记录可以晚到、版本号可以比目的大，也可以经多个不同 page_id 中转。commit 时所有已出现的目的页必须完整有效，包括中间和预取目的页；选中的依赖缺失、有环或与已知整页/导入图像冲突均失败。不得以未选中 revision 的依赖拒绝一个合法窗口。共享源只在 begin 时固定；之后的解析必须使用该读者持有的图像。

每个新目的名称从第一条合法记录到达起占一物理页。不能先把源页释放来“借出”容量，也不能等 gather 被解析或目的页第一次被查询才计费。数据依赖、逻辑引用和物理持有是三个不同关系；最终输出、下个窗口可读性及其他请求的接纳都必须一致。

## 稀疏视图与保留集合

共享部分前缀时，`kv_imports` 项可带 `slot_map`，其长度等于 page_size。接收方 slot i 映射到源**已提交视图**的 slot_map[i]；元素是合法 slot 整数或 null，至少一项非 null。重复映射合法，null 表示该接收方 slot 不存在，不能当作数值零。非 null 项必须指向源视图有效槽位。省略 slot_map 保留源视图原有的槽位顺序和有效集合。转借按视图逐层组合，不按最初生产者的槽位重新解释。K/V 量化参数仍按 head/dimension，逻辑位置仍由接收方 kv_table 决定。

一个导入视图保留源物理对象的身份，即使它隐藏了大部分槽位、改变了槽位顺序或重复同一槽位。视图不复制存储、不按有效槽位比例计费。不同视图可有不同的可读槽位和数值序列，但共享同一物理页。对于省略 slot_map 的稀疏源，恒等投影保留其已有有效集合（不要求把不存在的槽位补齐）。

v2 manifest 可声明 `valid_slots`：非空、无重复的合法 slot 整数列表，顺序无意义；省略时为全部槽位。只有有效槽位的 K/V 元素须完整定义；无效槽位没有数值，不能读取或写入。所选 span/gather 的目的元素必须全部落在有效槽位内；gather 的源元素也必须有效。未选中记录照常做类型、范围和编码检查，但不跟随其内容依赖。valid_slots 属于 manifest 语义，不能在等价重传中改变。

所有 kv_table 行都必须引用有效槽位，包括被因果或滑动窗口掩码排除的行。后续整页 delta、v1 分片 delta 继承稀疏父页的有效集合，不能通过 patch 把无效槽位变为有效；原有整页与 v1 的其他规则不变。整页 full 是稠密图像，不能与同名稀疏图像当成等价重传。

begin 可带 `kv_keep`，为 `{page_id,version}` 列表，默认空、名称不能重复。它指定成功提交后额外保留的页名称，不增加逻辑注意力位置。所引用页可以在本窗口晚到，但 commit 时必须可用且完整有效。每次提交以当前 kv_table 与当前 kv_keep 的并集决定保留名称，不自动延续上个窗口的 keep 清单。保留页可以被其他请求导入，也可以供后续压实使用。多个名称仍按不同物理对象并集计费；resident_pages 包含这些额外保留页。漏掉来源、重复清单或错误窗口不能降级为缓存未命中。

## 继承压实与写时复制

v2 manifest 还可带 `inherit:{page_id,version,slot_map}`；slot_map 可省略为恒等映射。base_version 仍为 0，仍须给出目的量化参数。inherit 指向接收方当前窗口可见的精确源页，可来自缓存、导入或本窗口晚到的目的页，版本号大小不限制跨名称依赖。

继承先按 slot_map 取源视图，按源参数反量化，再按目的参数取偶舍入、裁剪到 uint4。它只为目的有效槽位提供基线：映射为 null 的槽位没有基线，必须由选中片段完整补齐。显式非 null 映射必须引用有效源槽位，即使该目的槽位随后被覆盖；省略映射则保留源的稀疏有效集合。选中 span/gather 覆盖基线，与基线值不同合法；多个选中片段之间的重叠仍须一致。有效槽位最终不能有孔洞，无效槽位不参与完整性要求。

inherit 是 manifest 的一部分，参与等价重传比较，并始终是解析依赖，不能因为覆盖范围较大就忽略缺失或循环来源。没有 inherit 的 v2 沿用完整覆盖规则。源图像、其他别名及其他读者保持不变。目的物理对象从第一条合法记录到达起独立占一页；成功后可以释放只作数据依赖的来源，后续读取目的页不需要保持祖先永久存活。预取目的页也适用这些规则。

## 发布中的名称替换与重新分配

kv_imports 项可带 `replace_window`。它必须是接收方最近成功提交的窗口编号，且 as_page_id/as_version 必须已经在接收方已提交保留集合中。此项允许用一个新的导入视图替换该本地名称；没有 replace_window 时仍禁止名称冲突。一个 begin 的所有导入、槽位映射、替换条件与 kv_keep 语法共同验证，全部成功后才开始窗口，任一失败不能遗留部分持有。

该窗口内部的逻辑视图、gather、inherit 使用新名称绑定；旧整页 delta 的父页仍取接收方上次提交的缓存。窗口成功前，对外可借用的名称和图像仍是最近成功提交的版本，不能把新绑定与旧图像混合提供给第三个请求。新旧物理对象都需要持有，哪怕本地名称相同；resident_pages 描述上次提交的物理集合，staged_pages 描述本窗口持有但不在该集合中的对象。成功输出后再整体发布新绑定与保留集合。失败或取消不发布新视图，已有外部读者继续使用其固定的旧视图。

如果一个本地名称已经从请求保留集合中释放，之后同一请求再次上传相同 page_id/version，则是一次新的物理分配。其他读者可能仍持有此前分配，即使名字甚至数值完全相同，也不能把两次分配合并。新分配可以在符合前述规则时被再次导入。物理身份无需出现在公开 JSON 或使用指定算法；验收通过旧/新读者数值、连续容量和后续接纳观察这一点。


## 推测前缀依赖与已提交视图

worker 可在同一物理页内携带尚依赖当前 proposal 的元素。非树窗口 proposal 长度为 k；下述依赖值均为 0..k 的整数，不接受 bool，缺省为 0。它们表示当前窗口至少接受多少个 proposal token，该元素才属于已提交前缀。依赖与量化编码、有效槽位共同构成图像语义；相同数值不代表等价证据。

- 完整页的 `k_after`、`v_after` 为与对应扁平 K/V 元素等长的列表，可分别省略。
- 整页 delta 的每个 patch 可以带 `after`；delta 不得在顶层声明 k_after/v_after。未覆盖元素继承父图像的依赖。
- v1/v2 literal span 可带标量 `after`，作用于该记录覆盖的所有元素。
- v2 gather 可带标量 `after`；每个目的元素的依赖为该值与对应源元素依赖的最大值。源/目的坐标和量化换算遵守前文。
- slot_map 和 inherit 逐元素保留来源依赖。选中 span/gather 覆盖 inherit 基线时，其编码及依赖一起替换基线；不把已覆盖基线的依赖继续累加。

相同选择身份的重传、已知整页与分片的等价表示，以及多个选中片段的重叠，均比较编码和依赖。未选中记录仍须校验 after 类型与范围，但不追踪其来源。依赖不能赋予无效槽位数值；所有既有覆盖、孔洞和有效槽位规则继续生效。

一个有效槽位的依赖是该槽位全部 K/V 元素依赖的最大值。target/draft 查询行的 node n 只能读取依赖不超过 n 的逻辑行，然后再应用既有因果/窗口/sink 掩码及位置计算。每个给出的 query 行都须有合法上下文，即使实际采样提前结束，没有消费该行。不能以最终接受数量提前过滤整个窗口的查询。

采样成功提交后，令 a 为本窗口**实际接受的 proposal token 数**；correction/bonus token 不增加 a，提前达到输出上限时不计未消费的 proposal。提交保留的每个页视图只保留依赖不超过 a 的有效槽位；相应逻辑行也须移除。没有存活槽位的名称被释放，即使声明于 kv_keep。部分存活的视图保持物理身份，仍按整页计费。存活元素的依赖归零，成为后续窗口/读者的已提交前缀；不得原地修改其他别名、来源或已有读者的图像。失败窗口不发布。

诊断新增 `published_slots`（最近提交缓存中按本地名称求和的有效槽位数；别名可重复计数；终态为 0）和 `accepted_frontier`（最近成功窗口的 a，尚无提交为 0，终态保留）。既有 committed_windows/query_rows/last_positions 统计保持原义。

## 异步读取快照与迟到回执

POST `/fabric/kv-reads` 接受两类 body：

```json
{"action":"pin","read_id":"reader-x","attempt":1,"sources":[{"request_id":"source-x","window":0,"page_id":"p","version":1}]}
{"action":"complete","read_id":"reader-x","attempt":1}
```

read_id 非空，attempt 为正整数，不接受 bool；(read_id,attempt) 标识一次独立读取。pin 的 sources 为有序非空列表，不得包含重复来源；每项字段和类型同前文请求来源，且来自仍活跃请求当前最近提交的快照。全部来源的 tenant、decode 资源和接纳时完整资产 scope 必须一致。所有来源共同验证后固定视图和物理对象；原请求推进、失败、结束或取消不改变这份快照，也不结束读取。

相同读取标识、相同规范化 sources 的 pin 重传幂等；不重新访问已变化或退出的来源。相同标识但不同 sources 返回 409，不改变原读取。extra 审计字段不参与来源相等比较。complete 仅完成对应 attempt，多次 complete 幂等。complete 可先到：后来的 pin 仍校验来源字段语法并记录其规范化描述以识别后续冲突，但不查找来源、不固定页、不复活读取。未完成的其他 attempt 不受影响。

合法操作返回 200；字段/类型错误、来源不可用、混合 scope 等返回 422，失败不得留下部分持有。不要求固定响应 JSON、内部对象标识或哈希算法。

begin 的 kv_imports 也允许如下来源，与旧的 request_id/window/page_id/version 来源恰好二选一：

```json
{"read":{"read_id":"reader-x","attempt":1,"index":0},"as_page_id":"alias","as_version":1}
```

index 为非负整数且在 sources 范围内；引用必须是已 pin 且未 complete 的读取。其 scope 必须与接收请求一致。as_*、slot_map、replace_window、原子导入和提交规则与旧来源相同。导入立即固定独立持有；读取随后 complete 不影响已导入请求。引用已完成/不存在读取或混合来源字段是窗口错误。

容量按所有请求和所有未完成读取共同持有的不同物理对象并集计费，再加既有预留/scratch。请求已结束但读取未完成时，孤立读取仍占资源及租户 pages，不占 slots/work。最后一个请求和读取都释放才回收。回执不应通过重复扣减导致负计数，也不能把已完成 attempt 的晚到 pin 计作新分配。上述读取、推测发布、稀疏投影和压实使用同一物理页生命周期。

## 分块 DMA 与分页视图

新 worker 使用分块 DMA 搬运同一批量化 KV。旧完整页/v1/v2 仍按此前规定分配独立的一页；`kv-striped/v1` 页是由 DMA 块支撑的视图，不再拥有一份额外完整页分配。以下定义扩展同一数值、共享和发布链路，不增加独立控制系统。

### worker 记录

`kind="kv_block"` 与 page/query 使用相同的请求、部署及 window 身份，只能出现在 begin 与 commit 之间。块身份为该请求内的 `(block_id,epoch)`；block_id 非空、epoch 为正整数。独立请求发送同名同值块也不去重。令 N=page_size×kv_heads×head_dim。记录有两种形式：

- `record="directory"`：`length` 为 1..2N 的元素数，`channels` 为 1..2N 的通道数；`scale`、`zero` 长度为 channels，分别为正有限数和 0..15 整数；`chunks` 为不重复的 `{id,revision}` 选择列表，id 非空、revision 正整数。目录选择准确身份，不能用最高 revision 或到达顺序替代。目录按这些字段的语义比较，选择顺序无意义；矛盾目录收到时失败。
- `record="chunk"`：`id,revision,offset,count,data,nibble_order`，可带标量 `after`。offset 非负、count 正整数、offset+count≤2N；data 为 uint4 十六进制，长度恰为 `2*ceil(count/2)`，不接受空白。每条 chunk 自带 low-first/high-first 半字节顺序，与 attention 资产中的整页顺序独立；奇数 count 的未使用半字节必须为零。after 默认为 0，须是 0..当前 proposal 长度的整数。

所有整数字段不接受 bool。所有记录都校验语法，包括未选中的旧记录；额外审计字段不参与相等比较。相同 chunk 身份按 offset、解码元素、after 比较；相同幂等，不同内容可以暂存。commit 时每个目录选择身份必须有且仅有一种语义，未选择身份的内容矛盾不影响该块。选择 chunk 必须位于 directory.length 内，其并集覆盖完整 length；重叠元素须同时满足编码和 after 相等。块内容、源量化参数和选择证据必须完整有效，包括未被最终页视图引用的预取块。目录、chunk、页映射的到达顺序任意。

### 页映射与数值解释

`kind="page",encoding="kv-striped/v1",record="manifest"` 使用 page_id/version，带完整页的 k_scale/k_zero/v_scale/v_zero，以及可选 valid_slots（默认全部槽位）。`runs` 是非空列表，每项为：

```json
{"tensor":"k","offset":0,"count":4,"block_id":"buffer-a","epoch":2,"start":7,"stride":-2,"after":0}
```

tensor 为 k/v，offset/count 指定目的页连续扁平区间，须在 N 内且全部落在 valid_slots；start+j×stride 为源块元素下标，stride 为非零整数。收到映射时地址须在 0..2N-1；commit 时还必须在所选块实际 length 及其可读地址集合内。一个页的全部有效 K/V 元素必须覆盖，不能为孔洞补零。runs 顺序及完全重复的 run 不改变语义；覆盖同一目的元素的记录必须指向相同块身份、相同源地址且 after 相同，否则即使编码恰巧相同也失败。相同页名/version 的映射须语义一致。同一窗口中该名字不能再使用旧整页、v1/v2 或导入表示；已提交的页名/version 不接受新的 striped manifest，重映射使用新的页名或版本。

源地址 s 的通道是 s % directory.channels；先按 `(code-zero[channel])*scale[channel]` 解码，再用目的下标 d % (kv_heads×head_dim) 的页参数编码，最接近整数、半整数取偶、裁剪到 0..15。该 uint4 图像继续进入本契约已有注意力计算；不能省略中间量化、提前旋转 K、按目的通道读取源参数或按物理块分别做 softmax。元素依赖为 run.after 与源 chunk.after 的最大值；其 query 可见性和实际接受后的发布规则同前文。

### 驻留、地址可见性与后续共享

第一条语法合法的块记录到达即持有一个物理分配，大小不足 2N、分片未到齐、只用于预取均占一页容量。相同块后续记录不重复计费。单独收到页映射不创建块或额外页；commit 缺少引用块时失败。新旧 epoch 同时存在时各占一页；不能先释放来源来支付目的分配。旧页分配与新块分配在同一资源和租户容量池中求并集。

一个 striped 页的不同槽位可以指向不同块，多个 striped 页也可共享同一块。成功提交时先按实际接受的 proposal 前缀裁掉无效槽位，再按保留视图的有效 K/V 元素收集实际支撑块。没有保留元素的块从该请求释放；仍有任一保留元素的块占完整一页，不按有效地址比例计费。`kv_keep` 保留名称和有效视图，不强迫保留已被接受前缀裁掉的槽位。旧完整页、v1/v2 压实目的仍拥有自己的独立分配，不因其来源是 striped 页而共享目的存储。

块仍驻留不代表全部旧地址仍可读。一个成功窗口之后，该请求能通过块名读取的地址集合，精确等于此次保留页视图仍引用的源地址并集；这些地址的 after 归零。下一窗口可用新 manifest 引用这些驻留地址，无需重传；未发布/已丢弃地址不能因新映射、等价重传或旧 bytes 尚在而复活。重传目录/chunk 与原证据比较，不能扩大已发布地址集合。块从该请求完全释放后，相同 block_id/epoch 进入已回收状态，本请求不能再次上传该身份；不同 epoch 是新分配。epoch 不要求到达顺序递增。

请求导入、读取回执、slot_map、名称替换继续固定不可变图像，并同时固定这些有效元素实际支撑的块。接收方不继承来源的块命名空间；若要重组导入内容，可用已有 v2 gather/inherit 创建独立目的页。稀疏投影只持有其有效元素需要的块；隐藏部分地址仍可能保留整个块。创建者推进或结束后，其他请求/未完成回执仍持有的块不得释放；回执完成也不能释放已导入读者独立持有的块。K/V 可能来自不同块，两者都须计入。

既有 resident_pages/staged_pages 在此表示独立物理分配数：最近提交视图支撑集合为 resident，本窗口持有且不在 resident 集合内的分配为 staged，包含所有已到达的未完成/预取块。published_slots 仍按页名称的有效槽位求和。资源 pages 为请求预留、scratch 加全部请求/回执所持分配的全局并集；不同请求 resident 相加不代表池占用。错误、取消、成功终态、迟到帧和回执遵守既有一次释放规则。
