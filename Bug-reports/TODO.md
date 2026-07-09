


- **Huawei LiteOS 老仓库**  
  https://gitee.com/LiteOS/LiteOS  
  可能维护弱一些，但资源 bug 机会多。
  
- **eProsima Fast DDS**  
  https://github.com/eProsima/Fast-DDS  
  C++ 为主，当前工具可能覆盖差一些，但数据集好看。

- **NanoSDK**  
  https://github.com/nanomq/NanoSDK  
  MQTT SDK，C 风格更适合你的分析器。

- **EMQX**  
  https://github.com/emqx/emqx  
  主体不是 C，作为论文候选可以放，但不建议优先用 `IoT-lifetime-bugs` 扫。

- **EMQ Neuron**  
  https://github.com/emqx/neuron  
  工业连接 server，比 EMQX 更可能有 C/C++ 资源生命周期问题。

- **libwebsockets**  
  https://github.com/warmcat/libwebsockets  
  网络库，TLS/socket/poll/buffer 生命周期复杂，维护者也比较认真。

- **OpenHarmony LiteOS-M**  
  https://gitee.com/openharmony/kernel_liteos_m  
  MCU/轻量设备内核，适合中文生态 bug report。
