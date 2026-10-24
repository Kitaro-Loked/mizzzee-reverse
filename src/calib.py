# -*- coding: utf-8 -*-
"""
强度标定器 - 从 10 到 100 逐级提升，每级 4 秒。

用法：设备开机、手机断开连接后运行。
      记住「从哪个数值开始有感觉」「哪个数值最舒服/最强」。
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


LEVELS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
HOLD = 4.0


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
    print("已连接\n", flush=True)

    for v in LEVELS:
        f = frame(0x08, [v])
        t_end = time.time() + HOLD
        while time.time() < t_end:
            await c.write_gatt_char(CHR, f, response=False)
            left = t_end - time.time()
            bar = "█" * (v // 5)
            print(f"\r  当前强度 {v:3d}  {bar:<20}  剩余 {left:3.1f}s   ", end="", flush=True)
            await asyncio.sleep(0.25)
        print(flush=True)
        for _ in range(2):
            await c.write_gatt_char(CHR, frame(0x08, [0]), response=False)
            await asyncio.sleep(0.12)
        await asyncio.sleep(1.0)

    for _ in range(3):
        await c.write_gatt_char(CHR, frame(0x08, [0]), response=False)
        await asyncio.sleep(0.15)
    await c.disconnect()
    print("\n标定结束。请告诉开发者：从哪个数值开始有感觉、哪个数值最强。")


try:
    asyncio.run(main())
except Exception as e:
    print(f"\n出错：{type(e).__name__}: {e}")
input("按回车退出…")
