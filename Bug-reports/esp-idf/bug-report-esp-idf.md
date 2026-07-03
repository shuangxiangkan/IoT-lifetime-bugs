# ESP-IDF lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Espressif IoT Development Framework（ESP-IDF）
- commit：`fa8039b5cadb6e85dd830ff8c2c4bd73b6538aee`
- commit 日期：2026-06-23
- 扫描日期：2026-06-30
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：2,464 个文件、21,512 个函数、366 个候选
- 默认排除：1,251 个 test/doc/example 文件
- 解析警告：0

原始复现命令：

```bash
cd IoT-lifetime-bugs
python cli.py lifetime ../IoT-repos/esp-idf
```

本次直接扫描会在以下函数的数据流分析中超过 2,200 次迭代：

```text
components/wpa_supplicant/src/common/dragonfly.c
dragonfly_get_random_qr_qnr()
```

因此实际扫描使用了一个只排除 `dragonfly.c` 的临时硬链接镜像。扫描完成后
镜像已删除，ESP-IDF 源码没有被修改。这属于分析器的收敛性问题，而不是
ESP-IDF 的编译或运行错误。

候选分布：

| 类型 | 数量 |
|---|---:|
| `memory_not_freed` | 199 |
| `lock_not_released_on_path` | 45 |
| `use_after_release` | 43 |
| `acquire_in_loop_without_release` | 23 |
| `task_not_deleted` | 19 |
| `owned_overwrite` | 17 |
| `double_release` | 11 |
| `packet_buffer_not_freed` | 3 |
| `fd_not_closed` | 2 |
| `file_not_closed` | 2 |
| `socket_not_closed` | 1 |
| `queue_not_deleted` | 1 |
| 合计 | 366 |

人工复查确认了三处高可信错误路径泄漏：

1. Security1 握手加密失败时泄漏两个 protobuf response 对象。
2. HTTPS Server 在 TLS 握手成功、transport context 分配失败时泄漏 TLS 会话。
3. SDMMC 切换 DDR bus mode 失败时泄漏 DMA response buffer。

三处问题均位于正式组件源码，不依赖测试代码。

## 2. 高可信问题一：Security1 加密失败泄漏 response 对象

### 2.1 位置

文件：

```text
components/protocomm/src/security/security1.c
```

函数：

```c
handle_session_command1()
```

关键位置：

- 174 行：分配 `Sec1Payload *out`
- 175 行：分配 `SessionResp1 *out_resp`
- 187 行：分配加密输出 `outbuf`
- 196 行：调用 `psa_cipher_update()`
- 197—200 行：加密失败时只释放 `outbuf` 并返回

### 2.2 错误路径

代码先分配两个 response 对象：

```c
Sec1Payload *out = (Sec1Payload *) malloc(sizeof(Sec1Payload));
SessionResp1 *out_resp = (SessionResp1 *) malloc(sizeof(SessionResp1));
if (!out || !out_resp) {
    free(out);
    free(out_resp);
    return ESP_ERR_NO_MEM;
}
```

随后分配加密输出：

```c
uint8_t *outbuf = (uint8_t *) malloc(PUBLIC_KEY_LEN);
```

如果 `psa_cipher_update()` 失败：

```c
if (status != PSA_SUCCESS) {
    ESP_LOGE(TAG, "Failed at psa_cipher_update with error code : %d", status);
    free(outbuf);
    return ESP_FAIL;
}
```

此处释放了：

```text
outbuf
```

但没有释放：

```text
out
out_resp
```

由于两个对象尚未挂入 `resp`，上层清理函数也无法取得它们，函数返回后两个
地址永久丢失。

### 2.3 正常所有权路径

加密成功后代码建立以下所有权关系：

```text
resp->sec1
  -> Sec1Payload out
       -> SessionResp1 out_resp
            -> device_verify_data.data = outbuf
```

随后 `sec1_session_setup_cleanup()` 按相反顺序释放：

```c
free(out_resp1->device_verify_data.data);
free(out_resp1);
free(out);
```

错误发生在对象挂入 `resp` 之前，因此必须在
`handle_session_command1()` 本地完成清理。

### 2.4 触发条件和影响

触发条件：

1. Security1 握手进入 `Session_Command1`。
2. 三次堆分配均成功。
3. `psa_cipher_update()` 返回非 `PSA_SUCCESS`。

可能原因包括无效的 cipher 状态、底层 PSA driver 错误或硬件加密失败。
每次触发泄漏两个小型堆对象。攻击者如果可以反复建立并中断 provisioning
握手，可能逐步耗尽受限设备堆。

### 2.5 建议修复

在失败分支补齐本地对象清理：

```c
if (status != PSA_SUCCESS) {
    ESP_LOGE(TAG, "Failed at psa_cipher_update with error code : %d", status);
    free(outbuf);
    free(out_resp);
    free(out);
    return ESP_FAIL;
}
```

更稳妥的实现是统一跳转到一个 cleanup label，避免以后新增错误分支时再次
遗漏。

### 2.6 建议验证

给 PSA cipher driver 注入一次 `psa_cipher_update()` 失败，并在调用前后记录：

```c
heap_caps_get_free_size(MALLOC_CAP_8BIT)
```

重复执行握手，修复前可观察到可用堆持续下降；修复后应保持稳定。

## 3. 高可信问题二：HTTPS Server transport context 分配失败泄漏 TLS 会话

### 3.1 位置

文件：

```text
components/esp_https_server/src/https_server.c
```

函数：

```c
httpd_ssl_open()
```

关键位置：

- 179 行：`esp_tls_init()` 创建 TLS 对象
- 187 行：创建 server TLS session
- 196 行：为 HTTPD session 分配 `transport_ctx`
- 197—201 行：分配失败后直接返回
- 229—245 行：已有的 TLS 失败清理路径

### 3.2 错误路径

代码先创建 TLS 对象并完成 server handshake：

```c
esp_tls_t *tls = esp_tls_init();
int ret = esp_tls_server_session_create(global_ctx->tls_cfg, sockfd, tls);
```

然后分配用于把 TLS 对象绑定到 HTTPD session 的 context：

```c
httpd_ssl_transport_ctx_t *transport_ctx =
    (httpd_ssl_transport_ctx_t *)calloc(1, sizeof(httpd_ssl_transport_ctx_t));
```

如果该分配失败，当前代码直接返回：

```c
if (!transport_ctx) {
    ...
    return ESP_ERR_NO_MEM;
}
```

此时 `tls` 已经创建，TLS handshake 也已经完成，但：

- 没有调用 `esp_tls_server_session_delete(tls)`；
- 没有把 `tls` 注册到 HTTPD session；
- `httpd_ssl_close()` 因没有 transport context 而不会接管清理。

因此 TLS 对象及其内部握手资源发生泄漏。

### 3.3 正常和其他失败路径

成功时：

```c
transport_ctx->tls = tls;
httpd_sess_set_transport_ctx(server, sockfd, transport_ctx, httpd_ssl_close);
```

session 关闭后，`httpd_ssl_close()` 会执行：

```c
esp_tls_server_session_delete(tls);
free(ctx);
```

TLS handshake 自身失败时，函数也已经有统一的 `fail:` 清理：

```c
fail:
    ...
    esp_tls_server_session_delete(tls);
    return ESP_FAIL;
```

只有 transport context 的 OOM 分支绕过了这两套清理机制。

### 3.4 触发条件和影响

触发条件：

1. HTTPS client 完成或基本完成 server-side TLS handshake。
2. 紧接着分配一个较小的 `httpd_ssl_transport_ctx_t` 时发生 OOM。

虽然该窗口需要特定内存压力，但泄漏对象不只是一个小 context，而是已经
初始化的完整 TLS session。多个并发或重复连接可能扩大内存压力，形成
“OOM 导致泄漏、泄漏进一步加剧 OOM”的反馈循环。

### 3.5 建议修复

让该分支复用现有 `fail:` 清理：

```c
if (!transport_ctx) {
    esp_https_server_last_error_t last_error = {0};
    last_error.last_error = ESP_ERR_NO_MEM;
    http_dispatch_event_to_event_loop(
        HTTPS_SERVER_EVENT_ERROR, &last_error, sizeof(last_error));
    goto fail;
}
```

如果不希望 `fail:` 再次分发 TLS error event，也可以在直接返回前显式调用：

```c
esp_tls_server_session_delete(tls);
```

需要注意避免重复发送含义不同的 error event。

### 3.6 建议验证

使用 allocator fault injection，使该函数中的第二次分配，即
`calloc(sizeof(httpd_ssl_transport_ctx_t))` 失败。验证：

- `esp_tls_server_session_delete()` 被调用一次；
- session transport context 仍为 `NULL`；
- socket 后续由 HTTPD accept/session 错误路径关闭；
- ASan/heap tracing 不再报告 TLS 分配残留。

## 4. 高可信问题三：SDMMC DDR mode 切换失败泄漏 response buffer

### 4.1 位置

文件：

```text
components/sdmmc/sdmmc_sd.c
```

函数：

```c
sdmmc_enter_higher_speed_mode()
```

关键位置：

- 252 行：分配 DMA-capable `response`
- 258—277 行：执行 SD switch function
- 280 行：调用 host 的 `set_bus_ddr_mode`
- 281—284 行：失败时直接返回
- 322—324 行：统一 `out:` 清理

### 4.2 错误路径

函数为 CMD6 response 分配 DMA buffer：

```c
response = heap_caps_malloc(sizeof(*response), MALLOC_CAP_DMA);
```

大部分失败路径都跳转到：

```c
out:
    free(response);
    return err;
```

但 DDR50 分支中，host 切换 DDR mode 失败后直接返回：

```c
err = (*card->host.set_bus_ddr_mode)(card->host.slot, true);
if (err != ESP_OK) {
    ESP_LOGE(TAG, "%s: failed to switch bus to DDR mode (0x%x)", __func__, err);
    return err;
}
```

该 `return` 绕过 `out:`，导致 `response` 泄漏。

### 4.3 触发条件和影响

触发条件：

1. SD card 支持 SWITCH_FUNC 和 DDR50。
2. CMD6 切换 card-side DDR50 成功。
3. host driver 的 `set_bus_ddr_mode(slot, true)` 失败。

这是初始化错误路径，一次失败泄漏一个 `sdmmc_switch_func_rsp_t` DMA buffer。
DMA-capable 内存通常比普通堆更稀缺，因此即使单次泄漏不大，也会永久减少
后续外设可用的 DMA 内存。

此外，card-side mode 已经改变而 host-side mode 切换失败，调用方还需要关注
设备状态回滚；本报告只确认 lifetime 泄漏，不把协议状态问题计为已确认 bug。

### 4.4 建议修复

将直接返回改为统一清理：

```c
if (err != ESP_OK) {
    ESP_LOGE(TAG, "%s: failed to switch bus to DDR mode (0x%x)", __func__, err);
    goto out;
}
```

### 4.5 建议验证

构造一个 `set_bus_ddr_mode` stub，使其固定返回错误。验证：

- 返回值仍保持原错误码；
- `response` 被释放；
- 连续调用不会持续减少 `MALLOC_CAP_DMA` 可用空间。

## 5. 代表性误报和不应直接提交的问题

### 5.1 成功路径所有权转移

大量 `memory_not_freed` 是对象被写入输出参数、全局表、链表或异步队列：

```text
esp_console_cmd_register()
esp_event handler_instances_add()
各类 esp_*_new_*()
Bluetooth event queue post
```

成功返回后资源本来就应由调用方、设备对象或队列持有，不能因为函数内部没有
释放就判为泄漏。

### 5.2 `httpd_accept_conn()` 的 socket 候选

`accept()` 返回的新 fd 在失败路径通过 `close(new_fd)` 清理；成功路径交给
`httpd_sess_new()` 管理。因此 `socket_not_closed` 是跨函数所有权转移误报。

### 5.3 `transport_ctx` 成功路径候选

`httpd_ssl_open()` 成功返回时，`transport_ctx` 注册了 `httpd_ssl_close`
析构回调。报告中的真实问题是 `transport_ctx` 分配失败时泄漏此前创建的
`tls`，而不是成功路径上的 `transport_ctx` 泄漏。

### 5.4 FreeRTOS 任务、锁和队列

许多任务是系统常驻任务，锁获取函数与释放函数分布在调用者两侧；部分静态
队列也没有动态析构要求。仅凭单函数内没有 `vTaskDelete()`、
`xSemaphoreGive()` 或 `vQueueDelete()` 不能确认 bug。

### 5.5 Bluetooth 异步事件

Bluetooth adapter 的大量 `use_after_release`、`double_release` 和
`memory_not_freed` 候选来自事件对象 post 后的异步所有权转移及宏控制流。
需要结合 post API 的成功/失败契约和消费线程验证，当前不列为已确认问题。

### 5.6 PSRAM reserve DMA pool

`esp_psram_extram_reserve_dma_pool()` 在循环中分配内存后，通过
`heap_caps_add_region_with_caps()` 把区域注册成新 heap。成功路径属于有意的
所有权转移，不是循环泄漏。注册失败最终会在启动代码中 `abort()`，因此本轮
不把该候选作为可持续运行时泄漏提交。

## 6. 结论与复查优先级

建议按以下顺序验证和提交：

1. `sdmmc_enter_higher_speed_mode()`：控制流最简单，修复仅需把
   `return err` 改成 `goto out`。
2. `handle_session_command1()`：泄漏明确，适合用 PSA fault injection 验证。
3. `httpd_ssl_open()`：泄漏明确，但测试需要控制精确的 allocator 失败点，
   同时确认 error event 不会重复发送。

三个问题都是“资源成功创建后，后续步骤失败并提前返回，绕过既有清理”的
同一类 lifetime bug。这也说明粗筛工具在大型 IoT 框架中最有价值的输出，
不是全部候选数量，而是能够定位这些短小、可复现的错误路径。
