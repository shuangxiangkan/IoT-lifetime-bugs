# FreeRTOS-Kernel lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：FreeRTOS-Kernel
- commit：`49cec3e9b27e517ac5ea5db5482c59f937e6aea4`
- commit 日期：2026-06-22
- 扫描日期：2026-07-01
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：249 个文件、1,593 个函数、3 个候选
- 包含 tests：250 个文件、1,595 个函数、3 个候选
- 解析警告：0

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/FreeRTOS-Kernel
python cli.py lifetime ../IoT-repos/FreeRTOS-Kernel --include-tests
```

默认扫描和包含测试的扫描得到完全相同的三条结果：

| 类型 | 文件 | 函数 |
|---|---|---|
| `memory_not_freed` | `portable/Renesas/SH2A_FPU/port.c` | `xPortUsesFloatingPoint()` |
| `memory_not_freed` | `portable/ThirdParty/GCC/Posix/port.c` | `prvMarkAsFreeRTOSThread()` |
| `memory_not_freed` | `stream_buffer.c` | `vStreamBufferDelete()` |

人工复查结论：

1. Renesas SH2A FPU port 存在高可信的 task-associated buffer 泄漏。
2. POSIX port 在 `pthread_setspecific()` 失败时存在条件性泄漏。
3. `stream_buffer.c` 是预处理指令造成的分析器误报。

通用 task、queue、list 和 stream buffer 内核逻辑中，本轮没有确认到其他
lifetime bug。

## 2. 高可信问题：Renesas SH2A FPU task context buffer 没有析构

### 2.1 位置

文件：

```text
portable/Renesas/SH2A_FPU/port.c
```

函数：

```c
xPortUsesFloatingPoint()
```

关键位置：

- 249 行：分配 FPU register save buffer
- 254—258 行：初始化 buffer 和 FPSCR
- 262 行：把 buffer 尾部地址写入 task application tag
- 270 行：函数返回，不再持有原始基址

相关 task 删除代码：

```text
tasks.c:6484  prvDeleteTCB()
tasks.c:6489  portCLEAN_UP_TCB(pxTCB)
```

### 2.2 分配和所有权转移

当某个 task 需要使用浮点寄存器时，该 port 为它动态分配保存区：

```c
pulFlopBuffer =
    ( uint32_t * ) pvPortMalloc( portFLOP_STORAGE_SIZE );
```

其中：

```c
#define portFLOP_REGISTERS_TO_STORE    ( 18 )
#define portFLOP_STORAGE_SIZE          ( portFLOP_REGISTERS_TO_STORE * 4 )
```

因此每次分配 72 字节。

分配成功后，代码没有保存原始基址，而是把指向 buffer 末端的地址写入 task
application tag：

```c
vTaskSetApplicationTaskTag(
    xTask,
    ( void * )
        ( pulFlopBuffer + portFLOP_REGISTERS_TO_STORE ) );
```

使用末端地址是有意设计：汇编保存函数采用 pre-decrement，在写入每个寄存器
前先递减指针。

port 的 context switch hook 随后从 task tag 读取该地址：

```c
#define traceTASK_SWITCHED_OUT() \
    do { \
        if( pxCurrentTCB->pxTaskTag != NULL ) \
            vPortSaveFlopRegisters( pxCurrentTCB->pxTaskTag ); \
    } while( 0 )

#define traceTASK_SWITCHED_IN() \
    do { \
        if( pxCurrentTCB->pxTaskTag != NULL ) \
            vPortRestoreFlopRegisters( pxCurrentTCB->pxTaskTag ); \
    } while( 0 )
```

因此成功返回后的 buffer 不是函数内泄漏：它已经转移给 task。但是这种转移
要求 task 删除时存在对应析构。

### 2.3 Task 删除路径缺少清理

FreeRTOS 删除 TCB 时调用：

```c
portCLEAN_UP_TCB( pxTCB );
```

该 hook 正是供 port 清理 task-specific 内存使用的。随后内核只释放：

```text
task stack
TCB
C runtime TLS block（如启用）
```

Renesas SH2A FPU 的 `portmacro.h` 没有定义 `portCLEAN_UP_TCB`，所以使用默认
空实现：

```c
#ifndef portCLEAN_UP_TCB
    #define portCLEAN_UP_TCB( pxTCB )    ( void ) ( pxTCB )
#endif
```

内核也不会把 application task tag 当成普通内存释放，因为在其他 port 中它
本来可以保存用户 callback。

最终路径为：

```text
pvPortMalloc(72)
  -> task->pxTaskTag = allocation_end
  -> vTaskDelete(task)
  -> portCLEAN_UP_TCB does nothing
  -> stack and TCB freed
  -> original allocation becomes unreachable
```

因此，只要使用浮点上下文的 task 被删除，就会泄漏 72 字节。

### 2.4 重复调用造成即时泄漏

该函数没有检查 task 是否已经拥有 FPU buffer：

```c
xPortUsesFloatingPoint( xTask );
xPortUsesFloatingPoint( xTask );
```

第二次调用会：

1. 再次分配 72 字节；
2. 用新地址覆盖 `pxTaskTag`；
3. 丢失旧 buffer 的唯一地址。

这条路径不需要删除 task 即可泄漏。

函数名和接口说明暗示调用者通常只调用一次，但当前实现没有返回
`already initialized`，也没有释放或复用旧 buffer。即使把重复调用视为调用者
误用，task 删除路径仍然是独立且正常可达的问题。

### 2.5 影响

影响范围：

- 仅影响 `portable/Renesas/SH2A_FPU`；
- 需要启用动态分配；
- task 必须调用 `xPortUsesFloatingPoint()`；
- 删除 task 或重复初始化后出现泄漏。

单次泄漏为 72 字节。在反复创建、启用 FPU、运行并删除 worker task 的系统中，
泄漏会稳定累积，最终可能造成 `pvPortMalloc()` 失败。

嵌入式系统通常没有进程退出级别的统一内存回收，因此即使单次泄漏较小，也会
持续到设备重启。

### 2.6 建议修复

推荐在该 port 中定义 `portCLEAN_UP_TCB`，从保存的末端指针恢复原始地址后
释放：

```c
#define portCLEAN_UP_TCB( pxTCB )                                  \
    do                                                              \
    {                                                               \
        if( ( pxTCB )->pxTaskTag != NULL )                          \
        {                                                           \
            uint32_t * pulBufferEnd =                                \
                ( uint32_t * ) ( pxTCB )->pxTaskTag;                 \
            vPortFree( pulBufferEnd - portFLOP_REGISTERS_TO_STORE ); \
            ( pxTCB )->pxTaskTag = NULL;                             \
        }                                                           \
    } while( 0 )
```

实际补丁需注意：

- `portFLOP_REGISTERS_TO_STORE` 当前定义在 `port.c`，如果宏在
  `portmacro.h` 中实现，需要移动或共享该常量；
- `configUSE_APPLICATION_TASK_TAG` 和 port 对 `pxTaskTag` 的使用方式需要保持
  一致；
- cleanup 只应释放由这个 port 的 FPU 初始化函数创建的 tag；
- 应防止用户通过 `vTaskSetApplicationTaskTag()` 覆盖该字段。

一个结构更清晰的方案是不要复用 application task tag，而是在 port-specific
TCB 扩展字段中保存原始 allocation base。

对于重复调用，可以在分配前检查：

```c
if( xTaskGetApplicationTaskTag( xTask ) != NULL )
{
    return pdPASS;
}
```

但该检查需要确认 task tag 完全由该 port 独占，否则非空 tag 不一定代表已经
初始化 FPU context。

### 2.7 建议测试

#### Task 删除测试

1. 记录 heap free bytes。
2. 创建一个动态 task。
3. 对该 task 调用 `xPortUsesFloatingPoint()`。
4. 删除 task，并让 idle task 完成延迟 TCB 清理。
5. 重复多轮。

修复前每轮应减少约 72 字节；修复后 free bytes 应回到基线。

#### 重复调用测试

对同一个 task 连续调用两次：

```c
TEST_ASSERT_EQUAL( pdPASS, xPortUsesFloatingPoint( task ) );
TEST_ASSERT_EQUAL( pdPASS, xPortUsesFloatingPoint( task ) );
```

验证没有产生第二个不可达 allocation。

#### Context switch 回归测试

修复析构逻辑后还应验证：

- task 存活期间 FPU register save/restore 不受影响；
- 删除非 FPU task 不调用 `vPortFree()`；
- 删除 FPU task 只释放一次；
- self-delete 和由其他 task 删除两种路径都正确。

## 3. 条件性问题：POSIX port 忽略 `pthread_setspecific()` 失败

### 3.1 位置

文件：

```text
portable/ThirdParty/GCC/Posix/port.c
```

函数：

```c
prvMarkAsFreeRTOSThread()
```

关键位置：

- 134—137 行：TLS destructor
- 142 行：创建 pthread key
- 154 行：分配一字节 thread marker
- 159 行：设置 thread-specific value，但忽略返回值

### 3.2 正常路径不是泄漏

代码为每个 FreeRTOS pthread 分配一字节 marker：

```c
pucThreadData = malloc( 1 );
*pucThreadData = 1;
pthread_setspecific( xThreadKey, pucThreadData );
```

pthread key 注册了 destructor：

```c
static void prvThreadKeyDestructor( void * pvData )
{
    free( pvData );
}
```

因此，在 `pthread_setspecific()` 成功后，marker 的所有权转移给 pthread TLS；
线程退出或被取消时由 destructor 释放。扫描器只看到当前函数内没有 `free()`，
所以对正常路径的报告是跨 API 所有权转移误报。

### 3.3 失败路径会泄漏

POSIX `pthread_setspecific()` 返回非零错误码表示设置失败。当前代码：

```c
pthread_setspecific( xThreadKey, pucThreadData );
```

没有检查返回值。

如果设置失败：

- pointer 没有保存到 thread-specific storage；
- TLS destructor 不会收到该 pointer；
- 当前函数返回后局部变量消失；
- 一字节 allocation 泄漏；
- `prvIsFreeRTOSThread()` 也会错误地认为当前线程不是 FreeRTOS thread。

常见错误为：

```text
EINVAL：key 无效
ENOMEM：没有足够资源保存 thread-specific value
```

### 3.4 严重程度

该问题的 lifetime 影响较低：

- 每次失败只泄漏一字节及 allocator metadata；
- 正常初始化路径不会泄漏；
- `pthread_setspecific()` 失败通常表示 host 系统已经处于异常或内存紧张状态。

但 marker 设置失败还会破坏 port 的线程身份判断，因此其正确性影响大于单纯的
一字节泄漏。

### 3.5 建议修复

```c
int xResult;

xResult = pthread_setspecific( xThreadKey, pucThreadData );
if( xResult != 0 )
{
    free( pucThreadData );
    configASSERT( xResult == 0 );
}
```

因为当前函数返回 `void`，若 `configASSERT` 被禁用，需要定义失败后的策略：

- 终止当前 pthread；
- 调用 port-specific fatal error handler；
- 或把函数改成返回状态并让调用者停止初始化。

仅释放并继续运行会避免泄漏，但线程仍未被标记，后续控制流可能不正确。

### 3.6 相关健壮性问题

当前代码同样依赖：

```c
configASSERT( pucThreadData != NULL );
```

随后无条件执行：

```c
*pucThreadData = 1;
```

当 `configASSERT` 在 release 配置中为空且 `malloc()` 失败时，会发生空指针
解引用。这不是 lifetime bug，但建议和 `pthread_setspecific()` 返回值检查一起
修复。

`pthread_key_create()` 的返回值也被忽略；如果 key 创建失败，后续
`pthread_setspecific()` 会进一步失败。完整修复应同时保存并检查 key 初始化
状态。

### 3.7 建议测试

用 wrapper 或 link-time substitution 让 `pthread_setspecific()` 固定返回
`ENOMEM`，验证：

- `pucThreadData` 被释放；
- 不会继续把线程当成成功标记；
- 没有 double free；
- 正常路径在线程退出时仍由 destructor 释放一次。

## 4. 明确误报：`vStreamBufferDelete()`

### 4.1 位置

文件：

```text
stream_buffer.c
```

函数：

```c
vStreamBufferDelete()
```

扫描结果声称：

```text
resource variable: configSUPPORT_DYNAMIC_ALLOCATION
acquire API: pvPortMalloc
exit line: 599
```

代码中并没有在该函数分配资源。相关内容是预处理条件：

```c
#if ( configSUPPORT_DYNAMIC_ALLOCATION == 1 )
{
    vPortFree( ( void * ) pxStreamBuffer );
}
#endif
```

动态创建的 stream buffer 将 structure 和 backing buffer 放在同一次
`pvPortMalloc()` allocation 中，所以这里只需要一次：

```c
vPortFree( pxStreamBuffer );
```

静态创建的 stream buffer 则不能释放，代码通过清零结构使后续误用触发断言：

```c
memset( pxStreamBuffer, 0x00, sizeof( StreamBuffer_t ) );
```

两条路径都符合设计。

### 4.2 分析器根因

候选把预处理宏 `configSUPPORT_DYNAMIC_ALLOCATION` 当成资源变量，同时把其他
上下文中的 `pvPortMalloc` provenance 错配到删除函数。这说明分析器在处理：

```c
#if ( MACRO == 1 )
```

以及条件编译后的 acquire/release provenance 时仍有边界问题。

这条候选不应提交给 FreeRTOS-Kernel。

## 5. 结论和优先级

建议复查和提交顺序：

1. Renesas SH2A FPU task context buffer：高可信、可重复累积、具有清晰的正常
   task 删除触发路径。
2. POSIX `pthread_setspecific()`：真实但低概率，适合作为健壮性修复，并应和
   `malloc()`、`pthread_key_create()` 的错误检查一起处理。
3. `vStreamBufferDelete()`：明确误报，不应提交。

如果只选择一个问题提交，优先选择 Renesas SH2A FPU port。它的核心证据链
完整：

```text
明确分配
  -> 地址存入 TCB task tag
  -> port 独占该字段用于 FPU context
  -> task 删除会调用 cleanup hook
  -> 此 port 未实现 hook
  -> stack/TCB 删除后 allocation 不可达
```

该问题也非常适合作为论文中的典型案例：单函数扫描只能看到“资源逃逸到对象”，
必须继续结合对象析构协议才能区分合法所有权转移和跨生命周期泄漏。
