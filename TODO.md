# IoT-lifetime-bugs TODO

记录这个项目「做了什么」和「接下来怎么做」，便于查看。

---

## 已完成（第一阶段骨架）

### D1 复用通用分析层 `analysis/`
直接从 `jni-lifetime-bugs/analysis/` 拷贝 `sources.py` / `parsing.py` /
`controlflow.py` / `dataflow.py` / `README.md`，**零逻辑改动**。这一层在
py-cext → jni 之间已被证明可复用（两者仅差注释文字），到 IoT 是第三次套用同
一个模子。它只提供结构化 C/C++ 信息（源码发现、Tree-sitter 解析、函数内 CFG、
前向数据流求解器），不含任何 API 规则。

### D2 数据驱动的 IoT 语义层 `iot/`
不照搬 jni 把资源类别写死在代码里的做法，改成 **JSON 规格 + 索引**，新增平台
只改 JSON、不动引擎：

- `iot/semantics.py`：加载 `iot/api_specs/*.json`，建 acquire/release/lock 四张
  名字→规格索引。`ResourceSpec` 描述「申请 API、申请结果在返回值还是出参、成功
  条件 non_null / non_negative、释放 API、资源参数下标、泄漏 finding 类型」；
  `LockSpec` 描述 take/give 配对。
- `iot/api_specs/`：已带三个平台规格
  - `posix.json`：heap(malloc/free)、file_stream(fopen/fclose)、
    file_descriptor(open/close)、socket(socket·accept/close)、pthread_mutex 锁。
  - `lwip.json`：pbuf、netbuf、tcp_pcb、lwip_socket。
  - `freertos.json`：queue、task、timer、信号量锁。
- `iot/resource_state.py`：不可变资源状态格
  （active / released / escaped / declared / mixed），含 CFG 汇合点的保守合并。
  在 jni 版基础上加了 `get()` 和 `maybe_released`，供 double-release 检查用。
  申请位置/API 与最近一次状态转移位置/API 分开保存，释放操作不会覆盖申请来源。
- `iot/calls.py`：**通用 C 调用识别**（IoT 是普通 C 函数调用，没有 jni 的
  `(*env)->Foo(env, ...)` 间接层）。`find_c_calls()` 抓 `ident(args)` 并做括号
  平衡切参；`simple_name()` / `arg_at()` 会剥掉一层前导 `&`，以支持
  `pthread_mutex_destroy(&m)`、`pbuf_free(p)` 两种写法。排除 if/for/while/sizeof
  等关键字和成员调用。
- `iot/resource_transfer.py`：把 jni `lifetime_transfer.py` 的**路径敏感所有权
  模型**移植过来并改造（注意：jni 的 TODO.md 把 escape/return 跟踪写成「未修」，
  但其实代码里已实现，我是从已实现的好版本移植的）。改造点：
  - 用 `IoTSemantics` 查表替代 jni 的固定类别分支；
  - 支持申请结果在**出参**（`acquire_result: "arg"`），不只返回值；
  - release 索引允许同一 API 对应多个资源规格，并按当前资源 kind 验证配对；
    例如 `close` 可以关闭 fd/socket，但不能错误地释放 `FILE *`；
  - 出参申请和 lock take 支持 zero/nonzero/non-negative/non-NULL 成功约定，
    可精化直接调用条件和保存到变量后的返回状态检查；
  - 新增 **fd 负值返回**失败分支精化：`if (fd < 0) return;` 的失败分支把 fd 标
    released（jni 只有 NULL 检查；socket/open 返回 -1 而非 NULL，必须单独处理）；
  - 锁的 take/give（类比 jni monitor）；
  - 新增**路径敏感 double-release**：在每个 release 节点看 in-state，目标在所有
    路径上都已 released 才报（mixed 不报，避免误报）。
- `iot/analyzer.py`：调度 + `acquire_in_loop_without_release` 循环检查（类比 jni
  loop-local，按 byte span 扣掉嵌套循环体避免重复归因）。沿用 jni 的
  producer 隔离错误处理：单个检查抛错记进 warnings、打 stderr，不影响其它检查。
  输出里多带一个 `platforms` 字段。
- `cli.py`：`lifetime` 子命令，`--api-specs` 可指定平台规格，子命令可省略。

### D3 测试与 demo
- `tests/demo_iot_cases.c`：18 个函数，覆盖 pbuf/socket/malloc 泄漏
  vs ok、逃逸存字段、返回、全局缓存、循环泄漏 vs 循环内释放、double free、锁泄漏
  vs 锁释放）。
- `tests/test_iot_analyzer.py`：35 个测试，覆盖各 finding 类型的真阳性 + 关键负样本
  （ok/escape/return/cache/failed-acquire 不报），并覆盖错误 release API、共享
  `close`、trylock 失败、出参成功状态、acquire provenance，以及自定义释放包装
  抑制误报 / reader 不被误当释放。包装测试还覆盖条件释放、cast/alias、五层包装、
  跨文件传播和同名 `static` 函数隔离。**全部通过**。

### D4 自定义释放包装识别（`iot/wrappers.py`）—— 真实代码验证驱动
背景：在真实 C 代码（snowball、py-lmdb）上跑后，**头号误报源是项目自定义的释放
包装函数**：项目把 `free`/`close` 包成自己的函数（`SN_delete_env(z)`→`free(z)`、
`lose_s(p)`→`free(p)`），分析器不认识这些包装，就在每条经过它清理的路径上误报泄漏。
例如 snowball `SN_new_env` 的错误路径 `if (z->p==NULL){ SN_delete_env(z); return NULL; }`
被误报 `memory_not_freed`。
做法：`discover_release_wrappers()` **从代码自身结构推断**包装，零配置——若函数 F
把它的第 i 个参数转发给已知 release API（在该 API 的资源参数位），并且该参数在
**所有可达函数出口路径**上都已释放，才把 F 登记为「在第 i 参数位释放对应 kind」。
推断复用 CFG/前向数据流，支持 cast 和简单局部 alias，并运行到真正的不动点以处理
任意有限层包装套包装。文件内 `static` wrapper 以 `(file, name)` 隔离，不会污染其它
翻译单元的同名函数。只读参数的函数（`fread`/`fprintf`）以及仅在条件分支释放的函数
不会被误当成无条件 release wrapper。
接线：`analyze_path` 改成两遍——第一遍解析所有文件并缓存函数（单次解析），跨文件
收集所有函数做项目级包装识别，`semantics.with_release_wrappers()` 增广后第二遍跑检查；
`analyze_file` 单文件时退化为文件内识别。输出的 `release_wrappers` 统计唯一 wrapper
函数，`release_wrapper_specs` 统计按参数和资源 kind 展开的规则数。
验证：snowball 上 `SN_new_env` 误报消失（7→6），发现 3 个包装；`get_input` 真实 fopen
泄漏仍报（reader 没被过度抑制）；py-lmdb 发现 12 个包装、无误造。加了
`demo_free_ctx`/`demo_wrapper_release_ok`/`demo_read_all`/`demo_reader_is_not_release`
四个 demo + 2 个测试锁定。

### D5 P10 所有权 sink + P11 循环精度 + P12 排除 test + 内联判空（真实 IoT 验证驱动）
基于 6 个真实 IoT 仓库验证（见下）一次性做掉三个真实 FP 源 + 一个顺手修：

- **P10 所有权 sink（`iot/wrappers.py` 的 `discover_ownership_sinks`，D4 的对偶）**：
  结构化推断「把指针参数存进字段/全局/链表的函数」=所有权 sink。若函数 F 的第 i 个
  指针参数在体内逃逸（`x->f = p` 等结构化左值，或转发给另一个已知 sink，如
  `ListAppend(list, p)`），就登记 (F, i) 为 sink；调用 sink 时把对应实参标记 escaped，
  不再算泄漏。CFG 前向数据流 + 别名 + **must-escape**（所有出口均转移）+ fixpoint。
  对局部链表节点/容器会跟踪“参数被装入容器，容器随后逃逸”的传播；条件存储和仅存入
  局部数组不会被提升成无条件 sink。static 函数按 `(file)` 作用域隔离。
  `SinkSpec` 加进 `semantics`，经 `augmented()` 注入。条件所有权转移（例如仅在返回值
  表示成功时接管）仍需后续关系摘要，当前不会用不安全的 may-escape 强行压掉 finding。
- **P11 循环检查精度（`analyzer.py`）**：acquire 的 LHS 会区分长期 escape 存储和
  局部 aggregate。全局/参数支持的 `pcb[i]=` 会跳过，局部数组 `pcb[i]=` 仍报告并将
  variable 记为 `pcb[i]`，不再错记成 `i`。
- **P12 排除 test/doc（`analyzer.py`，不动共享 `analysis/`）**：默认跳过
  `test/ tests/ doc/ examples/ samples/ demos/` 等目录（判断相对扫描根，故直接指文件/单测
  demo 不受影响），`--include-tests` 可关。`--max-files` 在过滤后计数，避免测试文件
  吃掉生产代码配额。输出加 `excluded_test_files`。
- **顺手：内联赋值判空精化（`resource_transfer.py`）**：`if ((conn = malloc(sizeof(T))) ==
  NULL)` / `if ((fd = open(...)) < 0)` 用平衡括号扫描把 `(var = expr)` 归约成 `var`，让
  失败分支精化生效；`==`/`!=` 比较不受影响，括号扫描会忽略字符串/字符常量内的括号。

初版 may-escape 的扫描曾得到 **paho.mqtt.c 17** 个 finding，但会隐藏条件存储的真实
泄漏，已撤销该不安全策略。must-escape 加固后当前 Paho 为 40 个候选、10 个 sink；
增加的候选主要需要条件返回值/所有权关系摘要进一步区分。测试现为 35 个，新增条件
sink、局部容器、局部/全局数组、过滤后 max-files 和字符串括号回归。`analysis/`
仍与 jni 字节一致。

### D6 P13 链式判空归属 + P15 裸真值/退出泄漏边精化（残留 triage 驱动）
继续逐条 triage 真实仓库残留，修掉两个真实 FP：

- **P13 链式赋值变量归属（`resource_transfer._reduce_inline_assign`）**：
  `if ((ptr = buf = malloc(2)) == NULL)`（paho `MQTTPacket_send_ack`）。内联判空归约器
  原取首个 assignee `ptr`，但资源被追踪为最内层 `buf`。新增 `_last_chained_assignee()`
  沿 `a = b = ... = expr` 链取最靠近 expr 的 assignee，与 acquire 解析一致。
- **P15 裸真值 + 退出泄漏边精化（关键 correctness bug）**：`p = pbuf_alloc(); if (p) {
  ...; pbuf_free(p); }` 被误报。两处问题：①`_null_check` 不认裸真值 `if (p)`——补上
  `^name$` → 失败边为 false（p 为 NULL，受 `_is_non_null_resource` 守卫，fd==0 合法不误放）；
  ②**根因**：`_find_exit_leaks` 对“有边到 exit 的节点”直接看未精化的 `out_state`，**没应用
  边精化**——`if(p)` 的 false→exit 边本应释放 p，却被当成 active 泄漏。改为对 exit 入边
  调用 `refine_resource_edge` 后再判 active。这是影响所有 `if(res){...free...}` 习语的基础
  bug，wireguard-lwip 4 个 pbuf FP 全因它而来。

验证（D6 后）：**wireguard-lwip 4→0、paho 40→37、libcoap 0、lwip 1、mqttclient 1、
wolfMQTT 0**。35 测试仍全绿，`analysis/` 仍与 jni 字节一致。

triage 还发现两个更复杂的低频 FP（各 1 个，见 D7 已修）。

### D7 P16 链式逃逸到全局 + P17 cast 别名返回（收尾非 paho 仓库到零）
把 triage 发现的最后两个非 paho FP 修掉（均在 `resource_transfer.py`）：

- **P16 链式赋值逃逸（`_escaped_assignment`）**：`fe = malloc(); first = last = fe;`
  （lwip `register_filename`）把节点存入全局链表 = 逃逸。原 `_escaped_assignment` 只认单个
  `=`，改为按 `=` 全拆，最后一段是资源、前面任一 assignee 是逃逸左值（全局/字段/出参）就
  标 escaped。单赋值行为不变。
- **P17 值 cast 别名（`_strip_value_cast` + `_assigned_name`/`_escaped_assignment`）**：
  `ret = (int)fd; return ret;`（mqttclient `platform_net_socket_connect`）把 fd 经值类型
  cast 拷给 `ret` 再返回。`strip_casts` 只去指针 cast，故新增 `_strip_value_cast` 去掉前导
  `(type)` 值 cast，让 alias 穿透 → `return ret` 认出 fd 被返回。

验证（D7 后）：**libcoap 0、lwip 0、mqttclient 0、wireguard 0、wolfMQTT 0、paho 37**。
6 个真实仓库里 **5 个零误报**。测试 39→41，新增链式逃逸、cast 别名返回两条回归。
`analysis/` 仍与 jni 字节一致。paho 剩 37 全部需要条件所有权关系摘要，属独立一档功能。

### D8 P2 typestate 协议顺序检查（README 承诺的第二阶段主线，已落地）
新增 `invalid_protocol_transition` 检查：API 用在对象不合法的协议状态上（未 connect 就
publish、destroy 后再用、未 init 就用等）。**数据驱动 + 路径敏感 + 保守**：

- 规格：api_specs JSON 新增 `protocols` 段——`kind`、`create`（API + 结果在返回值/出参）、
  `initial_state`、`transitions`（每条 `{api, arg, from:[states], to}`）。`semantics.py` 加
  `ProtocolSpec`/`Transition` + create/transition 两张索引，`augmented()` 保留 protocols。
- 引擎：`iot/protocol_state.py`（var→状态串，合流冲突退 `UNKNOWN`）+ `iot/protocol.py`
  （复用 `analyze_forward` 前向数据流跑状态转移，后置 pass 用 in-state 判非法）。只在对象
  状态**确属该协议**（在该协议状态集内）且**不在合法 from 集**时才报；未跟踪/UNKNOWN 一律
  不报——粗筛低误报。非法转移后把状态置 UNKNOWN，避免级联重复报。
- 接线：`iot/analyzer.py` 加 `_protocol_findings` producer（与其它检查同样隔离错误）。
- **通用、不绑定任何库**：引擎默认**不带**任何库专属协议，休眠到用户用 `--api-specs`
  喂入规格才生效（`load_iot_semantics()` 默认 `protocols == ()`，有断言锁定）。规格示例放在
  测试夹具 `tests/demo_protocol_spec.json`（Paho `MQTTClient` 生命周期，仅作演示/测试，
  不进 bundled 默认）。`arg_at` 剥前导 `&`，故 `create(&c)`/`destroy(&c)` 与 `connect(c)`
  统一成同一对象 `c`。

> 注：曾一度把 Paho 规格 bundled 进 `iot/api_specs/mqtt.json`，但那是为特定 repo 量身定制、
> 违反“通用功能”原则，已撤下移成测试夹具。bundled 默认只保留通用平台资源规格
> （posix/lwip/freertos）。

验证：demo `publish-before-connect`、`use-after-destroy` 正确报，正确顺序/reconnect/未跟踪
参数不报；默认扫描（无协议规格）协议 finding 恒为 0。测试 41→44，新增 `demo_protocol_cases.c`
+ 协议断言 + “默认不带协议”断言。`analysis/` 仍与 jni 字节一致。真实平台/库的协议规格全部
留作用户按需 JSON 补充，引擎不变。

### D9 P3 use-after-release（释放后使用）—— 通用，补全 lifetime 网
新增 `use_after_release`：已释放的资源又被使用。**复用资源数据流已有的 `RELEASED` 状态**，
全自动、无需任何规格——后置 pass（`_find_use_after_release`）：某节点入口处资源为
definite `RELEASED`，且在该节点被「传给非释放调用」或「`->`/`[]` 解引用」即报。
- 再次释放算 `double_release`（不重复报 UAF）；`p=NULL`/`p=malloc()` 重新激活后不报；
  `&p` 取址（常是 re-init 出参）不算 use；`if(p==NULL)` 这种纯比较不报。
- **根因修复（关键）**：真实仓库验证一上来 UAF 误报 libcoap 5/mqttclient 4/wolfMQTT 3，
  全是 `rc`/`ret`/`q` 这类**从未 acquire 的借用指针/返回码**。根因：P15 裸真值 `if(q)`
  的失败分支精化把 `declared` 占位也「释放」了（`_is_non_null_resource` 对 declared 返回
  True）。修法：`ResourceState.release` 对 `DECLARED_KIND` 占位**空操作**——从未 acquire
  的东西不该被释放。真资源的 `if(p){...free(p);}` 释放不受影响（wireguard 仍 0）。
验证：6 仓库 UAF 误报全清零（5/6 零误报，UAF=0——成熟仓库本就干净）；demo
free/close/deref 后使用都报，重激活/取址/纯比较/借用指针 `if(q)` 均不报。测试 44→50，新增
6 条 UAF 回归（含借用指针不误判这条根因锁定）。`analysis/` 仍与 jni 字节一致。

### D10 数据流收敛性 bug（merge 不动点震荡）—— 鲁棒性根治
真实扫描里 paho `MQTTClient_run`（94 节点）数据流**永不收敛**（20 万次迭代仍失败），被
producer 隔离接住记 warning、该函数跳过分析。定位：MIXED 资源的**记账字段 `transition_api`**
在 `free`↔`failed-acquire`↔`None` 间随循环回边来回翻 → Resource 永不相等 → 无不动点。
修法（`resource_state._merge_resource`）：①合流产出 MIXED 时**丢弃 per-transition 溯源**
（`transition_line/api`、saved-status 字段），它在 join 点本就无意义；②base 选取确定化
（最小 acquire line）；③`alternatives` **展开嵌套 MIXED**（只含具体状态，永不含字面
`"mixed"`），使合并幂等。MQTTClient_run 现 693 次收敛，warning 消失。
副作用（正确性提升）：③修掉了 `"mixed"` 混入 alternatives 导致 `maybe_active` 误判 False 的
**掩盖 bug**——之前被掩盖的少量候选重新浮现（mqttclient 0→1、paho 38→40）。这是**减少漏报**，
对粗筛召回是好事。mqttclient 那 1 个是 `ret=(int)fd;break;...return ret`（alias 在循环+break
合流处被丢弃）的 corner-case FP，留给大模型（见 P17 备注：简单版已修，循环跨合流版属长尾）。
加 2 条 merge 确定性/幂等单元回归。测试 50→52。

---

## 真实 IoT 仓库初始验证快照（6 个，2026-06-27）

以下表格记录 D5 加固前的初始扫描，用于说明问题来源，不代表当前 finding 数量：

| 仓库 | files | funcs | wrappers | findings | 主要类型 |
|---|---|---|---|---|---|
| libcoap | 67 | 718 | 1 | 8 | file/memory（**全在 test/doc**）|
| lwip | 65 | 603 | 2 | 16 | pbuf/tcp_pcb（**全在 test/doc**）|
| mqttclient | 51 | 442 | 4 | 1 | socket |
| paho.mqtt.c | 59 | 1150 | 52 | 64 | memory(48)/loop(14) |
| wireguard-lwip | 11 | 136 | 1 | 4 | pbuf |
| wolfMQTT | 31 | 209 | 0 | 0 | — |

关键观察：
- **libcoap / lwip 的 finding 100% 落在 test/unit/doc/example**，真实 src 零噪声。
  说明真实网络栈要么干净，要么其所有权大多经回调/队列转移（我们看不到但也没误报）。
- **paho.mqtt.c 真实 src 有 45 个 `memory_not_freed`，经查全是同一类误报**（详见 P10）。
- 抽样精读确认了两个真实 FP 类（P10、P11）和一个噪声问题（P12）。

## 已知限制 / 待办（按优先级）

### P13 链式赋值的变量归属（真实代码暴露，paho 残留）
`MQTTPacket_send_ack`：`if ((ptr = buf = malloc(2)) == NULL)`。内联判空归约器取了
**首个** assignee `ptr`，但被跟踪的资源是 `buf`（malloc 紧邻的那个），于是失败分支精化
错变量、buf 在错误路径上仍 active → 误报。修法：归约器在链式赋值里取**最靠近 RHS 的**
assignee，或对 `a = b = expr` 把整链都映射到同一资源。改动局限在
`resource_transfer._reduce_inline_assign` + acquire 赋值变量解析。

### P16 链式赋值逃逸到全局/字段 —— 已修（见 D7）
### P17 资源值经 cast 拷给另一变量再返回 —— 已修（见 D7）

### P14 条件释放 / 中断转移所有权（真实代码，疑似 TP 与 FP 混合）
`MQTTPacket_send_ack`：`free(buf)` 只在 `MQTTPacket_send(...) != TCPSOCKET_INTERRUPTED`
分支执行——中断时 buf 是否泄漏取决于 socket 层是否接管，属歧义，可能是真阳性也可能是
所有权转移。这类需要更细的 per-API 语义或人工判断，先记录，不强行消除。

### P1 循环内资源被两个检查重复报告
现象：`demo_loop_leak` 里循环内申请、从不释放的 pbuf，会同时被
`acquire_in_loop_without_release`（循环检查）和 `packet_buffer_not_freed`
（退出泄漏数据流）各报一次，同一行同一变量。两条都是真阳性，但是同一个 bug 的
两种说法。jni 不会撞，因为 local ref 退出时自动释放、不进退出泄漏；IoT 资源不
自动释放，所以两个检查都合法触发。
修法（待定）：在 analyzer 跨 producer 去重——若某 (function, variable) 已被循环
检查覆盖，则压掉对应的退出泄漏 finding（保留信息量更大的循环检查那条，因为它点出
了「每次迭代泄漏 → 长期耗尽」的 IoT 语义）。需要一点跨 producer 协调，暂未做。

### P2 typestate / 协议状态分析 —— 已实现（见 D8）
引擎 + bundled Paho `MQTTClient` 协议已落地。剩余是**数据补全**：补更多真实平台/库的
协议规格（FreeRTOS task、socket connect/send/close、各 MQTT 库的 init/start/stop），
按需加 JSON 即可，引擎不变。

### P3 use_after_release（释放后使用）—— 已实现（见 D9）
没走 JSON-use-API-列表那条会要数据的路，而是直接复用资源数据流的 `RELEASED` 状态做通用
后置检查（传给调用 / `->`/`[]` 解引用）。全自动、零规格。

### P4 申请结果在出参的覆盖
引擎已支持 `acquire_result: "arg"` 及其调用返回状态精化，FreeRTOS 规格已覆盖
`xTaskCreate(..., &handle)`。其它真实场景如 `pthread_mutex_init(&m, ...)`、
`netconn`/`mbedtls_*_init` 等是出参/init 型，需要补规格 + 配对的 deinit，并加 demo
回归。注意 init/deinit 与 acquire/release 模型一致，剩余工作主要是数据补全。

### P5 realloc / 所有权转移类 API 建模偏弱
`realloc` 现在只当 acquire，没建模「成功时旧指针被释放、失败时旧指针仍有效」。
`tcp_close`/`tcp_abort` 之类也可能在内部转移所有权。这些细节先记下，按真实扫描中
暴露的误报再逐个 JSON 化或加专门规则。

### P6 网络事件上下文（研究目标，未落地）
README「研究目标」里的「断网/丢包/重连如何放大缺陷」目前只体现在
`acquire_in_loop_without_release` 的语义解释上，没有真正识别 retry/reconnect 路径
并加权。属于更后期的研究性工作，需要先有真实项目扫描数据支撑。

### P7 指针算术逃逸导致的误报（真实代码新暴露）
D4 修掉自定义包装后，snowball 上下一类误报是**资源通过指针算术逃逸**：
`mem=malloc(...); p=(symbol*)(HEAD+(char*)mem); return p;`（create_s）和
`*p=(symbol*)(HEAD+mem)`（increase_size）——返回/存出参的是 `mem` 的偏移指针 `p`，
而 `mem` 本身没被直接 return，于是误报 `memory_not_freed`。现有简单别名只认
`p = mem` 这种平凡赋值，认不出 `p = OFFSET + (char*)mem`。这是 container_of /
HEAD-offset 习语，偏小众，优先级低于包装；可考虑：赋值右侧若包含某个 active 资源
变量（即便夹在算术/cast 里），把 LHS 视为该资源的别名/逃逸。注意别过度宽松。

### P8 三元赋值的变量归属错误（精度）
`FILE *input = cond ? stdin : fopen(...)` 这类，`assigned_variable_for_call` 被
三元里的 `==`/`:` 干扰，把资源错记到 `filename` 而非 `input`（snowball get_input）。
泄漏信号大致还在（仍报对函数/行），但 `variable` 字段给错、且 stdin 分支本不该算
泄漏。属于 `iot/calls.py` 赋值左值解析的局限，按真实误报量决定是否单独加固。

### P9 status_variable 复用的精度风险（早先讨论，记录）
保存到变量的返回码（`int rc = trylock(); if(rc!=0)...`）若 `rc` 在资源仍 active
期间被复用于另一个无关 `if(rc!=0)`，理论上可能误放该资源。需 `rc` 被重新赋值才会
触发，属「保持基础」范围内可接受的取舍，暂不修，仅记录。

---

## 当前覆盖（通用粗筛器，给大模型喂候选）
资源生命周期：`<resource>_not_released_on_path`（memory/fd/socket/file/pbuf/netbuf/tcp_pcb/
queue/task/timer…）、`double_release`、`use_after_release`、`lock_not_released_on_path`、
`acquire_in_loop_without_release`；协议顺序：`invalid_protocol_transition`（数据驱动通用引擎）。
误报抑制全是结构化通用推断（逃逸/返回/sink/包装/裸真值/cast/declared 占位），不为任何 repo 定制。

真实 6 仓库现状：libcoap 0、lwip 0、mqttclient 1、wireguard 0、wolfMQTT 0、**paho 40**
（mqttclient 1 与 paho 比 D9 多出的 2 条，都是 D10 取消掩盖 bug 后浮现的候选，非新 FP 机制）。
52 个测试全绿，数据流对所有函数收敛、零 warning，`analysis/` 与 jni 字节一致。

## 下一步建议
lifetime 主网已基本完整，剩余都属「用户数据」或「独立立项」，不再是粗筛主线：
1. **协议规格 = 用户数据**：typestate 引擎通用，具体协议由使用者按需用 `--api-specs` 喂 JSON。
2. **paho 38 = 条件所有权**（must-escape sink 候选 + `freeData`/中断）：需 per-API/per-return
   的关系摘要，独立一档功能；或改回 may-escape 把噪声降到 ~17，靠大模型兜底。粗筛阶段可不做。
P5/P7（API 消费所有权如 `tcp_listen`、指针算术逃逸）、P14/P16-剩余 偏小众，按后续误报量再定。
其余真实 lifetime bug 类（引用计数、跨函数所有权、并发/ISR）刻意留给大模型，超出粗筛范畴。
