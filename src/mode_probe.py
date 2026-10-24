# -*- coding: utf-8 -*-
"""
模式测试器 - 系统测试四类候选「模式指令」，找出设备真正认的那一种。

用法：设备开机、手机断开连接后运行。记住有反应的段号告诉开发者。
"""
import asyncio
import os
import time

from bleak import BleakClient, BleakScanner

NAME_HINT = "mizzzee"
KNOWN = os.environ.get("MIZZZEE_ADDR", "")
CHR = "0000ffe1-0000-1000-8000-00805f9b34fb"


def frame(cmd, payload):
    body = [0xAA, cmd, len(payload)] + list(payload)
    body.append(sum(body) & 0xFF)
    return bytes(body)


# (名称, 命令字节, 是否带第二字节)
FAMILIES = [
    ("A", 0x0A, False),
    ("B", 0x06, False),
    ("C", 0x08, True),
    ("D", 0x06, True),
]
DURATION = 4.0
KEEP = 0.30


async def find():
    found = await BleakScanner.discover(timeout=8.0, return_adv=True)
    for addr, (dev, adv) in found.items():
        nm = adv.local_name or dev.name or ""
        if NAME_HINT in nm.lower():
            return addr
    return KNOWN


async def main():
    addr = await find()
    c = BleakClient(addr, timeout=20.0)
    await c.connect()
    print(f"已连接 {addr}\n")

    acks = []

    def cb(_h, d):
        d = bytes(d)
        if d and d[0] == 0xAA and d[1] in (0x05, 0x07, 0x09):
            acks.append(d.hex(" "))

    await c.start_notify(CHR, cb)
    await asyncio.sleep(0.4)

    idx = 0
    for fam, cmd, two in FAMILIES:
        for n in range(1, 10):
            idx += 1
            acks.clear()
            payload = [n, 50] if two else [n]
            f = frame(cmd, payload)
            t_end = time.time() + DURATION
            sent = 0
            while time.time() < t_end:
                try:
                    await c.write_gatt_char(CHR, f, response=False)
                    sent += 1
                except Exception as e:
                    print(f"写失败: {e}")
                    break
                await asyncio.sleep(KEEP)
            await asyncio.sleep(0.8)
            print(f"[{idx:02d}] 族{fam} cmd=0x{cmd:02X} 值={n}  "
                  f"帧 {f.hex(' ').upper():<16} 发 {sent} 次  ACK {len(acks)} 条",
                  flush=True)
            for _ in range(3):
                await c.write_gatt_char(CHR, frame(0x08, [0]), response=False)
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.7)

    for _ in range(3):
        await c.write_gatt_char(CHR, frame(0x08, [0]), response=False)
        await asyncio.sleep(0.15)
    await c.disconnect()
    print("\n测试结束。请告诉开发者：哪些段号有动作。")


try:
    asyncio.run(main())
except Exception as e:
    print(f"\n出错：{type(e).__name__}: {e}")
input("按回车退出…")
