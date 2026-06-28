# Azure IoT C SDK lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Azure IoT SDK for C
- commit：`fa6ad5d5a1c85be32fb834f68aa102255f9d2b71`
- 扫描日期：2026-06-28
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：93 个文件、1,956 个函数、195 个候选、0 个解析警告
- 包含 tests/samples：408 个文件、3,967 个函数、237 个候选、0 个解析警告

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/azure-iot-sdk-c
python cli.py lifetime ../IoT-repos/azure-iot-sdk-c --include-tests
```

正式源码候选分布：

| 类型 | 数量 |
|---|---:|
| `owned_overwrite` | 113 |
| `memory_not_freed` | 62 |
| `double_release` | 11 |
| `use_after_release` | 9 |
| 合计 | 195 |

人工复查确认了一组相关的 input callback 生命周期问题：

1. 更新已有 Ex callback 时，新 context 分配失败会释放仍在链表中的 callback。
2. 注销 callback 时先释放对象、后移除链表节点，移除失败会留下悬空指针。

这两条路径都可能在后续消息分发或 client 销毁阶段造成 UAF/double free。

## 2. 受影响代码和所有权模型

文件：

`iothub_client/src/iothub_client_core_ll.c`

相关结构：

```c
typedef struct IOTHUB_EVENT_CALLBACK_TAG
{
    STRING_HANDLE inputName;
    IOTHUB_CLIENT_MESSAGE_CALLBACK_ASYNC callbackAsync;
    IOTHUB_CLIENT_MESSAGE_CALLBACK_ASYNC_EX callbackAsyncEx;
    void* userContextCallback;
    void* userContextCallbackEx;
} IOTHUB_EVENT_CALLBACK;
```

`handleData->event_callbacks` 是保存 `IOTHUB_EVENT_CALLBACK *` 的单向链表。
链表保存指针，但 callback 对象及其嵌套字段由 IoT Hub client 负责释放。

析构 helper：

```c
static void delete_event(IOTHUB_EVENT_CALLBACK* event_callback)
{
    STRING_delete(event_callback->inputName);
    free(event_callback->userContextCallbackEx);
    free(event_callback);
}
```

消息到达时，代码从链表中取出 callback 并立即解引用：

```c
IOTHUB_EVENT_CALLBACK* event_callback =
    singlylinkedlist_item_get_value(item_handle);

if (event_callback->callbackAsyncEx != NULL)
{
    result = event_callback->callbackAsyncEx(
        messageHandle,
        event_callback->userContextCallbackEx);
}
```

client 销毁时则遍历链表，对每个值再次调用 `delete_event()`：

```c
singlylinkedlist_foreach(
    handleData->event_callbacks,
    delete_event_callback,
    NULL);
```

因此，只要链表中残留已释放的 callback，就会形成可利用的 UAF 或
double free 路径。

## 3. 确认问题一：更新 callback 分配失败后留下悬空链表节点

### 3.1 位置

函数：

```c
create_event_handler_callback()
```

关键位置：

- 3121 行：查找已有 callback
- 3138 行：取得链表中的 `event_callback`
- 3157—3158 行：释放旧 `userContextCallbackEx`
- 3165—3169 行：为新 context 分配内存
- 3169 行：分配失败时调用 `delete_event(event_callback)`
- 3170 行：返回错误，但未移除链表节点

### 3.2 触发前提

先成功注册一个 Ex callback：

```c
IoTHubClientCore_LL_SetInputMessageCallbackEx(
    handle,
    "input1",
    callback1,
    &context1,
    sizeof(context1));
```

此时：

```text
event_callbacks
  -> ListItem
       -> IOTHUB_EVENT_CALLBACK
            inputName = "input1"
            userContextCallbackEx = copy(context1)
```

随后用同一个 `inputName` 更新 callback：

```c
IoTHubClientCore_LL_SetInputMessageCallbackEx(
    handle,
    "input1",
    callback2,
    &context2,
    sizeof(context2));
```

并让复制 `context2` 的 `malloc()` 失败。

### 3.3 实际错误路径

已有 callback 被查找到后：

```c
event_callback =
    singlylinkedlist_item_get_value(item_handle);
```

此时 `add_to_list == false`，因为该对象已经在链表中。

代码首先破坏旧状态：

```c
free(event_callback->userContextCallbackEx);
event_callback->userContextCallbackEx = NULL;
```

然后尝试分配新 context：

```c
if ((userContextCallbackEx != NULL) &&
    (NULL == (event_callback->userContextCallbackEx =
        malloc(userContextCallbackExLength))))
{
    delete_event(event_callback);
    result = IOTHUB_CLIENT_ERROR;
}
```

`delete_event()` 会释放：

- `event_callback->inputName`
- `event_callback->userContextCallbackEx`
- `event_callback`

但链表中的 `ListItem` 仍保存原地址。

最终状态：

```text
event_callbacks
  -> ListItem
       -> freed IOTHUB_EVENT_CALLBACK
```

### 3.4 后续影响

#### 消息到达导致 UAF

消息分发函数通过 `singlylinkedlist_find()` 找到该节点，并读取：

```c
event_callback->callbackAsyncEx
event_callback->userContextCallbackEx
```

这是对已释放内存的读取，并可能进一步调用从释放内存中读取出的函数指针。

#### client 销毁导致 double free

client 销毁时遍历 `event_callbacks`，再次执行：

```c
delete_event(event_callback);
```

导致 callback、inputName 或 context 被重复释放。

#### 再次注册/注销导致 UAF

再次查找同一 `inputName` 时，比较函数可能读取已释放对象的 `inputName`。
注销时也会取得并再次释放该对象。

### 3.5 根因

更新已有对象时没有采用事务式更新：

```text
释放旧状态
  -> 尝试创建新状态
  -> 创建失败后销毁整个对象
  -> 忘记对象仍由链表引用
```

该函数把“新对象尚未入队”和“已有对象正在更新”两种所有权状态放在同一
错误清理逻辑中，仅用 `add_to_list` 区分成功路径，却没有区分失败析构路径。

### 3.6 建议修复

推荐使用事务式替换：

1. 先分配并复制新 context。
2. 所有新资源准备成功后，再释放旧 context。
3. 最后原子地更新 callback 字段。
4. 新资源分配失败时，保留原 callback 和原 context。

示意代码：

```c
void* new_context = NULL;

if (userContextCallbackEx != NULL)
{
    new_context = malloc(userContextCallbackExLength);
    if (new_context == NULL)
    {
        result = IOTHUB_CLIENT_ERROR;
        goto exit;
    }

    memcpy(new_context,
           userContextCallbackEx,
           userContextCallbackExLength);
}

free(event_callback->userContextCallbackEx);
event_callback->userContextCallbackEx = new_context;
event_callback->callbackAsync = callbackSync;
event_callback->callbackAsyncEx = callbackSyncEx;
event_callback->userContextCallback = userContextCallback;
```

对于新创建、尚未加入链表的 `event_callback`，失败时仍可以直接
`delete_event()`。

如果设计要求更新失败后移除旧 callback，则必须：

1. 先从链表移除对应节点。
2. 确认移除成功。
3. 再调用 `delete_event()`。

但保留原 callback 通常具有更好的失败原子性。

### 3.7 建议回归测试

新增 fault-injection 测试：

```text
1. 注册 input1/callback1/context1，返回成功。
2. 再次注册 input1/callback2/context2。
3. 让复制 context2 的 malloc 失败。
4. API 返回 IOTHUB_CLIENT_ERROR。
5. 模拟 input1 消息到达。
6. 验证 callback1/context1 仍然有效，或至少不会发生 UAF。
7. 销毁 client。
8. ASan/heap checker 不报告 double free。
```

还应覆盖：

- 已有同步 callback 更新为 Ex callback
- 已有 Ex callback 更新为另一个 Ex callback
- 默认 callback（`inputName == NULL`）
- 新 callback 首次注册时的分配失败

### 3.8 结论

**高置信度真实 lifetime bug。**

虽然扫描器最初报告的是 `event_callback` 可能未释放，但人工回溯发现真正的
问题不是泄漏，而是已有链表对象在失败路径被错误释放。

## 4. 确认问题二：注销时先释放对象、后移除链表节点

### 4.1 位置

函数：

```c
remove_event_unsubscribe_if_needed()
```

关键代码：

```c
delete_event(event_callback);
if (singlylinkedlist_remove(
        handleData->event_callbacks,
        item_handle) != 0)
{
    LogError("singlylinkedlist_remove failed");
    result = IOTHUB_CLIENT_ERROR;
}
```

### 4.2 错误顺序

当前顺序：

```text
找到链表节点
  -> 释放节点保存的 callback
  -> 尝试移除 ListItem
```

如果 `singlylinkedlist_remove()` 返回失败：

```text
event_callbacks
  -> ListItem
       -> freed IOTHUB_EVENT_CALLBACK
```

后果与问题一相同：

- 消息分发读取已释放 callback
- 后续注销再次释放
- client 销毁阶段 double free

### 4.3 可达性说明

这里的 `item_handle` 刚由同一个链表的 `singlylinkedlist_find()` 返回，因此
正常实现下 remove 通常应成功。该问题主要在以下情况下触发：

- list 内部错误或内存破坏
- 并发修改未被外层同步完全约束
- mock/fault-injection 让 remove 返回失败
- API 未来实现发生变化

因此它的现实可达性低于问题一，但生命周期顺序本身仍不安全。

### 4.4 建议修复

先移除节点，成功后再释放内容：

```c
if (singlylinkedlist_remove(
        handleData->event_callbacks,
        item_handle) != 0)
{
    LogError("singlylinkedlist_remove failed");
    result = IOTHUB_CLIENT_ERROR;
}
else
{
    delete_event(event_callback);
    result = IOTHUB_CLIENT_OK;
}
```

需要确认 `singlylinkedlist_remove()` 只释放 list node，不释放 node content。
从当前代码的使用方式看，content 确实由调用者单独管理。

### 4.5 建议回归测试

1. 注册一个 callback。
2. 注销该 callback。
3. 注入 `singlylinkedlist_remove()` 失败。
4. 验证 callback 没有提前释放，或者链表不会继续引用已释放对象。
5. 再次分发消息或销毁 client。
6. ASan 不应报告 UAF/double free。

### 4.6 结论

**真实的不安全生命周期顺序；触发概率低于问题一，建议随问题一一起修复。**

## 5. 主要误报来源

### 5.1 字段赋值被识别成 owned-variable 覆盖

113 条 `owned_overwrite` 大量来自：

```c
result->field = value;
instance->handle = create_handle();
new_config->string = clone_string();
```

工具把某些 `base->field = ...` 错误归约成对 `base` 自身的覆盖，导致：

```text
result = malloc(...)
result->field = ...
```

被报告成“`result` 未释放就被新值覆盖”。

典型文件：

- `iothub_module_client_ll.c`
- `iothubtransport_amqp_messenger.c`
- provisioning service client 的 JSON model parser
- `message_queue.c`

这些并不是真实 owned overwrite。

### 5.2 异步 callback context 所有权转移

大量 `memory_not_freed` 采用以下模式：

```text
malloc(context)
  -> 调用 async API(context, completion_callback)
  -> async API 成功：completion_callback 负责 free(context)
  -> async API 失败：当前函数立即 free(context)
```

抽查确认清理完整的例子包括：

- `IoTHubClientCore_SendEventAsync()`
- `IoTHubClientCore_SendReportedState()`
- `IoTHubClientCore_GetTwinAsync()`
- `amqp_device_send_event_async()`
- `amqp_device_send_twin_update_async()`
- `amqp_device_get_twin_async()`
- `IoTHubTransport_AMQP_Common_ProcessItem()`
- `telemetry_messenger_send_async()`
- `twin_messenger_report_state_async()`

工具目前不能表达“返回成功后由 callback 释放、返回失败由调用方释放”的
条件式异步所有权。

### 5.3 容器所有权

以下对象成功加入容器后由容器或所属 handle 管理：

- `DList_InsertTailList`
- `singlylinkedlist_add`
- `VECTOR_push_back`
- message queue
- device/module/configuration linked list

函数退出时对象仍 active 是预期行为。

### 5.4 返回所有权

多个 create/clone/helper 函数返回新分配对象：

- `retry_control_clone_option`
- `retry_control_create`
- 各 AMQP `*_create`
- service-client create API
- TPM key/sign helper
- serializer schema/multitree create API

成功返回时所有权交给调用方，不能按局部函数泄漏处理。

### 5.5 条件编译错误合流

authorization 和 provisioning auth 模块包含大量：

```c
#ifdef USE_PROV_MODULE
#else
#endif
```

释放对象、将指针设为 `NULL` 和最终返回处于不同预处理路径。未确定具体编译
配置时，工具可能把互斥路径合并成 UAF/double-free。

例如 `IoTHubClient_Auth_CreateFromDeviceAuth()` 在释放 `result` 后明确设置：

```c
result = NULL;
```

最终返回的不是已释放指针。

## 6. 工具改进建议

该项目暴露出以下通用精度问题。

### P1：字段赋值与 base variable 覆盖必须区分

以下语句不能杀死 `result` 的 owned state：

```c
result->field = value;
(*result).field = value;
result[index].field = value;
```

只有真正给 `result` 自身赋值时才属于覆盖：

```c
result = new_value;
```

### P2：条件式异步 ownership sink

应支持类似：

```text
async_submit(arg, callback)
  success -> ownership transfers to callback/system
  failure -> ownership remains with caller
```

如果当前函数在失败分支释放参数，就不应报告成功退出路径泄漏。

### P3：容器 sink 失败条件

`singlylinkedlist_add()`、`VECTOR_push_back()` 等 API 只有成功时接管对象。
分析器需要同时记录：

- 成功分支：对象逃逸到容器
- 失败分支：对象仍由调用方持有

### P4：链表内容与链表节点的独立生命周期

本次真 bug 的关键在于：

```text
ListItem lifetime != ListItem->content lifetime
```

释放 content 不会自动删除 ListItem。后续检查器可以针对：

```c
free(content);
remove(list, item);
```

这种顺序生成 dangling-container-entry 候选。

## 7. 推荐后续步骤

1. 为“更新已有 Ex callback 时 malloc 失败”添加最小 fault-injection 测试。
2. 使用 ASan 验证消息分发和 client 销毁路径。
3. 将 callback 更新改成事务式替换。
4. 调整注销顺序为“先 remove，后 delete”。
5. 单独修复工具的字段赋值识别，重新扫描以消除 113 条级联误报。
6. 再对剩余 async-context 候选进行大模型或人工验证。

建议向上游提交时把两个 callback 问题放在同一个 issue/补丁中，因为它们共享
同一根因：

> callback content 的释放没有与链表节点生命周期保持一致。
