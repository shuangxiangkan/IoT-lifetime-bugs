# IoT-lifetime-bugs

## 目的

`IoT-lifetime-bugs` 是一个面向 IoT 固件、设备 SDK 和边缘网关 C/C++ 代码的
**资源生命周期静态分析工具**。它针对的是那些在普通功能测试中不易暴露、却会被
断网、超时、重试、重连等网络事件反复触发，最终导致内存或句柄耗尽、网络服务退化、
设备失联的资源管理缺陷。

它的定位是一个**通用的静态预筛选层**：不编译、不运行目标程序，只做轻量、路径敏感的
扫描，输出候选缺陷（JSON），交给开发者或大模型做进一步确认与定性。因此它追求“广撒网、
低噪声、对任意 C/C++ 代码通用”——不为任何具体项目定制，把需要深层语义或人工判断的
复杂情形留给下游确认。

## 支持的功能

工具在函数内做路径敏感的数据流分析，覆盖两大类生命周期缺陷。资源 API（申请/释放、
协议状态机）由数据驱动的 JSON 规格描述，新增平台或库主要是增改 JSON，不动分析引擎。

**1. 资源生命周期**（资源遵循“获取 → 使用 → 释放”模型）：

- `<resource>_not_released_on_path` —— 资源在某条真实路径上到达函数出口仍未释放
  （按资源类型命名，如内存、文件、文件描述符、socket、数据包缓冲、队列、任务、定时器…）
- `double_release` —— 同一资源在某路径上被释放两次
- `use_after_release` —— 释放后又被使用（传给其它调用，或经 `->`/`[]` 解引用）
- `lock_not_released_on_path` —— 锁在某条退出路径上未释放
- `acquire_in_loop_without_release` —— 循环内反复申请、循环体内无释放（重连耗尽型）

**2. 协议顺序（typestate）**（对象需按状态机顺序使用，如 init → start → stop → destroy）：

- `invalid_protocol_transition` —— API 用在对象不合法的协议状态上（如未连接就发送、
  销毁后再用、未初始化就使用）。引擎通用、不绑定任何具体库，默认不带协议规格，由用户
  按需喂入。

为降低误报，分析器会自动识别常见的“非泄漏”写法并豁免：所有权逃逸（存入字段/全局/
出参）、返回给调用方、申请失败分支（`p == NULL` / fd `< 0`）、`if (p) { ... 释放 ... }`
守卫，以及项目自定义的释放包装函数和所有权接管函数（均从代码结构自动推断，无需配置）。

尚未覆盖、刻意留给下游（大模型/人工）的复杂情形见 [TODO.md](TODO.md)，包括引用计数语义、
跨函数所有权、条件所有权转移与并发/中断生命周期。

## 安装与运行

```bash
pip install -r requirements.txt
```

安装 `tree-sitter`、`tree-sitter-c`、`tree-sitter-cpp`。

```bash
# 默认加载 iot/api_specs/ 下所有平台规格（POSIX、lwIP、FreeRTOS）
python IoT-lifetime-bugs/cli.py lifetime path/to/project > iot_findings.json

# 只用指定平台规格
python IoT-lifetime-bugs/cli.py lifetime path/to/project \
    --api-specs IoT-lifetime-bugs/iot/api_specs/lwip.json
```

子命令可省略：`python IoT-lifetime-bugs/cli.py path/to/project` 等价。
输出为 JSON，含 `findings`、按类型/置信度的 `summary`、加载的 `platforms`
和 `warnings`。

## 核心思想

IoT 程序会同时管理多种有限资源，例如：

- 堆内存和固定大小内存池；
- socket、MQTT client 和网络连接；
- lwIP `pbuf` 等数据包缓冲区；
- FreeRTOS/Zephyr 的 mutex、semaphore、queue 和 task；
- UART、DMA、GPIO 等驱动及硬件句柄。

这些资源通常遵循“获取—使用—释放”或更复杂的状态协议：

```text
acquire/init
    ↓
active
    ├── release/destroy → released
    ├── return/store    → ownership escaped
    └── error path      → resource may remain active
```

如果某条真实控制流路径到达函数出口时，资源仍由当前函数持有，分析器将其报告
为潜在泄漏。分析器还会检查重复释放、释放后使用、循环内持续申请、错误的锁释放，
以及违反 API 状态顺序等问题。

与一般资源泄漏检测不同，本项目尤其关注网络事件对缺陷的放大作用：

```text
packet loss / timeout / disconnect
                ↓
       retry or reconnect path
                ↓
       one resource leaked each time
                ↓
 memory pool, socket or task exhaustion
                ↓
 latency increase, reconnect failure or device outage
```

一次只泄漏少量资源的错误，在长期运行的 IoT 设备上可能被重连循环执行数千次，
因此最终表现为网络可靠性和可用性问题，而不只是局部内存错误。

## 总体架构

项目复用 `py-cext-bugs` 和 `jni-lifetime-bugs` 已有的通用 C/C++ 静态分析框架，
同时使用独立的 IoT 资源语义层：

```text
C/C++ source discovery
          ↓
Tree-sitter parsing and function extraction
          ↓
intraprocedural control-flow graph (CFG)
          ↓
forward, path-sensitive data-flow analysis
          ↓
IoT API resource and protocol semantics
          ↓
JSON candidate findings
```

计划中的目录结构如下：

```text
IoT-lifetime-bugs/
├── analysis/                 # 通用 C/C++ 解析、CFG 和数据流
├── iot/
│   ├── resource_state.py     # 资源状态与路径合并
│   ├── resource_transfer.py  # acquire/release/handoff 规则
│   ├── protocol_state.py     # API typestate/调用顺序
│   ├── analyzer.py           # 分析调度与结果生成
│   └── api_specs/            # 各 IoT 平台的数据驱动 API 规格
├── tests/
└── cli.py
```

其中 `analysis/` 与具体 API 无关，可以复用现有的源码发现、Tree-sitter 解析、
函数内 CFG 和前向数据流求解器。`iot/` 只负责 IoT 领域语义，避免把 POSIX、
lwIP、FreeRTOS 或厂商 SDK 的规则写死在通用分析层中。

## 数据驱动的 API 语义

不同 IoT 平台使用不同的资源 API。项目将使用 JSON 规格描述资源类型、申请函数、
释放函数、参数位置、成功条件和所有权转移规则。例如：

```json
{
  "resource": "lwip_pbuf",
  "acquire": {
    "api": "pbuf_alloc",
    "result": "return",
    "success": "non_null"
  },
  "release": {
    "api": "pbuf_free",
    "resource_arg": 0
  }
}
```

这样，扩展一个新平台主要是增加或修正 API 规格，而不需要修改 CFG 和数据流引擎。
第一阶段计划覆盖 POSIX socket、lwIP、FreeRTOS、Zephyr 和 ESP-IDF/MQTT。

## 资源状态与路径分析

每个被跟踪的资源具有如下抽象状态：

```text
declared
active
released
escaped
unknown
mixed
```

- `active`：资源已经成功获取，当前作用域负责释放；
- `released`：资源已经释放；
- `escaped`：资源通过返回值、字段、全局变量或输出参数转移到其他作用域；
- `mixed`：不同 CFG 路径上的状态不同，例如一条路径释放、另一条路径仍然持有。

分析器沿 CFG 传播这些状态，并在分支合流处进行保守合并。例如：

```c
struct pbuf *p = pbuf_alloc(...);
if (send_packet(p) < 0)
    return -1;                  // active 到达出口
pbuf_free(p);
return 0;
```

分析结果应指出具体资源、申请位置、未清理的出口路径和对应 API，而不只报告一次
文本模式匹配。

## 协议状态分析

一些 IoT 对象不能用简单的 acquire/release 配对描述。例如，一个网络 client
可能要求：

```text
uninitialized → initialized → started → stopped → destroyed
```

因此项目还计划支持 typestate 分析，用状态机描述合法 API 转移，检测：

- 未停止 client 就销毁；
- 尚未初始化就启动或发送；
- 已销毁对象再次使用；
- 重复初始化但没有释放旧资源；
- 错误路径破坏协议状态。

资源分析回答“有没有释放”，协议状态分析回答“是否以正确顺序使用”。

## 预期检查项

第一阶段聚焦函数内、路径敏感且能够较可靠识别的问题：

- `resource_not_released_on_path`
- `missing_cleanup_on_error_path`
- `double_release`
- `use_after_release`
- `acquire_in_loop_without_release`
- `lock_not_released_on_path`
- `packet_buffer_not_freed`
- `socket_not_closed`
- `network_client_not_destroyed`
- `invalid_protocol_transition`

后续再逐步加入函数摘要、有限函数间传播、编译配置感知和网络事件上下文分析。

## 研究目标

本项目不仅希望回答“代码中是否存在资源泄漏”，还希望研究：

1. 生命周期缺陷在真实 IoT 软件中主要影响哪些资源和执行路径；
2. 断网、丢包、超时和重连如何放大这些缺陷；
3. 缺陷如何进一步影响内存占用、连接成功率、恢复时间、时延、能耗和设备可用性；
4. 统一的资源规格能否以较低成本适配不同 IoT 平台和 SDK。

最终将通过真实开源项目扫描、人工复核、缺陷报告，以及真实设备上的网络故障实验
评估分析精度、覆盖范围、运行开销和网络系统影响。

## 边界

`IoT-lifetime-bugs` 输出的是值得进一步检查的候选缺陷，不是完整的正确性证明。
复杂宏、函数指针、跨任务所有权、完整 C++ RAII、项目自定义封装和并发执行仍可能
需要更深的函数间分析、动态实验或人工判断。
