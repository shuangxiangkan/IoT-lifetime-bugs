# Zephyr lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Zephyr
- commit：`a80b84df51aa6373e774ac67607e1ded623ea06b`
- commit 日期：2026-06-30
- 扫描日期：2026-07-01
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：5,434 个文件、54,208 个函数、15 个候选
- 默认排除：3,955 个 test/sample/doc 文件
- 解析警告：0
- 单轮耗时：约 6 分钟

默认复现命令：

```bash
cd IoT-lifetime-bugs
python cli.py lifetime ../IoT-repos/zephyr
```

Zephyr 的正式源码主要使用原生资源 API，而不是 POSIX `malloc/free`：

```c
k_malloc()
k_calloc()
k_realloc()
k_aligned_alloc()
k_free()
net_buf_alloc()
net_buf_alloc_len()
net_buf_get()
net_buf_unref()
```

因此，本次又补充了一份通用 Zephyr 资源规格：

```json
{
  "platform": "zephyr",
  "resources": [
    {
      "kind": "zephyr_heap_memory",
      "leak_type": "memory_not_freed",
      "acquire": [
        "k_malloc",
        "k_calloc",
        "k_realloc",
        "k_aligned_alloc"
      ],
      "acquire_result": "return",
      "success": "non_null",
      "release": ["k_free"],
      "release_arg": 0
    },
    {
      "kind": "zephyr_net_buf",
      "leak_type": "packet_buffer_not_freed",
      "acquire": [
        "net_buf_alloc",
        "net_buf_alloc_len",
        "net_buf_get"
      ],
      "acquire_result": "return",
      "success": "non_null",
      "release": ["net_buf_unref"],
      "release_arg": 0
    }
  ]
}
```

补充规格后的复现命令：

```bash
python cli.py lifetime ../IoT-repos/zephyr \
  --api-specs iot/api_specs/posix.json \
  --api-specs /tmp/zephyr-api-spec.json
```

第二轮结果：

| 类型 | 数量 |
|---|---:|
| `packet_buffer_not_freed` | 120 |
| `memory_not_freed` | 36 |
| `acquire_in_loop_without_release` | 19 |
| `use_after_release` | 6 |
| `owned_overwrite` | 5 |
| `lock_not_released_on_path` | 2 |
| 合计 | 188 |

人工复查确认三组高可信问题：

1. Cadence NAND 跨多页读取在正常成功路径和部分分配失败路径泄漏 page buffer。
2. Settings management 的 read/write/delete handler 在多条 callback/OOM 路径泄漏。
3. GICv3 ITS 在 MAPD command 失败时泄漏 Interrupt Translation Table。

## 2. 高可信问题一：Cadence NAND 多页读取泄漏两个 page buffer

### 2.1 位置

文件：

```text
drivers/flash/flash_cadence_nand_ll.c
```

函数：

```c
cdns_nand_read()
```

关键位置：

- 1297 行：进入首尾均不对齐且跨越两个以上 page 的分支
- 1298 行：分配 `first_end_page`
- 1299 行：分配 `last_end_page`
- 1303—1305 行：任一分配失败后直接返回
- 1307—1328 行：读取首、尾和中间 page
- 1330—1333 行：复制首尾数据
- 1336 行：成功返回，未释放两个 page buffer

### 2.2 触发条件

目标分支为：

```c
else if ((check_page_last == 0) &&
         (check_page_first == 0) &&
         (page_count > 2))
```

含义是：

- 起始 offset 不在 page 边界；
- 结束位置不在 page 边界；
- 读取范围覆盖三个或更多 NAND page。

函数需要分别读取第一个和最后一个不完整 page，因此分配两个临时 buffer：

```c
first_end_page =
    (char *)k_malloc(params->page_size);
last_end_page =
    (char *)k_malloc(params->page_size);
```

### 2.3 正常成功路径双重泄漏

三个 `cdns_read_data()` 调用全部成功后，代码把临时 page 内容复制到调用者
buffer：

```c
memcpy((char *)buffer,
       first_end_page + r_bytes,
       bytes_dif);

memcpy((char *)buffer + ...,
       last_end_page,
       lp_bytes_dif);
```

随后离开分支并直接：

```c
return 0;
```

缺少：

```c
k_free(first_end_page);
k_free(last_end_page);
```

因此该问题不依赖 OOM 或硬件失败。每次满足上述读取形状且读取成功，都会泄漏
两个 page-sized allocation。

如果 NAND page size 为 2 KiB 或 4 KiB，一次调用可能泄漏 4 KiB 或 8 KiB。
对于资源受限设备，这是明显且快速累积的内存损失。

### 2.4 部分分配失败也会泄漏

当前检查：

```c
if ((first_end_page != NULL) &&
    (last_end_page != NULL)) {
    ...
} else {
    LOG_ERR(...);
    return -ENOSR;
}
```

如果：

```text
first_end_page != NULL
last_end_page == NULL
```

会泄漏 `first_end_page`。

反过来：

```text
first_end_page == NULL
last_end_page != NULL
```

会泄漏 `last_end_page`。

这是典型的多个独立 allocation 使用组合式判空、失败后没有回滚已成功部分的
问题。

### 2.5 其他错误路径

三个 NAND read 失败分支均正确释放两个 buffer：

```c
if (ret != 0) {
    k_free(first_end_page);
    k_free(last_end_page);
    return ret;
}
```

这进一步说明正常成功路径和初始分配失败路径中的遗漏不是有意所有权转移。
临时 buffer 没有保存到 driver state，也没有返回给调用者。

### 2.6 建议修复

最小修复：

```c
first_end_page = k_malloc(params->page_size);
last_end_page = k_malloc(params->page_size);

if ((first_end_page == NULL) || (last_end_page == NULL)) {
    k_free(first_end_page);
    k_free(last_end_page);
    LOG_ERR("Memory allocation error occurred %s", __func__);
    return -ENOSR;
}

...

memcpy(...);
memcpy(...);

k_free(last_end_page);
k_free(first_end_page);
```

更稳妥的方式是统一 cleanup：

```c
ret = ...;
if (ret != 0) {
    goto free_end_pages;
}

...

free_end_pages:
    k_free(last_end_page);
    k_free(first_end_page);
    return ret;
```

`k_free(NULL)` 在 Zephyr 中允许使用，因此可以减少分支。

### 2.7 建议测试

#### 成功路径

构造：

```text
offset % page_size != 0
(offset + size) % page_size != 0
page_count > 2
```

让全部 `cdns_read_data()` 返回成功。重复调用并检查 heap free bytes，修复前应
每轮减少约 `2 * page_size`。

#### 部分 OOM

分别让第一次和第二次 `k_malloc()` 失败，验证另一个已成功 allocation 被释放。

#### 读取失败

分别让三个 `cdns_read_data()` 调用失败，验证每条路径释放两块内存且没有
double free。

## 3. 高可信问题二：Settings management 多条路径泄漏 heap buffer

### 3.1 位置和配置条件

文件：

```text
subsys/mgmt/mcumgr/grp/settings_mgmt/src/settings_mgmt.c
```

受影响函数：

```text
settings_mgmt_read()
settings_mgmt_write()
settings_mgmt_delete()
```

这些问题只在以下配置启用时存在：

```c
CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
```

非 heap 配置使用栈上固定数组，不受这些泄漏影响。

### 3.2 `settings_mgmt_read()`：两个 allocation 的部分失败清理不完整

位置：

- 78 行：分配 `key_name`
- 79 行：分配 `data`
- 81—87 行：组合判空

代码：

```c
key_name = (char *)k_malloc(key.len + 1);
data = (uint8_t *)k_malloc(max_size);

if (data == NULL || key_name == NULL) {
    if (key_name != NULL) {
        k_free(key_name);
    }

    return MGMT_ERR_ENOMEM;
}
```

当：

```text
key_name == NULL
data != NULL
```

代码不会释放 `data`，随后直接返回。

建议无条件释放两者：

```c
if ((data == NULL) || (key_name == NULL)) {
    k_free(key_name);
    k_free(data);
    return MGMT_ERR_ENOMEM;
}
```

### 3.3 `settings_mgmt_read()`：access callback 直接返回泄漏两个 buffer

位置：

- 104—106 行：调用 `mgmt_callback_notify()`
- 108—111 行：`MGMT_CB_ERROR_RC` 直接返回
- 144—148 行：正常统一清理

当前代码：

```c
if (status != MGMT_CB_OK) {
    if (status == MGMT_CB_ERROR_RC) {
        return ret_rc;
    }

    ...
    goto end;
}
```

`goto end` 会释放：

```c
k_free(key_name);
k_free(data);
```

但 `return ret_rc` 绕过 `end:`，因此同时泄漏两个 allocation。

### 3.4 `settings_mgmt_write()`：access callback 直接返回泄漏 key

位置：

- 194 行：分配 `key_name`
- 217—219 行：调用 callback
- 221—224 行：`MGMT_CB_ERROR_RC` 直接返回
- 248—250 行：正常清理

错误路径：

```c
if (status == MGMT_CB_ERROR_RC) {
    return ret_rc;
}
```

此时 `key_name` 已分配，但返回绕过：

```c
end:
    k_free(key_name);
```

### 3.5 `settings_mgmt_delete()`：callback 任一非成功结果都可能泄漏 key

位置：

- 295 行：分配 `key_name`
- 316—318 行：调用 callback
- 320—326 行：处理 callback 错误
- 333—334 行：仅正常业务路径释放
- 351 行：`end:` label 本身没有清理

代码：

```c
if (status != MGMT_CB_OK) {
    if (status == MGMT_CB_ERROR_RC) {
        return ret_rc;
    }

    ok = smp_add_cmd_err(...);
    goto end;
}
```

两种路径都有问题：

- `return ret_rc` 直接泄漏；
- `goto end` 也泄漏，因为 `end:` 位于 `k_free(key_name)` 之后。

只有继续执行到：

```c
rc = settings_delete(key_name);
k_free(key_name);
```

的正常业务路径会释放。

### 3.6 同文件中的正确对照

`settings_mgmt_save()` 对相同 callback 状态做了显式清理：

```c
if (status == MGMT_CB_ERROR_RC) {
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
    k_free(key_name);
#endif
    return ret_rc;
}
```

这说明 callback 返回后不会接管 `key_name`，read/write/delete 的缺失属于清理
不一致，而不是有意所有权转移。

### 3.7 影响

远程 management client 可以触发 settings handler。是否能够稳定触发 callback
拒绝取决于应用注册的 access hook，但该路径属于正常、受支持的访问控制行为，
并非硬件故障或极低概率 OOM。

如果应用使用 access hook 持续拒绝某些 key：

- read 每次可能泄漏 key buffer 和 value buffer；
- write/delete 每次泄漏 key buffer；
- 未授权请求可以逐步消耗 kernel heap。

因此，这组问题可能形成远程可触发的内存耗尽条件，严重程度高于普通初始化
错误路径泄漏。

### 3.8 建议修复

三个函数都应让所有已分配 buffer 走统一的 `end:` cleanup。

例如：

```c
if (status == MGMT_CB_ERROR_RC) {
    rc = ret_rc;
    goto end;
}
```

但需要注意现有返回值变量和 `MGMT_RETURN_CHECK(ok)` 的语义。更直接且低风险的
补丁是在 direct return 前释放。

对于 delete，建议移动 cleanup 到 `end:`：

```c
end:
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
    k_free(key_name);
#endif
    return MGMT_RETURN_CHECK(ok);
```

并删除正常路径中原有的提前 `k_free(key_name)`，防止 double free。

### 3.9 建议测试

覆盖以下矩阵：

| Handler | 注入条件 | 期望 |
|---|---|---|
| read | key allocation 失败 | data 不泄漏 |
| read | data allocation 失败 | key 不泄漏 |
| read | callback 返回 `MGMT_CB_ERROR_RC` | key/data 都释放 |
| read | callback 返回其他错误 | key/data 都释放 |
| write | callback 返回 `MGMT_CB_ERROR_RC` | key 释放 |
| write | callback 返回其他错误 | key 释放 |
| delete | callback 返回 `MGMT_CB_ERROR_RC` | key 释放 |
| delete | callback 返回其他错误 | key 释放 |
| 三者 | callback 成功 | 不发生 double free |

建议结合 heap listener、ztest allocator fault injection 或替换 `k_malloc/k_free`
进行精确计数。

## 4. 高可信问题三：GICv3 ITS MAPD 失败泄漏 ITT

### 4.1 位置

文件：

```text
drivers/interrupt_controller/intc_gicv3_its.c
```

函数：

```c
gicv3_its_init_device_id()
```

关键位置：

- 577 行：分配 Interrupt Translation Table `itt`
- 581—584 行：初始化并刷新 cache
- 587 行：发送 MAPD command，把 ITT 映射给设备 ID
- 588—590 行：MAPD 失败后直接返回

### 4.2 错误路径

```c
itt = k_aligned_alloc(256, alloc_size);
if (!itt) {
    return -ENOMEM;
}

memset(itt, 0, alloc_size);

ret = its_send_mapd_cmd(
    data,
    device_id,
    fls_z(nr_ites) - 2,
    (uintptr_t)itt,
    true);

if (ret) {
    LOG_ERR("Failed to map device id %x ITT table", device_id);
    return ret;
}
```

当 MAPD command 失败：

- `itt` 已成功分配；
- command 没有成功把表映射给 GIC ITS；
- driver state 没有保存 `itt`；
- 函数直接返回并丢失唯一 pointer。

因此该 allocation 不属于成功的硬件所有权转移，而是错误路径泄漏。

### 4.3 影响

ITT 大小为：

```c
nr_ites = MAX(2, nites);
alloc_size = ROUND_UP(nr_ites * entry_size, 256);
```

至少分配 256 字节，并随 interrupt vector 数增长。MAPD command 失败通常表示
ITS command queue 或硬件状态异常，未必可由普通应用触发；但初始化重试可能
重复泄漏 aligned heap。

### 4.4 建议修复

```c
if (ret) {
    LOG_ERR("Failed to map device id %x ITT table", device_id);
    k_free(itt);
    return ret;
}
```

修复前需确认 `its_send_mapd_cmd()` 返回错误时 command 不可能稍后异步生效。
从当前 API 的同步返回语义看，失败后 driver 没有记录 ITT 地址，因此保留该
allocation 也无法用于后续 teardown。

### 4.5 建议测试

stub `its_send_mapd_cmd()` 返回错误，验证：

- `k_free(itt)` 被调用一次；
- 错误码原样返回；
- 非 coherent DMA 配置下，cache flush 不影响释放；
- 成功路径不提前释放仍由硬件使用的 ITT。

## 5. 代表性误报

### 5.1 Bluetooth 和网络 `net_buf`

120 条 `packet_buffer_not_freed` 大多来自：

```text
Bluetooth HCI command/event queue
connection TX queue
advertising work
mesh friend queue
USB network class
network packet pipeline
```

`net_buf` 使用引用计数。常见正常路径为：

```text
net_buf_alloc()
  -> enqueue/send/store in connection
  -> consumer completes asynchronously
  -> net_buf_unref()
```

单函数内没有 `net_buf_unref()` 不等于泄漏。必须结合被调 API 的成功/失败
ownership contract，当前不把这些候选列为确认问题。

### 5.2 Driver probe 私有数据

例如 `dai_ssp_probe()`：

```c
ssp = k_calloc(1, sizeof(*ssp));
dai_set_drvdata(dp, ssp);
```

成功后 private data 由 device 持有，`dai_ssp_remove()` 会：

```c
k_free(dai_get_drvdata(dp));
dai_set_drvdata(dp, NULL);
```

这是跨对象生命周期的正常所有权转移。

### 5.3 POSIX 返回资源

以下 API 的成功结果按协议返回给调用者：

```text
if_nameindex()
zsock_getaddrinfo()
sem_open()
shm_open()
pthread_setspecific()
```

对应资源由 `if_freenameindex()`、`zsock_freeaddrinfo()`、`sem_close/unlink`、
fd close/shm unlink 或 pthread key cleanup 管理。单函数扫描会产生返回所有权
误报。

### 5.4 TEE shared memory

`tee_add_shm()` 成功时把 `struct tee_shm *` 写入 `*shmp`；失败路径会释放
descriptor，并在 `TEE_SHM_ALLOC` 时释放 backing memory。该候选不是泄漏。

### 5.5 Realtek task wrapper

`rtos_task_create()` 分配 `struct k_thread` 和 stack 后，把 handle 返回给
调用者。`rtos_task_delete()` 或 deferred delete work 会释放二者。扫描器无法
完整表达 self-delete 的异步清理。

## 6. 工具覆盖边界

本轮没有运行 `--include-tests`，原因是：

- 默认扫描已经覆盖 5,434 个正式文件；
- 额外 3,955 个文件主要是 tests/samples/docs；
- 单轮约 6 分钟；
- 当前目标是寻找正式源码中的候选，而不是评估测试代码质量。

本轮规格仍未完整覆盖：

```text
k_heap_alloc()/k_heap_free()
sys_heap_*
net_pkt refcount
device runtime PM references
kernel object init/uninit typestate
work item cancellation
Bluetooth connection references
```

因此，188 条候选不是 Zephyr 全部 lifetime bug 的上界，未报告某类问题也不代表
该资源族已经证明安全。

## 7. 结论和提交优先级

建议按以下顺序验证和提交：

1. Cadence NAND：正常成功路径稳定泄漏两个 page buffer，证据最直接、影响最
   容易量化。
2. Settings management：多条清理遗漏形成同一 bug family，且 access hook
   拒绝可能由远程 management 请求重复触发。
3. GICv3 ITS：错误路径明确，但依赖较少见的硬件 command 失败。

如果只选择一个作为 paper 案例，Cadence NAND 最适合展示路径敏感资源分析：

```text
两个 allocation
  -> 组合判空
  -> 部分成功时需要 rollback
  -> 三个读取错误分支均正确释放
  -> 唯独正常成功路径遗漏析构
```

Settings management 则适合展示大模型验证的价值：多个相似 handler 和同文件
中的正确对照实现，可以帮助判断 callback 是否接管 ownership，并确认这是一组
真实的清理不一致，而不是单函数误报。
