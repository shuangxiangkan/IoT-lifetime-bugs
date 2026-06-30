# ThreadX lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Eclipse ThreadX
- commit：`b91b03b9e75fa523b17127f9e0eca09dca916459`
- commit 日期：2026-06-08
- 扫描日期：2026-06-30
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：1,634 个文件、7,297 个函数、6 个候选
- 默认排除：227 个 test/doc/example 文件
- 解析警告：0

默认复现命令：

```bash
cd IoT-lifetime-bugs
python cli.py lifetime ../IoT-repos/threadx
```

ThreadX 主要使用自己的资源 API：

```c
tx_byte_allocate()
tx_byte_release()
tx_block_allocate()
tx_block_release()
tx_mutex_get()
tx_mutex_put()
tx_semaphore_get()
tx_semaphore_put()
```

这些 API 不在默认 POSIX、FreeRTOS 和 lwIP 规格中。因此，本次另外使用了一份
通用 ThreadX API 规格，把 byte/block pool 建模为内存资源，把 mutex 和
semaphore 建模为同步资源。

补充规格后产生 3,219 条原始候选：

| 类型 | 数量 |
|---|---:|
| `owned_overwrite` | 2,212 |
| `lock_not_released_on_path` | 702 |
| `double_release` | 221 |
| `use_after_release` | 62 |
| `memory_not_freed` | 22 |
| 合计 | 3,219 |

原始数量很大，但并不代表存在数千个不同问题。ThreadX 仓库为大量 CPU、
编译器和 SMP 组合保存了相似的 port 示例，例如：

```text
ports/*/example_build/sample_threadx.c
ports_smp/*/example_build/sample_threadx.c
ports_module/*/example_build/sample_threadx_module.c
```

同一份 sample 会在不同目录重复出现，并反复使用局部变量 `pointer`：

```c
tx_byte_allocate(&byte_pool_0, &pointer, stack_size, TX_NO_WAIT);
tx_thread_create(..., pointer, stack_size, ...);
```

分配结果已经作为线程栈转移给 RTOS 对象，随后覆盖 `pointer` 并不等于覆盖
仍由局部变量拥有的内存。因此，大部分 `owned_overwrite` 是重复的所有权转移
误报。

排除 `ports/`、`ports_smp/` 和 `ports_module/` 后剩余 199 条候选。进一步按
函数和根因复查后，确认：

1. POSIX compatibility `mq_send()` 发送失败时泄漏消息副本。
2. FreeRTOS compatibility `xQueueCreate()` 在 semaphore 创建失败时泄漏。
3. FreeRTOS compatibility `xQueueCreateSet()` 在 ThreadX queue 创建失败时泄漏。
4. 两个 module converter host 工具在循环中持续覆盖未释放的 `code_buffer`。

目前没有在 `common/` ThreadX 内核中确认到真实 lifetime bug。确认的问题都
位于 compatibility layer 或 host utility。

## 2. 高可信问题一：POSIX `mq_send()` 失败时泄漏消息缓冲区

### 2.1 位置

文件：

```text
utility/rtos_compatibility_layers/posix/px_mq_send.c
```

函数：

```c
mq_send()
```

关键位置：

- 164—165 行：从 queue byte pool 分配消息副本
- 179—184 行：把用户消息复制到分配的 buffer
- 201 行：调用 `tx_queue_send()`
- 202—209 行：发送失败后直接返回

### 2.2 所有权路径

POSIX message queue 不能直接保存调用者的 `msg_ptr`，因此先从 queue 自己的
byte pool 分配一份副本：

```c
temp1 = tx_byte_allocate(
    (TX_BYTE_POOL *)&q_ptr->vq_message_area,
    &bp,
    msg_len,
    TX_NO_WAIT);
```

随后把副本地址编码进 ThreadX queue message：

```c
source = save_ptr;
msg[0] = (ULONG)source;
...
temp1 = tx_queue_send(Queue, msg, TX_WAIT_FOREVER);
```

发送成功后，buffer 所有权转移给 queue。接收方取出消息后调用：

```c
tx_byte_release(msgbuf1);
```

因此成功路径没有泄漏。

### 2.3 错误路径

当 `tx_queue_send()` 失败时：

```c
if (temp1 != TX_SUCCESS)
{
    posix_errno = EINTR;
    posix_set_pthread_errno(EINTR);
    return(ERROR);
}
```

此时：

- `bp` 已成功分配；
- 消息未进入 queue；
- 接收方不会看到该指针，也无法释放；
- 发送方返回前没有调用 `tx_byte_release(bp)`。

因此每次失败都会永久消耗 `q_ptr->vq_message_area` 中的一块空间。

### 2.4 触发条件和影响

可能的 `tx_queue_send()` 失败包括 queue pointer/state 错误或等待被终止等。
该函数使用 `TX_WAIT_FOREVER`，正常队列满不会直接失败，但 ThreadX API 仍然
返回状态，现有代码也显式处理了非成功结果，因此不能假设此分支不可达。

这里使用的是每个 message queue 的有限 byte pool。重复触发后可能耗尽该
queue 的消息存储，即使 queue 本身已经恢复正常，后续 `mq_send()` 仍会因
无法分配消息副本而失败。

### 2.5 建议修复

在失败返回前释放尚未转移的消息副本：

```c
if (temp1 != TX_SUCCESS)
{
    tx_byte_release(bp);
    posix_errno = EINTR;
    posix_set_pthread_errno(EINTR);
    return(ERROR);
}
```

建议检查 `tx_byte_release()` 返回值；但即使 release 失败，也应优先保留原始
`tx_queue_send()` 错误语义。

### 2.6 建议验证

使用 stub 让：

```c
tx_byte_allocate() == TX_SUCCESS
tx_queue_send() != TX_SUCCESS
```

验证 `tx_byte_release()` 恰好被调用一次，且参数等于 `bp`。再循环触发失败，
确认 queue byte pool 的 available bytes 不再持续减少。

## 3. 高可信问题二：FreeRTOS `xQueueCreate()` 初始化失败泄漏

### 3.1 位置

文件：

```text
utility/rtos_compatibility_layers/FreeRTOS/tx_freertos.c
```

函数：

```c
xQueueCreate()
```

关键位置：

- 1520 行：分配 `p_queue`
- 1527 行：分配 queue backing memory `p_mem`
- 1544 行：创建 `read_sem`
- 1545—1546 行：第一次 semaphore 创建失败后直接返回
- 1549 行：创建 `write_sem`
- 1550—1551 行：第二次 semaphore 创建失败后直接返回

### 3.2 第一个失败分支

完成两次内存分配后：

```c
p_queue = txfr_malloc(sizeof(txfr_queue_t));
p_mem = txfr_malloc(mem_size);
```

代码创建读 semaphore：

```c
ret = tx_semaphore_create(&p_queue->read_sem, "", 0u);
if (ret != TX_SUCCESS) {
    return NULL;
}
```

该分支遗漏：

```c
txfr_free(p_mem);
txfr_free(p_queue);
```

### 3.3 第二个失败分支

`read_sem` 创建成功后，再创建 `write_sem`：

```c
ret = tx_semaphore_create(
    &p_queue->write_sem, "", uxQueueLength);
if (ret != TX_SUCCESS) {
    return NULL;
}
```

此时需要清理三项资源：

```text
read_sem
p_mem
p_queue
```

当前实现三项都没有清理。除了 byte pool 泄漏，还会留下一个不可访问的
ThreadX semaphore control block 状态。

### 3.4 为什么不是成功路径误报

成功时 `p_queue` 和 `p_mem` 被返回给调用者，并由 `vQueueDelete()` 负责：

```c
tx_semaphore_delete(&xQueue->read_sem);
tx_semaphore_delete(&xQueue->write_sem);
vPortFree(xQueue->p_mem);
vPortFree(xQueue);
```

错误分支没有返回 queue handle，因此调用者不可能调用 `vQueueDelete()` 完成
清理。这是确定的错误路径泄漏。

### 3.5 触发条件和影响

当 ThreadX 无法创建 read 或 write semaphore 时触发。可能原因包括：

- control block 状态异常；
- caller/context 不允许创建；
- ThreadX 配置或系统状态错误。

每次失败会泄漏 queue descriptor 和 queue backing storage。第二个分支还泄漏
已创建的 read semaphore。由于 backing storage 大小与
`uxQueueLength * uxItemSize` 成正比，泄漏量可能明显大于 queue descriptor。

### 3.6 建议修复

采用分层 cleanup：

```c
ret = tx_semaphore_create(&p_queue->read_sem, "", 0u);
if (ret != TX_SUCCESS) {
    txfr_free(p_mem);
    txfr_free(p_queue);
    return NULL;
}

ret = tx_semaphore_create(&p_queue->write_sem, "", uxQueueLength);
if (ret != TX_SUCCESS) {
    (void)tx_semaphore_delete(&p_queue->read_sem);
    txfr_free(p_mem);
    txfr_free(p_queue);
    return NULL;
}
```

也可以使用 cleanup labels，避免未来增加初始化步骤后再次遗漏。

### 3.7 建议验证

分别注入：

1. 第一次 `tx_semaphore_create()` 失败；
2. 第一次成功、第二次失败。

检查 byte pool 可用空间恢复，并验证第二种情况下
`tx_semaphore_delete(&p_queue->read_sem)` 被调用一次。

## 4. 高可信问题三：FreeRTOS `xQueueCreateSet()` 创建失败泄漏

### 4.1 位置

文件：

```text
utility/rtos_compatibility_layers/FreeRTOS/tx_freertos.c
```

函数：

```c
xQueueCreateSet()
```

关键位置：

- 2693 行：分配 queue-set descriptor `p_set`
- 2699 行：分配 queue backing memory `p_mem`
- 2705 行：调用 `tx_queue_create()`
- 2706—2708 行：创建失败后直接返回

### 4.2 错误路径

```c
p_set = txfr_malloc(sizeof(txfr_queueset_t));
p_mem = txfr_malloc(queue_size);

ret = tx_queue_create(
    &p_set->queue,
    "",
    sizeof(void *) / sizeof(UINT),
    p_mem,
    queue_size);

if (ret != TX_SUCCESS) {
    TX_FREERTOS_ASSERT_FAIL();
    return NULL;
}
```

当 `tx_queue_create()` 失败时，`p_set` 和 `p_mem` 都没有释放。

默认头文件中的定义为：

```c
#ifndef TX_FREERTOS_ASSERT_FAIL
#define TX_FREERTOS_ASSERT_FAIL()
#endif
```

配置模板默认同样为空宏。因此不能认为
`TX_FREERTOS_ASSERT_FAIL()` 一定会终止程序或回收进程资源；默认配置下代码会
继续执行并返回 `NULL`，两块内存永久丢失。

### 4.3 触发条件和影响

当 ThreadX queue 创建失败时触发。泄漏量包括：

```text
sizeof(txfr_queueset_t)
+ uxEventQueueLength * sizeof(void *)
```

攻击者通常不能直接控制底层 queue 创建失败，但在内存紧张或对象状态异常时，
该错误路径会进一步消耗 ThreadX byte pool。

### 4.4 建议修复

```c
if (ret != TX_SUCCESS) {
    TX_FREERTOS_ASSERT_FAIL();
    txfr_free(p_mem);
    txfr_free(p_set);
    return NULL;
}
```

若项目允许 `TX_FREERTOS_ASSERT_FAIL()` 被配置为不返回的死循环，cleanup 应放
在 assert 之前，否则该配置下仍无法释放：

```c
if (ret != TX_SUCCESS) {
    txfr_free(p_mem);
    txfr_free(p_set);
    TX_FREERTOS_ASSERT_FAIL();
    return NULL;
}
```

### 4.5 建议验证

让 `tx_queue_create()` 返回错误，确认：

- `txfr_free(p_mem)` 被调用；
- `txfr_free(p_set)` 被调用；
- 函数仍返回 `NULL`；
- 默认空 assert 和自定义 assert 两种配置均有明确行为。

## 5. 低严重度问题：module converter 循环覆盖 `code_buffer`

### 5.1 位置

文件一：

```text
common_modules/module_manager/utilities/module_to_binary.c
```

函数：

```c
main()
```

关键位置：

- 296 行：遍历 code sections
- 311 行：为当前 section 分配 `code_buffer`
- 336 行：结束本轮循环，未释放
- 342 行：程序退出

文件二：

```text
common_modules/module_manager/utilities/module_to_c_array.c
```

关键位置：

- 343 行：为当前 section 分配 `code_buffer`
- 393 行：结束本轮循环，未释放
- 402 行：程序退出

### 5.2 错误模式

两个工具采用相同模式：

```c
for (i = 0; i < code_section_index; i++)
{
    code_buffer = malloc(code_section_array[i].code_section_size);
    elf_object_read(..., code_buffer, ...);
    ...
}
```

每轮都会覆盖 `code_buffer`，但循环尾部没有：

```c
free(code_buffer);
```

因此输入 ELF 中每个 code section 都会保留一块分配，直到 converter 进程
退出。

### 5.3 严重程度

这是确定的内存泄漏，但它发生在 host-side module conversion utility，而不是
嵌入式 ThreadX runtime：

- 操作系统会在进程退出后回收内存；
- 工具通常只处理一个输入文件；
- 影响随 code section 数量和大小增长；
- 极端或恶意构造的 ELF 可能显著提高峰值内存。

因此建议标为低严重度，不应和 runtime byte-pool 泄漏放在同一优先级。

此外，两个位置都没有检查 `malloc()` 是否返回 `NULL`，OOM 时会把空指针传给
`elf_object_read()`。这是独立的健壮性问题，不计入本报告的 lifetime bug
数量。

### 5.4 建议修复

每轮使用完成后释放，并检查分配结果：

```c
code_buffer = malloc(code_section_array[i].code_section_size);
if (code_buffer == NULL) {
    /* close files and report failure */
    return 1;
}

...

free(code_buffer);
code_buffer = NULL;
```

### 5.5 建议验证

构造包含多个 code section 的 ELF，使用 ASan 或 Valgrind 运行两个 converter。
修复后应无 `code_buffer` definite leak。

## 6. 代表性误报

### 6.1 port 示例中的 `owned_overwrite`

`tx_application_define()` 通常用一个临时 `pointer` 连续分配：

```text
线程栈
queue storage
byte pool storage
block pool storage
```

每次分配后，地址立即传给相应 ThreadX object。对象在整个 demo 生命周期内
常驻，因此覆盖临时变量不是资源丢失。

同一示例被复制到大量 architecture/compiler 目录，导致一个错误模型生成数千
条重复候选。

### 6.2 semaphore 不等同于 mutex

本轮为了粗筛，把 `tx_semaphore_get()` 暂时建模为 acquire，把
`tx_semaphore_put()` 建模为 release。但 semaphore token 可以：

- 由另一个线程产生；
- 被当前函数消费而无需归还；
- 用于事件通知，而不是互斥保护。

因此 `lock_not_released_on_path` 只能作为路由信息，不能直接判定 bug。

### 6.3 POSIX lock wrapper

`pthread_mutex_lock()`、`pthread_mutex_trylock()` 等 wrapper 成功返回时，本来就
需要让调用者持有锁。wrapper 内没有 unlock 是正确 API 语义。

`pthread_cond_wait()` 还包含“释放 mutex、等待、返回前重新获取 mutex”的协议，
单纯的函数内 acquire/release 配对模型无法准确表达。

### 6.4 allocator wrapper 返回

以下函数的成功结果会返回给调用者：

```text
txfr_malloc()
posix_memory_allocate()
osek_memory_allocate()
tm_memory_pool_allocate()
```

它们是 allocator wrapper，不是泄漏点。

## 7. 结论和提交优先级

建议按以下顺序进一步验证或提交：

1. `xQueueCreate()`：两个清晰的初始化 rollback 缺失，且可能同时泄漏内存和
   semaphore 状态。
2. `mq_send()`：有限 queue byte pool 中的确定错误路径泄漏。
3. `xQueueCreateSet()`：默认空 assert 下确定泄漏两块内存。
4. 两个 module converter：代码问题明确，但只影响短生命周期 host utility。

ThreadX 的扫描也暴露了工具层面的两个改进点：

1. 默认 test/example 排除规则没有识别 `example_build`，导致大量 port 示例进入
   扫描。
2. out-parameter allocator 结果在传入 object create API 后，需要更精确地区分
   “成功转移所有权”和“初始化失败仍由本函数负责回滚”。

因此，本项目不适合直接使用原始 finding 数量评价精度。更合理的流程是先按
port 示例去重，再对 compatibility layer 中的短错误路径进行人工或大模型
验证。
