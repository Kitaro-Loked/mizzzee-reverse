# 协议规范

> 实测对象：`mizzzee_0008`（广播名），白牌模组 `urt-01`
> 特征：`0000ffe1-0000-1000-8000-00805f9b34fb`（notify + write-without-response）
> 校验方式：全部经实测验证，非推测

---

## 1. 帧格式

```
+------+------+------+------------------+----------+
| 0xAA | cmd  | len  | payload (len 字节) | checksum |
+------+------+------+------------------+----------+
```

- `checksum = (0xAA + cmd + len + Σpayload) & 0xFF`
- 大端序，无转义、无分帧、无加密、无握手
- 写入方式：`write-without-response`（写 with response 会失败）

**帧构造（与官方 App 逐字节一致，经 Node 执行官方函数验证）：**

```python
def frame(cmd, payload):
    body = [0xAA, cmd, len(payload)] + list(payload)
    body.append(sum(body) & 0xFF)
    return bytes(body)
```

官方 `Ce()` 在 `AA+cmd+len+Σ > 255` 时取低 16 位再 &0xFF，与本实现等价。

---

## 2. 命令集

### 2.1 `0x08` —— 强度控制（核心指令）

```
AA 08 01 <level> <crc>
```

| level | 效果 |
|---|---|
| 0 | 停止 |
| 1 – 254 | 强度，**线性量程** |
| 255 | **被拒收**，无应答 |

**应答：**

```
AA 09 06 <payload> <sum> 00 00 <X> <crc>
```

例：发 `AA 08 01 32 E5` → 收 `AA 09 06 32 E5 36 00 00 B0 B6`

`<X>` 无常理规律（强度 0 与 90 同值），判定为滚动计数/校验，**不可用作状态反馈**。

### 2.2 `0x04` —— 加热

```
AA 04 01 <0|1> <crc>      0=关 1=开
```

**应答：** `AA 05 01 <payload> <crc>`

例：`AA 04 01 01 B0` → `AA 05 01 01 B1`

### 2.3 `0x06` —— 已实现但无可观测副作用

```
AA 06 01 <n> <crc>          单字节
AA 06 05 <b0..b4> <crc>     五通道（官方 writeMultiMotor / writeTimeAndIntensity）
```

**应答：** `AA 07 05 <payload> <sum> 00 00 <crc>`

实测结论：有 ACK，但**不产生任何可观测的电机/加热状态变化**。推测为多马达机型的兼容指令路径。

### 2.4 未实现的 253 个命令字节

对 `0x00`–`0xFF` 全扫描（每字节 2 种长度），除上述 3 个外全部静默丢弃。

---

## 3. APK 中的模式指令表（本机不支持）

来自官方 `writeSpecialMode`，13 个索引：

| 索引 | 帧 | 推断含义 |
|---|---|---|
| 0–6 | `AA 0A 01 01` … `AA 0A 01 07` | 7 个固件模式 |
| 7 | `AA 06 01 01 B2` | 加热开 |
| 8 | `AA 06 01 00 B1` | 加热关 |
| 9 | `AA 0A 01 00 B5` | 停止 |
| 10–12 | `AA 0A 01 08` … `AA 0A 01 0A` | 3 个固件模式 |

**本机对 `0x0A` 完全无应答。** 这套表是给老机型用的，`mizzzee_0008` 的固件没实现。

> 注：索引 7/8 说明官方把加热同时映射到 `0x06 01 <0/1>`，而我们实测 `0x04 01 <0/1>` 才是本机的加热指令——两套固件分支的证据。

---

## 4. 状态帧（自发上报，约 12 秒一条）

```
AA 01 06 <bat> <b1> <b2> 01 <counter> <crc>
```

| 字节 | 含义 |
|---|---|
| `bat` | 电量百分比（实测 0x0A=10%、0x1E=30%、0x28=40%、0x3C=60%） |
| `b1` | 未知（观测到 03/04/05/06） |
| `b2` | 未知，疑似速度/功率（观测到 09/28/64/C0/FC/74…） |
| `01` | 常量 |
| `counter` | **会话命令计数器**，01→02→03… 单调递增，**不是模式位** |
| `crc` | 校验 |

**没有可主动诱发的查询指令。** `0x3D 02 32 32` 曾被误判为状态查询，单帧触发实验已证伪（那只是周期帧碰巧赶到）。

---

## 5. GATT 全景

```
服务 1800 (Generic Access)
  2A00 Device Name    = "mizzzee_0008"
  2A01 Appearance     = 00 00
  2A04 Preferred Params

服务 1801 (Generic Attribute)
  2A05 Service Changed  ← 订阅被拒

服务 180A (Device Information)
  2A23 System ID        = 0C B3 42 00 00 3D 32 69
  2A24 Model Number     = "Model Number"      ← 占位字符串
  2A25 Serial Number    = "Serial Number"     ← 占位字符串
  2A26 Firmware Rev     = "Firmware Revision" ← 占位字符串
  2A27 Hardware Rev     = "Hardware Revision" ← 占位字符串
  2A28 Software Rev     = "Software Revision" ← 占位字符串
  2A29 Manufacturer     = "Manufacturer Name" ← 占位字符串
  2A2A PnP ID           = FE 00 "experimental"
  2A50 PnP ID           = 01 0F 0F 00 00 10 01

服务 FFE0 (自定义)
  FFE1  notify + write-without-response   ← 唯一命令通道
```

**无 OTA / DFU 服务**，无法通过蓝牙拉取固件。

厂商特定数据：`3855: 06 75 72 74 2D 30 31 00…` = `"urt-01"`

---

## 6. 运维注意事项

### 6.1 帧率限制（重要）

实测连续突发下发约 66 帧后，设备开始丢帧（ACK 完全消失）。

**对策：** 两次写入最小间隔 **180ms**；持续输出用 250ms 保活帧。

### 6.2 多字节 payload

`0x08` 的 2/3 字节 payload 会被原样回显，但无可观测行为差异：

```
AA 08 02 32 20      → ACK 回显 32 20
AA 08 03 32 32 20   → ACK 回显 32 32 20
```

官方 `writeTimeAndIntensity` 会用 3 字节带时长，但本机不响应时长语义。

### 6.3 无鉴权

任何能连上的客户端都能完全控制设备，无配对、无绑定、无加密。
