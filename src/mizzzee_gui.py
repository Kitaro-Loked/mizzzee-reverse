#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mizzzee_gui.py - 醉清风 / mizzzee 设备 Windows 图形控制器（PySide6）

协议（从 APK 的 uni-app 逻辑 app-service.js 提取，并经真机验证）：
    帧 = AA | CMD | LEN | PAYLOAD | SUM        SUM = 前 (3+LEN) 字节之和 & 0xFF
    0x08  单字节强度 [0-100]                                  <- 本机设备实测唯一有效指令
    0x06  五通道控制  [伸缩, 振动, 吸吮, 旋转, 加热]  各 0-100   <- 本机设备仅 ACK，无动作
    0x08  N 字节 = N 个马达强度（本机 N=1）
    0x04  加热开关 [0/1]      0x0D 计数复位 [1]      0x12 计数停止 [0]
    设备应答：0x06 -> AA 07 ...（回显 payload）；0x08 -> AA 09 ...
    0x0A 档位帧在本机设备上无任何应答，已弃用。

关键设计（v3）：
    设备不会「记住」最后一次指令持续运转，必须**周期性刷新**才保持动作。
    因此所有动作统一走一条 250ms 的保活流：UI 只更新目标值，由 BLE 线程
    按固定节奏下发，天然避免指令重叠与刷爆（过快下发会让固件假死）。
"""

import asyncio
import datetime as dt
import os
import queue
import time
import random
import socket
import sys
import threading

from PySide6.QtCore import Qt, QTimer, QRectF, QUrl
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget,
    QVBoxLayout, QWidget,
)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("缺少 bleak：pip install bleak")
    sys.exit(1)

try:
    from config_tool import ConfigDlg
except Exception:
    ConfigDlg = None

NAME_HINT = "mizzzee"
KNOWN_ADDR = os.environ.get("MIZZZEE_ADDR", "")   # 填你的设备 MAC，或留空自动搜索

CMD_HEAT, CMD_MODE, CMD_FLEX, CMD_RESET = 0x04, 0x06, 0x08, 0x0D
CHANNELS = ["伸缩", "振动", "吸吮", "旋转", "加热"]

STREAM_MS = 250          # 保活下发间隔（过快会刷爆固件）
STOP_REPEAT = 3          # 停止帧重发次数


def frame(cmd, payload=()):
    body = [0xAA, cmd & 0xFF, len(payload) & 0xFF] + [max(0, min(255, int(p))) for p in payload]
    body.append(sum(body) & 0xFF)
    return bytes(body)


def f_level(v):
    """本机设备实测：0x08 + 单字节 = 强度（0-100）。"""
    return frame(CMD_FLEX, [max(0, min(100, int(v)))])


def f_channels(vals):
    """兼容旧调用：取最大值作为单通道强度。"""
    return f_level(max(list(vals) + [0]))


def f_zero():
    return frame(CMD_FLEX, [0])


def decode(data):
    if len(data) < 4 or data[0] != 0xAA:
        return None
    cmd, ln = data[1], data[2]
    if len(data) != 4 + ln:
        return None
    return cmd, data[3:3 + ln], (sum(data[:3 + ln]) & 0xFF) == data[-1]


# 技能：档位数组取自 APK 内置表 _s，x10 映射到 0-100
SKILLS = [
    ("如影随形", [40, 60, 50], 900),
    ("疾风旋舞", [80, 90, 90], 700),
    ("幻影随行", [60, 60, 80], 850),
    ("水光环绕", [20, 20, 30], 1100),
    ("神龙摆尾", [30, 90, 30], 750),
]

def soften(levels, max_step=16, floor=0):
    """柔化：限制每步变化幅度，去掉生硬跳变，让曲线更顺。"""
    out = []
    prev = levels[0] if levels else 0
    for v in levels:
        v = max(floor, min(100, v))
        if v > prev + max_step:
            v = prev + max_step
        elif v < prev - max_step:
            v = prev - max_step
        out.append(v)
        prev = v
    return out


def _w_tide():
    """潮汐：平滑正弦起伏"""
    import math
    return [int(50 + 40 * math.sin(2 * math.pi * i / 16)) for i in range(32)]


def _w_heart():
    """心跳：双跳 + 停顿"""
    one = [88, 88, 0, 0, 72, 0, 0, 0]
    return one * 3


def _w_breath():
    """呼吸：慢吸 3s，停 1s，慢呼 3s，停 1s"""
    up = [int(15 + (70 - 15) * i / 11) for i in range(12)]
    down = list(reversed(up))
    return up + [70] * 4 + down + [15] * 4


def _w_stairs():
    """阶梯：逐级爬升后回落"""
    steps = [20, 32, 44, 56, 68, 80, 92, 20]
    return [v for v in steps for _ in range(2)]


def _w_rain():
    """雨点：随机短促点击"""
    out = []
    for _ in range(12):
        out.append(random.randint(45, 95))
        out += [0] * random.randint(1, 3)
    return out


def _w_sting():
    """电击：高频通断"""
    return [92, 0] * 16


def _w_crescendo():
    """渐强：由弱到强持续推高"""
    return [int(18 + (100 - 18) * i / 23) for i in range(24)]


def _w_spiral():
    """螺旋：缓慢爬升后骤然回落"""
    return list(range(20, 101, 5)) + [20, 20]


def _w_morse_old():
    """节拍：短-短-长"""
    return [90, 0, 90, 0, 90, 90, 90, 0, 0, 0, 0, 0]


def _w_walk():
    """漫步：随机游走，不重复"""
    lv, out = 62, []
    for _ in range(28):
        lv = max(15, min(95, lv + random.randint(-28, 28)))
        out.append(lv)
    return out


def _w_storm():
    """雷雨：低沉轰鸣 + 突然闪电"""
    out = []
    for i in range(28):
        out.append(max(8, 18 + random.randint(-6, 10)))
    for _ in range(2):
        out += [100, 0, 0, 100, 0, 0, 92, 0, 0, 0, 0]
    return out


def _w_ball():
    """弹跳球：每次落地弹得更低，间隔越来越长"""
    out, lv, gap = [], 95, 1
    while lv > 8:
        out.append(lv)
        out += [0] * gap
        lv = int(lv * 0.72)
        gap = min(3, gap + (1 if lv < 55 else 0))
    return out


def _w_racing():
    """心跳加速：间隔逐次缩短，像被撩到"""
    out = []
    pauses = [6, 5, 4, 3, 3, 2, 2, 1]
    for pause in pauses:
        out += [90, 0, 74, 0]
        out += [0] * pause
    return out


def _w_type():
    """打字机：不规则短促敲击"""
    out = []
    for _ in range(14):
        for _ in range(random.randint(1, 3)):
            out += [random.randint(65, 92), 0]
        out += [0] * random.randint(1, 3)
    return out


def _w_morse():
    """摩斯情话：把 LOVE 打成节拍"""
    code = {"L": ".-..", "O": "---", "V": "...-", "E": "."}
    out = []
    for ch in "LOVE":
        for sym in code[ch]:
            out += [88] * (1 if sym == "." else 3)
            out += [0]
        out += [0, 0]
    return out


def _w_slot():
    """老虎机：随机连击后突然定格"""
    out = []
    for _ in range(9):
        out += [random.randint(70, 100), 0, 0]
    hold = random.randint(60, 95)
    out += [hold] * 16
    return out


def _w_train():
    """火车：咣当咣当，越跑越快"""
    out, gap = [], 3
    for seg in range(8):
        out += [82, 82] + [0] * max(1, gap)
        gap = max(1, gap - 1)
    return out


def _w_ebb():
    """退潮：缓慢退去，最后归于平静"""
    out = []
    lv = 92
    while lv > 0:
        out.append(int(lv))
        out.append(int(max(0, lv + random.randint(-6, 6))))
        lv -= 4
    return out + [0] * 4


def _w_swell():
    """绵长：30 秒一轮的缓慢涨落，全程不断"""
    import math
    return [int(45 + 25 * math.sin(2 * math.pi * i / 60)) for i in range(120)]


def _w_onsen():
    """温泉：恒定温热感，只有极轻微的呼吸式起伏"""
    return [36 + (2 if i % 12 < 6 else 0) for i in range(80)]


def _w_surge():
    """潮起：极慢地涨到顶，停一会儿，再慢慢退"""
    up = [int(20 + 55 * i / 39) for i in range(40)]
    return up + [75] * 12 + list(reversed(up))


def _w_afterglow():
    """余韵：从高处缓缓退去，始终不归零"""
    return [max(10, int(58 - 46 * i / 39)) for i in range(40)]


def _w_caress():
    """轻抚：在 32 附近轻微游走，没有突变"""
    out, lv = [], 32
    for _ in range(60):
        lv = max(22, min(46, lv + random.choice([-2, -1, 0, 0, 1, 2])))
        out.append(lv)
    return out


def _w_fadein():
    """渐入：两分钟才升到 60，几乎察觉不到起点"""
    return [int(6 + 54 * i / 479) for i in range(480)]


def _w_whisper():
    """耳语：极轻的持续，几乎只有一丝颤动"""
    out = []
    for i in range(120):
        out.append(14 + (3 if i % 6 < 3 else 0) + random.choice([0, 0, 1, -1]))
    return out


def _w_warm():
    """暖流：二十秒缓缓升起，再缓缓落下，全程不断"""
    up = [int(24 + 30 * i / 39) for i in range(40)]
    return up + list(reversed(up))


def _w_linger():
    """缠绵：两个强度之间长时间往返，变化极缓"""
    return [46] * 16 + [58] * 16 + [46] * 16 + [58] * 16


def _w_spring():
    """春潮：两段式涨落，中间留一小段安静"""
    up = [int(22 + 46 * i / 29) for i in range(30)]
    return up + [26] * 10 + up + [26] * 10


def _w_prelude():
    """前奏：先轻抚一分钟，再慢慢升上来"""
    light = [12 + (2 if i % 8 < 4 else 0) for i in range(240)]
    rise = [int(14 + 52 * i / 59) for i in range(60)]
    return light + rise



def _m_const():
    return [55] * 20


def _m_slowin():
    return [30, 45, 60, 72, 25, 30, 45, 60, 72, 25]


def _m_fastin():
    return [78, 66, 56, 48, 42, 78, 66, 56, 48, 42]


def _m_three():
    return [40, 40, 40, 88] * 4


def _m_long():
    return [20, 20, 88, 88, 20, 20, 88, 88]


def _m_accel():
    out = []
    for period in (8, 7, 6, 5, 4, 3):
        out += [92] + [28] * (period - 1)
    return out


def _m_staccato():
    return [72, 72, 0, 0] * 6


def _m_double():
    return [82, 82, 0, 0, 82, 82, 0, 0, 0, 0] * 2


def _m_random():
    out = []
    for _ in range(24):
        out += [random.randint(35, 95)] * random.randint(1, 3)
    return out


AI_STYLES = {
    "温柔": "缓慢连贯，强度 20-45，不要突变，像呼吸一样",
    "渐进": "从 25 逐步升到上限，中途有短暂回落，整体呈上升趋势",
    "激烈": "强度长期贴近上限，节奏偏快，偶尔爆发",
    "挑逗": "强弱交替，时长忽长忽短，制造期待与停顿",
    "随机": "自由发挥，变化丰富但不要失控",
}

FW_MODES = [
    ("固件模式1", "aa0a0101b6"),
    ("固件模式2", "aa0a0102b7"),
    ("固件模式3", "aa0a0103b8"),
    ("固件模式4", "aa0a0104b9"),
    ("固件模式5", "aa0a0105ba"),
    ("固件模式6", "aa0a0106bb"),
    ("固件模式7", "aa0a0107bc"),
    ("固件模式8", "aa0a0108bd"),
    ("固件模式9", "aa0a0109be"),
    ("固件模式10", "aa0a010abf"),
]

THRUST_MODES = [
    ("匀速", _m_const),
    ("缓入急出", _m_slowin),
    ("急入缓出", _m_fastin),
    ("三浅一深", _m_three),
    ("长行程", _m_long),
    ("渐快", _m_accel),
    ("顿挫", _m_staccato),
    ("双连击", _m_double),
    ("随机", _m_random),
]

WAVE_DEFS = [
    ("潮汐", "平滑起伏", _w_tide),
    ("心跳", "双跳停顿", _w_heart),
    ("呼吸", "慢吸慢呼", _w_breath),
    ("阶梯", "逐级爬升", _w_stairs),
    ("雨点", "随机点击", _w_rain),
    ("电击", "高频通断", _w_sting),
    ("渐强", "持续推高", _w_crescendo),
    ("螺旋", "爬升骤落", _w_spiral),
    ("漫步", "随机游走", _w_walk),
    ("雷雨", "轰鸣+闪电", _w_storm),
    ("弹跳球", "越弹越低", _w_ball),
    ("心跳加速", "越来越急", _w_racing),
    ("打字机", "不规则敲击", _w_type),
    ("摩斯情话", "LOVE 节拍", _w_morse),
    ("老虎机", "连击后定格", _w_slot),
    ("火车", "咣当加速", _w_train),
    ("退潮", "缓缓退去", _w_ebb),
    ("绵长", "30秒缓涨落", _w_swell),
    ("温泉", "恒定温热", _w_onsen),
    ("潮起", "极慢涨退", _w_surge),
    ("余韵", "缓缓退去不归零", _w_afterglow),
    ("轻抚", "轻微游走", _w_caress),
    ("渐入", "两分钟渐强", _w_fadein),
    ("耳语", "极轻持续", _w_whisper),
    ("暖流", "缓缓升降", _w_warm),
    ("缠绵", "两级往返", _w_linger),
    ("春潮", "两段涨落", _w_spring),
    ("前奏", "先轻后升", _w_prelude),
]
TEMPOS = [("慢", 2), ("中", 1), ("快", 0.5)]

QSS = """
* { font-family: "Microsoft YaHei UI"; }
#root { background: #0d1117; }
#card { background: #161b22; border: 1px solid #30363d; border-radius: 14px; }
#cardSoft { background: #161b22; border-radius: 14px; }
QLabel { color: #e6edf3; }
#title { font-size: 20px; font-weight: 700; }
#sub { color: #8b949e; font-size: 11px; font-family: Consolas; }
#metricDim { color: #8b949e; font-size: 13px; font-family: Consolas; }
#state { color: #8b949e; font-size: 12px; }
#chan { color: #8b949e; font-size: 12px; }
#val { color: #7cf03d; font-size: 12px; font-family: Consolas; }
#hint { color: #484f58; font-size: 11px; }
QPushButton { background: #1c2430; color: #e6edf3; border: none; border-radius: 10px;
    padding: 12px 16px; font-size: 13px; }
QPushButton:hover { background: #24303f; }
QPushButton:disabled { color: #484f58; background: #14181f; }
#tab { background: transparent; color: #8b949e; border-radius: 9px; padding: 8px 22px; }
#tab:checked { background: #1c2430; color: #7cf03d; font-weight: 600; }
#skill { min-height: 66px; font-size: 13px; }
#wave { min-height: 40px; font-size: 12px; padding: 6px 4px;
        background: #1c2430; border-radius: 8px; }
#wave:hover { background: #24303f; }
#wave:checked { background: #14361a; color: #7cf03d; border: 1px solid #2ea043; }
#skill:checked { background: #14361a; color: #7cf03d; border: 1px solid #2ea043; }
#play { background: #7cf03d; color: #08120a; font-size: 15px; font-weight: 700;
        border-radius: 12px; padding: 14px 20px; }
#play:hover { background: #8dff4f; }
#stop { background: #3d1414; color: #f85149; font-size: 14px; font-weight: 600;
        border-radius: 12px; padding: 14px 18px; }
#stop:hover { background: #4d1a1a; }
#power { background: #1c2430; color: #8b949e; border-radius: 20px; padding: 8px 16px; }
QSlider::groove:horizontal { height: 6px; background: #21262d; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #7cf03d; border-radius: 3px; }
QSlider::handle:horizontal { width: 16px; height: 16px; margin: -5px 0;
    background: #e6edf3; border-radius: 8px; }
QPlainTextEdit { background: #161b22; color: #8b949e; border: none;
    font-family: Consolas; font-size: 11px; }
QScrollBar:vertical { background: #161b22; width: 8px; }
QScrollBar::handle:vertical { background: #30363d; border-radius: 4px; }
"""


class AIClient:
    """极简 OpenAI 兼容客户端（urllib，无第三方依赖）。"""

    def __init__(self, base=os.environ.get("AI_BASE_URL", "https://api.example.com/v1"),
                 key=os.environ.get("AI_API_KEY", ""),   # 从环境变量读取，勿硬编码
                 model="deepseek-v4-flash"):
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
        self.last_ttft = 0.0
        self.last_total = 0.0
        self.last_err = ""

    def chat(self, prompt, system="", max_tokens=400, temperature=0.85, timeout=40):
        import json
        import time
        import urllib.error
        import urllib.request

        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        body = json.dumps({"model": self.model, "messages": msgs,
                           "max_tokens": max_tokens, "temperature": temperature,
                           "stream": True}).encode()
        req = urllib.request.Request(
            self.base + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        t0 = time.perf_counter()
        ttft, text = None, ""
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for line in r:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    line = line.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        j = json.loads(payload)
                        text += j["choices"][0].get("delta", {}).get("content", "") or ""
                    except Exception:
                        pass
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                detail = ""
            self.last_err = f"HTTP {e.code} {detail}"
            return ""
        except Exception as e:
            self.last_err = f"{type(e).__name__}: {e}"
            return ""
        self.last_ttft = (ttft or 0) * 1000
        self.last_total = (time.perf_counter() - t0) * 1000
        self.last_err = ""
        return text


class FsVideo(QVideoWidget):
    """全屏时 ESC 退出；双击切换全屏。"""

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.isFullScreen():
            self.setFullScreen(False)
            return
        super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.setFullScreen(not self.isFullScreen())
        super().mouseDoubleClickEvent(e)


class VideoEngine:
    """视频分析：动作(帧差) + 声音(RMS) 双通道，融合成强度曲线。"""

    GRID = 40  # 毫秒/格

    def __init__(self):
        self.ts = []
        self.motion = []
        self.audio = []
        self.lv = []
        self.duration = 0.0
        self.fps = 25.0
        self.path = ""
        self.mix = 0.55          # 动作权重，0.55 表示动作 55% + 声音 45%

    # ---------- 分析 ----------
    def analyze(self, path, progress_cb=None, gain=1.0):
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise RuntimeError("无法打开视频（编码不支持或文件损坏）")
        self.path = path
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration = (total / self.fps) if total else 0.0

        prev, m_ts, m_val = None, [], []
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            small = cv2.resize(frame, (96, 54), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype("float32")
            if prev is not None:
                m_ts.append(idx * 1000.0 / self.fps)
                m_val.append(float(np.abs(gray - prev).mean()))
            prev = gray
            idx += 1
            if progress_cb and total and idx % 8 == 0:
                progress_cb(min(0.7, idx / total * 0.7))
        cap.release()
        if not m_val:
            raise RuntimeError("视频太短或全静止，无法分析")

        # 动作曲线归一
        arr = np.array(m_val, dtype="float32")
        ref = float(np.percentile(arr, 92)) or 1.0
        motion = np.clip(arr / ref * 100.0 * gain, 0, 100)

        # 声音曲线
        a_ts, a_val = [], []
        try:
            a_ts, a_val = self._audio_curve(path)
        except Exception:
            pass
        if progress_cb:
            progress_cb(0.85)

        # 统一到 40ms 网格
        dur = max(self.duration, (m_ts[-1] + self.GRID) / 1000.0 if m_ts else 1.0)
        grid = np.arange(0, max(0.5, dur), self.GRID / 1000.0) * 1000.0
        mo = np.interp(grid, np.array(m_ts), motion) if m_ts else np.zeros_like(grid)
        if a_ts:
            au = np.interp(grid, np.array(a_ts), np.array(a_val, dtype="float32"))
        else:
            au = np.zeros_like(grid)

        self.ts = grid.tolist()
        self.motion = mo.tolist()
        self.audio = au.tolist()
        self._rebuild()
        if progress_cb:
            progress_cb(1.0)
        return len(grid)

    def _audio_curve(self, path):
        import os
        import subprocess
        import tempfile
        import wave

        import numpy as np

        ff = None
        try:
            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return [], []
        tmp = os.path.join(tempfile.gettempdir(), "_mz_audio.wav")
        subprocess.run([ff, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "wav", tmp], capture_output=True, timeout=600)
        if not os.path.exists(tmp):
            return [], []
        with wave.open(tmp, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        try:
            os.remove(tmp)
        except Exception:
            pass
        x = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
        win = max(1, int(sr * self.GRID / 1000.0))
        n = len(x) // win
        if n < 2:
            return [], []
        blocks = x[:n * win].reshape(n, win)
        rms = np.sqrt((blocks ** 2).mean(axis=1))
        ref = float(np.percentile(rms, 95)) or 1e-6
        vals = np.clip(rms / ref * 100.0, 0, 100)
        ts = [i * self.GRID for i in range(n)]
        return ts, vals.tolist()

    def set_mix(self, mix):
        self.mix = max(0.0, min(1.0, mix))
        self._rebuild()

    def _rebuild(self):
        import numpy as np
        if not self.ts:
            self.lv = []
            return
        mo = np.array(self.motion)
        au = np.array(self.audio)
        fused = mo * self.mix + au * (1 - self.mix)
        # 平滑 + 限速
        out, acc, prev = [], 0.0, 0.0
        for v in fused:
            acc = acc * 0.45 + float(v) * 0.55
            v = min(prev + 10, max(prev - 10, acc))
            out.append(v)
            prev = v
        self.lv = out

    def level_at(self, ms):
        if not self.ts:
            return 0
        import bisect
        i = bisect.bisect_right(self.ts, ms) - 1
        if i < 0:
            return int(self.lv[0])
        if i >= len(self.ts) - 1:
            return int(self.lv[-1])
        t0, t1 = self.ts[i], self.ts[i + 1]
        v0, v1 = self.lv[i], self.lv[i + 1]
        if t1 <= t0:
            return int(v1)
        r = (ms - t0) / (t1 - t0)
        return int(v0 + (v1 - v0) * r)


class MicLevel:
    """麦克风实时电平 -> 0-100。用于音乐/声音联动。"""

    def __init__(self):
        self.level = 0.0
        self.sens = 1.0
        self.stream = None
        self.err = ""

    def start(self):
        try:
            import sounddevice as sd
        except Exception as e:
            self.err = f"缺少 sounddevice：{e}"
            return False
        def cb(indata, frames, t, status):
            x = indata[:, 0]
            rms = float((x * x).mean()) ** 0.5
            db = 20 * __import__("math").log10(rms + 1e-9)
            v = (db + 55) / 45 * 100
            v = max(0.0, min(100.0, v)) * self.sens
            v = max(0.0, min(100.0, v))
            self.level = self.level * 0.5 + v * 0.5

        for ch in (1, 2):
            try:
                self.stream = sd.InputStream(channels=ch, blocksize=1024, callback=cb)
                self.stream.start()
                return True
            except Exception as e:
                self.err = f"{type(e).__name__}: {e}"
        return False

    def stop(self):
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        self.level = 0.0


class VoiceCmd:
    """离线中文语音识别（vosk），识别到文本后回调 on_text。"""

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.rec = None
        self.stream = None
        self.thread = None
        self.running = False
        self.err = ""
        self._last = ""
        self.on_text = None

    def model_path(self):
        import os
        root = os.path.join(self.base_dir, "model")
        if not os.path.isdir(root):
            return None
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if os.path.isdir(p) and d.startswith("vosk-model"):
                return p
        return None

    def start(self):
        import queue as _q
        import threading as _th
        try:
            import vosk
            import sounddevice as sd
        except Exception as e:
            self.err = f"缺少依赖：{e}"
            return False
        mp = self.model_path()
        if not mp:
            self.err = "未找到 model 目录下的 vosk 中文模型"
            return False
        try:
            vosk.SetLogLevel(-1)
            model = vosk.Model(mp)
        except Exception as e:
            self.err = f"模型加载失败：{e}"
            return False
        self.rec = vosk.KaldiRecognizer(model, 16000)
        self._q = _q.Queue()

        def cb(indata, frames, t, status):
            self._q.put(bytes(indata))

        try:
            self.stream = sd.RawInputStream(samplerate=16000, blocksize=4000,
                                            dtype="int16", channels=1, callback=cb)
            self.stream.start()
        except Exception as e:
            self.err = f"麦克风启动失败：{e}"
            return False
        self.running = True
        self.thread = _th.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def _loop(self):
        import json
        while self.running:
            try:
                data = self._q.get(timeout=0.5)
            except Exception:
                continue
            try:
                if self.rec.AcceptWaveform(data):
                    txt = json.loads(self.rec.Result()).get("text", "")
                else:
                    txt = json.loads(self.rec.PartialResult()).get("partial", "")
            except Exception:
                continue
            txt = (txt or "").replace(" ", "")
            if txt and txt != self._last:
                self._last = txt
                if self.on_text:
                    self.on_text(txt)

    def stop(self):
        self.running = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        self._last = ""


class BleWorker:
    """独占一个 asyncio 事件循环；保活流与连接管理都在这里。"""

    def __init__(self, q):
        self.q = q
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.chr = None
        self.connected = False
        self.busy = False
        self.target = [0, 0, 0, 0, 0]
        self.streaming = False
        self._stream_fut = None
        self._lock = asyncio.Lock()
        self._last_write = 0.0
        self.min_gap = 0.18          # 实测：突发下发会导致设备丢帧
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def post(self, kind, **kw):
        self.q.put((kind, kw))

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- 保活流 ----
    def set_target(self, vals):
        self.target = [max(0, min(100, int(v))) for v in (list(vals) + [0] * 5)[:5]]

    def start_stream(self):
        if self.streaming:
            return
        self.streaming = True
        self._stream_fut = asyncio.run_coroutine_threadsafe(self._stream(), self.loop)

    def stop_stream(self):
        self.streaming = False

    async def _stream(self):
        while self.streaming:
            if self.connected:
                await self._write(f_channels(self.target), quiet=True)
            await asyncio.sleep(STREAM_MS / 1000)

    # ---- 连接 ----
    async def connect_loop(self):
        if self.busy:
            return
        self.busy = True
        try:
            while not self.connected:
                await self._try_once()
                if not self.connected:
                    await asyncio.sleep(6)
        finally:
            self.busy = False

    async def _try_once(self):
        self.post("status", text="扫描中…")
        try:
            found = await BleakScanner.discover(timeout=6.0, return_adv=True)
        except Exception as e:
            self.post("status", text=f"扫描失败：{e}")
            return
        cands = []
        for addr, (dev, adv) in found.items():
            nm = adv.local_name or dev.name or ""
            if NAME_HINT in nm.lower():
                cands.append((adv.rssi or -999, addr, nm))
        if cands:
            cands.sort(reverse=True)
            rssi, addr, nm = cands[0]
            self.post("log", text=f"发现 {nm} ({addr}) RSSI {rssi}")
        else:
            addr, rssi, nm = KNOWN_ADDR, None, "mizzzee"
            self.post("status", text="未扫描到，尝试已知地址…")
        await self._connect(addr, rssi, nm)

    async def _connect(self, addr, rssi, nm):
        try:
            self.post("status", text=f"连接 {addr} …")
            self.client = BleakClient(addr, timeout=15.0)
            await self.client.connect()
            self.chr = None
            for s in self.client.services:
                for ch in s.characteristics:
                    if ch.uuid.lower().startswith("0000ffe1"):
                        self.chr = ch.uuid
            if not self.chr:
                for s in self.client.services:
                    for ch in s.characteristics:
                        if "write" in ch.properties:
                            self.chr = ch.uuid
                            break
                    if self.chr:
                        break
            await self.client.start_notify(self.chr, self._on_notify)
            self.connected = True
            self.post("log", text=f"已连接，写入特征 {self.chr}")
            self.post("state", connected=True, name=nm or "mizzzee", addr=addr, rssi=rssi)
            self.post("status", text="已连接")
            await asyncio.sleep(0.3)
            await self._write(f_zero(), label="SYNC 0", quiet=True)
        except Exception as e:
            self.connected = False
            self.post("status", text=f"连接失败：{type(e).__name__}")
            self.post("log", text=f"连接失败：{type(e).__name__}: {e}")

    def _on_notify(self, _h, data: bytearray):
        d = bytes(data)
        dec = decode(d)
        if dec:
            cmd, payload, _ok = dec
            if cmd == 0x01 and payload:
                self.post("battery", cap=payload[0])
            if cmd in (0x07, 0x09):
                self.post("ack", cmd=cmd)
                return
            self.post("log", text=f"RX  {d.hex(' ')}   cmd=0x{cmd:02X}")
        else:
            self.post("log", text=f"RX  {d.hex(' ')}")

    async def _write(self, data, label="", quiet=False):
        if not (self.client and self.client.is_connected):
            return
        # 帧率保护：两次写入至少间隔 min_gap，避免刷爆固件
        gap = self.min_gap - (time.perf_counter() - self._last_write)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            await self.client.write_gatt_char(self.chr, data, response=False)
            self._last_write = time.perf_counter()
            if not quiet:
                self.post("log", text=f"TX  {data.hex(' ')}   {label}")
        except Exception as e:
            self.post("log", text=f"发送失败：{type(e).__name__}: {e}")
            self.connected = False
            self.post("state", connected=False)

    async def send(self, data, label=""):
        await self._write(data, label)

    async def hard_stop(self):
        self.streaming = False
        await asyncio.sleep(0.05)
        for _ in range(STOP_REPEAT):
            await self._write(f_zero(), label="STOP", quiet=True)
            await asyncio.sleep(0.15)
        self.post("log", text="已下发停止帧 x%d" % STOP_REPEAT)

    async def recover(self):
        """设备假死时：停止 -> 断开 -> 重连。"""
        await self.hard_stop()
        try:
            if self.client:
                await self.client.disconnect()
        except Exception:
            pass
        self.connected = False
        self.post("state", connected=False)
        self.post("log", text="已断开，准备重连")
        await asyncio.sleep(1.0)
        await self.connect_loop()

    async def disconnect(self):
        self.streaming = False
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.connected = False


class Ring(QWidget):
    def __init__(self):
        super().__init__()
        self.level = 0
        self.maxv = 254
        self.setMinimumSize(230, 230)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_level(self, v):
        self.level = max(0, min(100, int(v)))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 24
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        pen = QPen(QColor("#21262d"), 12)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)
        if self.level > 0:
            pen = QPen(QColor("#7cf03d"), 12)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 90 * 16, -int(360 * 16 * self.level / 100))
        p.setPen(QColor("#e6edf3"))
        f = QFont("Microsoft YaHei UI", int(side * 0.2))
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect.adjusted(0, -side * 0.06, 0, -side * 0.06),
                   Qt.AlignCenter, str(self.level))
        p.setPen(QColor("#8b949e"))
        p.setFont(QFont("Microsoft YaHei UI", int(side * 0.05)))
        p.drawText(rect.adjusted(0, side * 0.26, 0, side * 0.26),
                   Qt.AlignCenter, f"/ {self.maxv}")


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.ble = BleWorker(self.q)

        self.connected = False
        self.range_max = 254        # 实测设备上限；100 = 与原版 App 一致
        self.playing = False
        self.pattern = [[40, 40, 40, 40, 0]]
        self.idx = 0
        self.ch = [0, 0, 0, 0, 0]
        self.start_ts = None
        self.limit_min = 15
        self.active_skill = None
        self.active_wave = None
        self.heat_on = False
        self.mic = MicLevel()
        self.mic_on = False
        self.voice = VoiceCmd(os.path.dirname(os.path.abspath(sys.argv[0])))
        self.voice.on_text = lambda t: self.q.put(("voice", {"text": t}))
        self.voice_on = False
        self._last_voice = ("", 0.0)
        self.video = VideoEngine()
        self.vcap = None
        self.video_playing = False
        self.video_timer = QTimer(self)
        self.video_timer.timeout.connect(self._on_video_tick)
        self.heat_auto_done = False
        self.ai = AIClient()
        self.ai_buf = []
        self.ai_playing = False
        self.ai_busy = False
        self.ai_style = "渐进"
        self._fw_timer = QTimer(self)
        self._fw_timer.timeout.connect(self._on_fw_tick)
        self.ai_timer = QTimer(self)
        self.ai_timer.setSingleShot(True)
        self.ai_timer.timeout.connect(self._ai_tick)

        self.setWindowTitle("醉清风 · PC 控制台")
        self.resize(560, 900)
        self.setStyleSheet(QSS)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)

        v.addWidget(self._header())
        self.ring = Ring()
        v.addWidget(self.ring, 1)
        self.lbl_state = QLabel("等待连接…", objectName="state")
        self.lbl_state.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lbl_state)
        v.addWidget(self._tabs())
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_skill())
        self.stack.addWidget(self._page_wave())
        self.stack.addWidget(self._page_link())
        self.stack.addWidget(self._page_video())
        self.stack.addWidget(self._page_ai())
        self.stack.addWidget(self._page_manual())
        v.addWidget(self.stack)
        v.addLayout(self._controls())
        v.addWidget(self._footer())
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setFixedHeight(80)
        v.addWidget(self.log)

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._on_tick)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._on_clock)
        self.pump = QTimer(self)
        self.pump.timeout.connect(self._pump)
        self.pump.start(80)

        self.player = QMediaPlayer(self)
        self.aout = QAudioOutput(self)
        self.player.setAudioOutput(self.aout)
        self.player.setVideoOutput(self.vw)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self._seeking = False
        self._vgain = 1.0

        self.mic_timer = QTimer(self)
        self.mic_timer.timeout.connect(self._on_mic_tick)
        self._set_enabled(False)

        self.ble.submit(self.ble.connect_loop())

    # ---------- 构建 ----------
    def _header(self):
        card = QFrame(objectName="card")
        h = QHBoxLayout(card)
        h.setContentsMargins(16, 10, 16, 10)
        left = QVBoxLayout()
        self.lbl_name = QLabel("未连接", objectName="title")
        self.lbl_addr = QLabel("正在搜索设备…", objectName="sub")
        left.addWidget(self.lbl_name)
        left.addWidget(self.lbl_addr)
        h.addLayout(left)
        h.addStretch()
        self.lbl_bat = QLabel("—", objectName="metricDim")
        self.lbl_sig = QLabel("—", objectName="metricDim")
        h.addWidget(self.lbl_bat)
        h.addWidget(self.lbl_sig)
        self.btn_link = QPushButton("重连", objectName="power")
        self.btn_link.clicked.connect(self.on_reconnect)
        self.btn_recover = QPushButton("恢复", objectName="power")
        self.btn_recover.clicked.connect(self.on_recover)
        self.btn_power = QPushButton("电源", objectName="power")
        self.btn_power.clicked.connect(self.on_power)
        self.btn_cfg = QPushButton("配置", objectName="power")
        self.btn_cfg.setToolTip("填入 App token 拉取服务端机型配置")
        self.btn_cfg.clicked.connect(self.on_open_cfg)
        h.addWidget(self.btn_link)
        h.addWidget(self.btn_recover)
        h.addWidget(self.btn_power)
        h.addWidget(self.btn_cfg)
        return card

    def _tabs(self):
        box = QFrame(objectName="cardSoft")
        h = QHBoxLayout(box)
        h.setContentsMargins(6, 6, 6, 6)
        self.tabs = []
        for i, t in enumerate(["技能", "变频", "语音", "视频", "AI", "手动"]):
            b = QPushButton(t, objectName="tab")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.clicked.connect(lambda _=False, n=i: self.switch(n))
            h.addWidget(b)
            self.tabs.append(b)
        return box

    def _page_skill(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("档位", objectName="hint"))
        self.skill_level = QSlider(Qt.Horizontal)
        self.skill_level.setRange(0, self.range_max)
        self.skill_level.setValue(178)          # ≈ 原 70% 的物理等效值
        self.skill_level.setFixedWidth(150)
        self.skill_lbl = QLabel(str(self.skill_level.value()), objectName="val")
        self.skill_lbl.setFixedWidth(30)
        self.skill_level.valueChanged.connect(lambda v: self.skill_lbl.setText(str(v)))
        row.addWidget(self.skill_level)
        row.addWidget(self.skill_lbl)
        row.addStretch()
        v.addLayout(row)

        g = QGridLayout()
        g.setSpacing(8)
        self.skill_btns = {}
        for i, (name, _, _) in enumerate(SKILLS):
            b = QPushButton(name, objectName="skill")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=name: self.on_skill(n))
            g.addWidget(b, 0, i)
            g.setColumnStretch(i, 1)
            self.skill_btns[name] = b
        v.addLayout(g)

        v.addWidget(QLabel("抽动模式", objectName="hint"))
        g2 = QGridLayout()
        g2.setSpacing(6)
        self.mode_btns = {}
        for i, (name, _fn) in enumerate(THRUST_MODES):
            b = QPushButton(name, objectName="wave")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=name: self.on_thrust(n))
            g2.addWidget(b, i // 5, i % 5)
            g2.setColumnStretch(i % 5, 1)
            self.mode_btns[name] = b
        v.addLayout(g2)

        v.addWidget(QLabel("固件模式（APK 原生指令，部分机型支持）", objectName="hint"))
        g3 = QGridLayout()
        g3.setSpacing(6)
        self.fw_btns = {}
        for i, (name, hexs) in enumerate(FW_MODES):
            b = QPushButton(name, objectName="wave")
            b.setCheckable(True)
            b.setToolTip(f"{hexs.upper()}")
            b.clicked.connect(lambda _=False, n=name, hx=hexs: self.on_fw_mode(n, hx))
            g3.addWidget(b, i // 5, i % 5)
            g3.setColumnStretch(i % 5, 1)
            self.fw_btns[name] = b
        v.addLayout(g3)
        v.addStretch()
        return w

    def on_fw_mode(self, name, hexs):
        data = bytes.fromhex(hexs)
        for n, b in self.fw_btns.items():
            b.setChecked(n == name)
        for b in list(self.skill_btns.values()) + list(self.mode_btns.values()):
            b.setChecked(False)
        if not self.connected:
            self._append("未连接设备")
            return
        # 固件模式是「一次性触发」，按 300ms 重发以覆盖不同固件的判定窗口
        self._fw = (name, data)
        self._fw_tick = 0
        self.ble.submit(self.ble.send(data, f"FW {name}"))
        self._append(f"固件模式：{name} 帧 {hexs.upper()}")
        if not self._fw_timer.isActive():
            self._fw_timer.start(300)

    def _on_fw_tick(self):
        if not getattr(self, "_fw", None) or not self.connected:
            self._fw_timer.stop()
            return
        self._fw_tick += 1
        if self._fw_tick > 20:          # 约 6 秒后自动停
            self._fw_timer.stop()
            self._fw = None
            for b in self.fw_btns.values():
                b.setChecked(False)
            return
        self.ble.submit(self.ble.send(self._fw[1], f"FW {self._fw[0]}"))

    def _scaled(self, levels):
        """把预设曲线缩放到当前档位（峰值 = 档位）。"""
        cap = self.skill_level.value() if hasattr(self, "skill_level") else 100
        if cap <= 0:
            return [0] * len(levels)
        m = max(levels) or 1
        f = cap / float(m)
        return [max(0, min(100, int(round(v * f)))) for v in levels]

    def on_thrust(self, name):
        fn = dict(THRUST_MODES).get(name)
        if not fn:
            return
        levels = self._scaled(fn())
        for n, b in self.mode_btns.items():
            b.setChecked(n == name)
        for b in self.skill_btns.values():
            b.setChecked(False)
        self._append(f"抽动模式：{name}（{len(levels)} 步）")
        self._start([[v, v, v, v, 0] for v in levels], 300)

    def _page_wave(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        holder = QWidget()
        g = QGridLayout(holder)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(6)
        self.wave_btns = {}
        for i, (name, tip, _fn) in enumerate(WAVE_DEFS):
            b = QPushButton(name, objectName="wave")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, n=name: self.on_wave(n))
            g.addWidget(b, i // 5, i % 5)
            g.setColumnStretch(i % 5, 1)
            self.wave_btns[name] = b

        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(212)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{border:none;background:#0d1117;}"
                             "QScrollBar:vertical{background:#0d1117;width:6px;}"
                             "QScrollBar::handle:vertical{background:#30363d;border-radius:3px;}")
        v.addWidget(scroll)

        row = QHBoxLayout()
        row.addWidget(QLabel("节奏", objectName="hint"))
        self.tempo_btns = {}
        for name, factor in TEMPOS:
            b = QPushButton(name, objectName="tab")
            b.setCheckable(True)
            b.setChecked(name == "中")
            b.clicked.connect(lambda _=False, n=name: self.set_tempo(n))
            row.addWidget(b)
            self.tempo_btns[name] = b
        self.btn_soft = QPushButton("柔化", objectName="tab")
        self.btn_soft.setCheckable(True)
        self.btn_soft.setChecked(True)
        self.btn_soft.setToolTip("限制每步变化幅度，去掉生硬跳变（建议常开）")
        self.btn_soft.clicked.connect(lambda: self._append(
            "柔化 " + ("开启" if self.btn_soft.isChecked() else "关闭")))
        row.addWidget(self.btn_soft)
        row.addStretch()
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("节拍", objectName="hint"))
        for bpm in (60, 90, 120, 150):
            b = QPushButton(f"{bpm}", objectName="wave")
            b.setCheckable(True)
            b.setToolTip(f"{bpm} BPM 四拍节奏")
            b.clicked.connect(lambda _=False, n=bpm: self.on_bpm(n))
            b.setFixedWidth(52)
            row2.addWidget(b)
            setattr(self, f"bpm_btn_{bpm}", b)
        b1 = QPushButton("盲盒", objectName="wave")
        b1.setToolTip("随机挑一个波形，不告诉你是哪个")
        b1.clicked.connect(self.on_gacha)
        b2 = QPushButton("突袭", objectName="wave")
        b2.setToolTip("长时间安静，随机时刻突然爆发")
        b2.clicked.connect(self.on_ambush)
        row2.addWidget(b1)
        row2.addWidget(b2)
        v.addLayout(row2)
        return w

    def _page_link(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 0, 2, 0)
        v.setSpacing(6)

        row1 = QHBoxLayout()
        self.btn_mic = QPushButton("声控", objectName="wave")
        self.btn_mic.setCheckable(True)
        self.btn_mic.setToolTip("麦克风采集音乐/人声，强度跟随声音起伏")
        self.btn_mic.clicked.connect(self.on_mic)
        self.btn_mic.setFixedWidth(66)
        row1.addWidget(self.btn_mic)

        self.mic_bar = QSlider(Qt.Horizontal)
        self.mic_bar.setRange(0, self.range_max)
        self.mic_bar.setEnabled(False)
        row1.addWidget(self.mic_bar, 1)

        row1.addWidget(QLabel("灵敏", objectName="hint"))
        self.sens = QSlider(Qt.Horizontal)
        self.sens.setRange(20, 300)
        self.sens.setValue(100)
        self.sens.setFixedWidth(80)
        self.sens.valueChanged.connect(self.on_sens)
        row1.addWidget(self.sens)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_voice = QPushButton("语音控制", objectName="wave")
        self.btn_voice.setCheckable(True)
        self.btn_voice.setToolTip("说出关键词控制：快点 / 慢点 / 停 / 重点 / 轻点 / 好爽")
        self.btn_voice.clicked.connect(self.on_voice)
        self.btn_voice.setFixedWidth(86)
        row2.addWidget(self.btn_voice)
        _mp = self.voice.model_path() if hasattr(self, "voice") else None
        self.lbl_voice = QLabel("模型已就绪，点左侧开启" if _mp else "模型未找到：需 model 文件夹",
                                objectName="hint")
        row2.addWidget(self.lbl_voice, 1)
        v.addLayout(row2)

        grid = QGridLayout()
        grid.setContentsMargins(4, 6, 4, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        rows = [
            ("快点 / 快一点 / 提高", "强度 +10"),
            ("慢点 / 轻一点 / 降低", "强度 −10"),
            ("重点 / 用力 / 最大", "直接满档 95"),
            ("好爽 / 舒服 / 别停", "强度 +8"),
            ("停 / 停止 / 够了", "立即停止"),
            ("加热 / 热一点", "开启加热"),
        ]
        for i, (k, e) in enumerate(rows):
            grid.addWidget(QLabel(k, objectName="chan"), i, 0)
            grid.addWidget(QLabel(e, objectName="val"), i, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        v.addStretch()
        return w

    def _page_video(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 0, 2, 0)
        v.setSpacing(6)

        row = QHBoxLayout()
        self.btn_pick = QPushButton("选择视频", objectName="wave")
        self.btn_pick.clicked.connect(self.on_pick_video)
        self.btn_vplay = QPushButton("▶ 播放", objectName="wave")
        self.btn_vplay.setCheckable(True)
        self.btn_vplay.clicked.connect(self.on_video_play)
        self.btn_vstop = QPushButton("停止", objectName="wave")
        self.btn_vstop.clicked.connect(self.on_video_stop)
        self.btn_vfull = QPushButton("全屏", objectName="wave")
        self.btn_vfull.setToolTip("全屏播放（ESC 或双击退出）")
        self.btn_vfull.clicked.connect(self.on_video_fullscreen)
        row.addWidget(self.btn_pick, 3)
        row.addWidget(self.btn_vplay, 3)
        row.addWidget(self.btn_vstop, 2)
        row.addWidget(self.btn_vfull, 2)
        v.addLayout(row)

        self.vw = FsVideo()
        self.vw.setMinimumHeight(150)
        self.vw.setToolTip("双击切换全屏，全屏时按 ESC 退出")
        self.vw.setStyleSheet("background:#000;border-radius:8px;")
        v.addWidget(self.vw, 1)

        self.vslider = QSlider(Qt.Horizontal)
        self.vslider.setRange(0, 1000)
        self.vslider.setValue(0)
        self.vslider.sliderMoved.connect(self.on_video_seek)
        self.vslider.sliderReleased.connect(self.on_video_seek_end)
        v.addWidget(self.vslider)

        self.vprog = QLabel("未加载视频", objectName="hint")
        v.addWidget(self.vprog)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("玩法", objectName="hint"))
        self.mix_btns = {}
        for name, mix in (("动作优先", 0.75), ("均衡", 0.55), ("声音优先", 0.25), ("只看声音", 0.0)):
            b = QPushButton(name, objectName="wave")
            b.setCheckable(True)
            b.setChecked(name == "均衡")
            b.clicked.connect(lambda _=False, m=mix, n=name: self.set_mix(n, m))
            row2.addWidget(b)
            self.mix_btns[name] = b
        v.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("灵敏", objectName="hint"))
        self.vgain = QSlider(Qt.Horizontal)
        self.vgain.setRange(50, 200)
        self.vgain.setValue(100)
        self.vgain.setFixedWidth(110)
        self.vgain.valueChanged.connect(self.on_vgain)
        row3.addWidget(self.vgain)
        row3.addStretch()
        v.addLayout(row3)
        return w

    def _page_ai(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 0, 2, 0)
        v.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("风格", objectName="hint"))
        self.ai_btns = {}
        for name in AI_STYLES:
            b = QPushButton(name, objectName="wave")
            b.setCheckable(True)
            b.setChecked(name == "渐进")
            b.clicked.connect(lambda _=False, n=name: self.set_ai_style(n))
            row.addWidget(b)
            self.ai_btns[name] = b
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("上限", objectName="hint"))
        self.ai_cap = QSlider(Qt.Horizontal)
        self.ai_cap.setRange(30, self.range_max)
        self.ai_cap.setValue(216)               # ≈ 原 85% 的物理等效值
        self.ai_cap.setFixedWidth(120)
        self.ai_caplbl = QLabel(str(self.ai_cap.value()), objectName="val")
        self.ai_cap.valueChanged.connect(lambda v: self.ai_caplbl.setText(str(v)))
        row2.addWidget(self.ai_cap)
        row2.addWidget(self.ai_caplbl)
        row2.addStretch()
        self.btn_aitest = QPushButton("测延迟", objectName="wave")
        self.btn_aitest.clicked.connect(self.on_ai_test)
        row2.addWidget(self.btn_aitest)
        v.addLayout(row2)

        self.ai_stat = QLabel("模型 deepseek-v4-flash · 未开始", objectName="hint")
        self.ai_stat.setWordWrap(True)
        v.addWidget(self.ai_stat)

        row3 = QHBoxLayout()
        self.btn_aistart = QPushButton("▶  让 AI 接管", objectName="play")
        self.btn_aistart.clicked.connect(self.on_ai_start)
        self.btn_aistop = QPushButton("■  停止", objectName="stop")
        self.btn_aistop.clicked.connect(self.on_ai_stop)
        row3.addWidget(self.btn_aistart, 3)
        row3.addWidget(self.btn_aistop, 2)
        v.addLayout(row3)
        v.addStretch()
        return w

    def _page_manual(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 2, 4, 0)
        v.setSpacing(6)
        row = QHBoxLayout()
        lb = QLabel("强度", objectName="chan")
        lb.setFixedWidth(38)
        self.slider = QSlider(Qt.Horizontal)
        self.range_max = 254
        self.slider.setRange(0, self.range_max)
        self.slider.setValue(0)
        self.slider.valueChanged.connect(self.on_slider)
        self.val_label = QLabel("0", objectName="val")
        self.val_label.setFixedWidth(46)
        self.val_label.setAlignment(Qt.AlignRight)
        row.addWidget(lb)
        row.addWidget(self.slider, 1)
        row.addWidget(self.val_label)
        v.addLayout(row)

        row_r = QHBoxLayout()
        self.btn_range = QPushButton("量程 0-254", objectName="wave")
        self.btn_range.setCheckable(True)
        self.btn_range.setChecked(True)
        self.btn_range.setToolTip("实测设备接受 0-254；切到 0-100 与原版 App 一致")
        self.btn_range.setFixedSize(104, 34)
        self.btn_range.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_range.clicked.connect(self.on_toggle_range)
        self.ring.maxv = 254
        row_r.addWidget(self.btn_range)
        row_r.addWidget(QLabel("提示：254 为设备上限，255 会被拒收", objectName="hint"))
        row_r.addStretch()
        v.addLayout(row_r)
        tip = QLabel("拖动即生效，松手后保持输出；归零即停止", objectName="hint")
        v.addWidget(tip)
        v.addStretch()
        self.sliders = [self.slider]
        self.val_labels = [self.val_label]
        return w

    def _controls(self):
        h = QHBoxLayout()
        self.btn_play = QPushButton("▶  开始玩", objectName="play")
        self.btn_play.clicked.connect(self.on_play)
        self.btn_stop = QPushButton("■  停止", objectName="stop")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_heat = QPushButton("加热", objectName="power")
        self.btn_heat.setCheckable(True)
        self.btn_heat.clicked.connect(self.on_heat)
        h.addWidget(self.btn_play, 3)
        h.addWidget(self.btn_stop, 2)
        h.addWidget(self.btn_heat, 1)
        return h

    def _footer(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        self.lbl_time = QLabel("运行时长 00:00", objectName="sub")
        self.lbl_status = QLabel("", objectName="sub")
        h.addWidget(self.lbl_time)
        h.addStretch()
        h.addWidget(self.lbl_status)
        return w

    # ---------- 状态 ----------
    def _s(self, v):
        """逻辑强度(0-100) -> 物理输出(0-range_max)"""
        return int(round(max(0.0, min(100.0, v)) * self.range_max / 100.0))

    def _set_enabled(self, on):
        for b in [self.btn_play, self.btn_stop, self.btn_power] + self.tabs:
            b.setEnabled(on)
        for s in self.sliders:
            s.setEnabled(on)

    def _pump(self):
        try:
            while True:
                kind, kw = self.q.get_nowait()
                if kind == "log":
                    self._append(kw["text"])
                elif kind == "status":
                    self.lbl_status.setText(kw["text"])
                elif kind == "state":
                    self._on_state(kw)
                elif kind == "aistat":
                    self.ai_stat.setText(kw["text"])
                elif kind == "aisteps":
                    self.ai_busy = False
                    self.ai_buf.extend(kw["steps"])
                    if not kw["steps"]:
                        self._append("AI 返回空脚本")
                elif kind == "aierr":
                    self.ai_busy = False
                    self._append(f"AI 请求失败：{kw['text'][:120]}")
                    if not self.ai_buf and self.ai_playing:
                        self.ai_buf = self._ai_fallback()
                        self._append("已切到本地兜底节奏")
                elif kind == "vload":
                    self.player.setSource(QUrl.fromLocalFile(kw["path"]))
                    self.vprog.setText(kw["text"])
                elif kind == "vprog":
                    self.vprog.setText(kw["text"])
                elif kind == "vlog":
                    self._append(kw["text"])
                elif kind == "voice":
                    self._handle_voice(kw["text"])
                elif kind == "battery":
                    cap = kw["cap"]
                    self.lbl_bat.setText(f"{cap}%")
                    self.lbl_bat.setStyleSheet(
                        f"color:{'#7cf03d' if cap > 30 else '#f85149'};"
                        "font-family:Consolas;font-size:13px;")
        except queue.Empty:
            pass

    def _append(self, text):
        self.log.appendPlainText(f"[{dt.datetime.now():%H:%M:%S}] {text}")

    def _on_state(self, kw):
        self.connected = kw["connected"]
        self._set_enabled(self.connected)
        if self.connected:
            self.lbl_name.setText(kw.get("name") or "mizzzee")
            self.lbl_addr.setText(kw.get("addr", ""))
            rssi = kw.get("rssi")
            if rssi:
                pct = max(0, min(100, int((rssi + 100) * 2)))
                self.lbl_sig.setText(f"{pct}%")
                self.lbl_sig.setStyleSheet(
                    "color:#7cf03d;font-family:Consolas;font-size:13px;")
            self.lbl_state.setText("已连接 · 待机")
            if not self.heat_auto_done:
                self.heat_auto_done = True
                self.heat_on = True
                self.btn_heat.setChecked(True)
                self.ble.submit(self.ble.send(frame(CMD_HEAT, [1]), "HEAT AUTO ON"))
                self._append("已自动开启加热")
        else:
            self.lbl_name.setText("未连接")
            self.lbl_addr.setText("正在重试…")
            self.playing = False
            self.tick.stop()
            self.clock.stop()

    # ---------- 交互 ----------
    def switch(self, idx):
        for i, b in enumerate(self.tabs):
            b.setChecked(i == idx)
        self.stack.setCurrentIndex(idx)

    def on_reconnect(self):
        self._append("手动重连…")
        self.ble.connected = False
        self.ble.submit(self.ble.connect_loop())

    def on_recover(self):
        self._append("执行恢复：停止 → 断开 → 重连")
        self.on_stop()
        self.ble.submit(self.ble.recover())

    def on_heat(self):
        self.heat_on = not self.heat_on
        self.btn_heat.setChecked(self.heat_on)
        self.ble.submit(self.ble.send(frame(CMD_HEAT, [1 if self.heat_on else 0]),
                                      f"HEAT {'ON' if self.heat_on else 'OFF'}"))
        self._append(f"加热 {'开启' if self.heat_on else '关闭'}")

    def on_toggle_range(self):
        ext = self.btn_range.isChecked()
        self.range_max = 254 if ext else 100
        self.ring.maxv = self.range_max
        self.ring.update()
        self.btn_range.setText(f"量程 0-{self.range_max}")
        cur = self.slider.value()
        self.slider.setRange(0, self.range_max)
        self.slider.setValue(min(cur, self.range_max))

    def on_slider(self, val):
        # 手动操作时只停节奏定时器，不发停止帧（否则零值帧会淹没目标值）
        if self.playing:
            self.tick.stop()
            self.clock.stop()
            self.playing = False
            self.btn_play.setText("▶  开始玩")
        self.ch = [val, 0, 0, 0, 0]
        self.val_label.setText(str(val))
        self.ring.set_level(val)
        self.ble.set_target(self.ch)
        if val > 0:
            self.ble.start_stream()
            self.lbl_state.setText("手动输出中")
        else:
            self.ble.stop_stream()
            self.lbl_state.setText("已停止")

    def on_open_cfg(self):
        ConfigDlg(self).exec()

    def on_power(self):
        if self.playing or self.ble.streaming:
            self.on_stop()
        else:
            lv = max(self.ch) or 40
            self._start([[lv, lv, lv, lv, 0]], 500)

    def on_play(self):
        if not self.connected:
            self._append("未连接设备")
            return
        if self.playing:
            self.on_stop()
            return
        idx = self.stack.currentIndex()
        if idx == 0:
            self.on_skill(SKILLS[0][0])
        elif idx == 1:
            self.on_wave(WAVE_DEFS[0][0])
        elif idx == 2:
            self.on_mic()
            return
        elif idx == 3:
            self.on_video_play()
            return
        elif idx == 4:
            self.on_ai_start()
            return
        else:
            lv = max(self.ch) or 40
            self._start([[lv, lv, lv, lv, 0]], 500)

    def _mark(self, kind, name):
        for n, b in (self.skill_btns.items() if kind == "skill" else self.wave_btns.items()):
            b.setChecked(n == name)
        self.active_skill = name if kind == "skill" else None
        self.active_wave = name if kind == "wave" else None

    def on_skill(self, name):
        for n, levels, ms in SKILLS:
            if n == name:
                self._mark("skill", name)
                levels = self._scaled(levels)
                self._append(f"技能：{name}  档位 {self.skill_level.value()}  序列 {levels}")
                self._start([[lv, lv, lv, lv, 0] for lv in levels], ms)
                return

    def set_tempo(self, name):
        for n, b in self.tempo_btns.items():
            b.setChecked(n == name)
        self.tempo_name = name
        if self.playing and self.active_wave:
            self.on_wave(self.active_wave)

    def on_wave(self, name):
        fn = dict((n, f) for n, _t, f in WAVE_DEFS).get(name)
        if not fn:
            return
        levels = fn()
        factor = dict(TEMPOS).get(getattr(self, "tempo_name", "中"), 1)
        if factor < 1:
            levels = levels[::2] or levels
        elif factor > 1:
            levels = [x for x in levels for _ in range(int(factor))]
        if getattr(self, "btn_soft", None) and self.btn_soft.isChecked():
            levels = soften(levels, max_step=20)
        self._mark("wave", name)
        self._append(f"变频：{name}（节奏 {getattr(self, 'tempo_name', '中')}，{len(levels)} 步）")
        self._start([[lv, lv, lv, lv, 0] for lv in levels], 250)

    def on_mic(self):
        if self.mic_on:
            self._stop_mic()
            self._append("声控已关闭")
            return
        if not self.mic.start():
            self._append(f"麦克风启动失败：{self.mic.err}")
            self.btn_mic.setChecked(False)
            return
        self.mic_on = True
        self.btn_mic.setChecked(True)
        self.tick.stop()
        self.clock.stop()
        self.playing = True
        self.start_ts = dt.datetime.now()
        self.btn_play.setText("❚❚  暂停")
        self.lbl_state.setText("声控联动中")
        self.mic_timer.start(100)
        self.clock.start(1000)
        self._append("声控已开启：强度跟随环境声音，播放音乐试试")

    def _stop_mic(self):
        self.mic_on = False
        self.btn_mic.setChecked(False)
        self.mic_timer.stop()
        self.mic.stop()
        self.mic_bar.setValue(0)

    def on_sens(self, v):
        self.mic.sens = v / 100.0

    def _on_mic_tick(self):
        lv = self._s(self.mic.level)
        self.mic_bar.setValue(lv)
        if not (self.mic_on and self.connected):
            return
        self.ch = [lv, lv, lv, lv, 0]
        self.ring.set_level(lv)
        self.ble.set_target(self.ch)
        if not self.ble.streaming:
            self.ble.start_stream()

    def on_voice(self):
        if self.voice_on:
            self.voice.stop()
            self.voice_on = False
            self.btn_voice.setChecked(False)
            self.lbl_voice.setText("语音控制已关闭")
            self._append("语音控制已关闭")
            return
        if not self.voice.start():
            self.btn_voice.setChecked(False)
            self.lbl_voice.setText(self.voice.err or "启动失败")
            self._append(f"语音控制启动失败：{self.voice.err}")
            return
        self.voice_on = True
        self.btn_voice.setChecked(True)
        self.lbl_voice.setText("正在听…")
        self._append("语音控制已开启，模型已加载")

    def _set_level(self, lv):
        lv = max(0, min(self.range_max, int(lv)))
        if not self.connected:
            return
        self.ch = [lv, lv, lv, lv, 0]
        self.slider.blockSignals(True)
        self.slider.setValue(lv)
        self.slider.blockSignals(False)
        self.val_label.setText(str(lv))
        self.ring.set_level(lv)
        self.ble.set_target(self.ch)
        if lv > 0:
            self.ble.start_stream()
        else:
            self.ble.stop_stream()

    def _handle_voice(self, text):
        import time as _t
        self.lbl_voice.setText(f"听到：{text}")
        groups = [
            (("别停", "不要停", "好爽", "舒服", "继续", "再来"), "boost"),
            (("停止", "停", "够了", "行了", "不要"), "stop"),
            (("重点", "用力", "大力", "最大", "最快", "最重"), "max"),
            (("轻点", "轻一点", "温柔", "轻", "慢点", "慢一点", "降低",
              "低一点", "小一点", "减速", "慢"), "down"),
            (("快点", "快一点", "提高", "高一点", "大一点", "加强",
              "加速", "快"), "up"),
            (("加热", "热一点", "好热"), "heat"),
        ]
        act = None
        for words, a in groups:
            if any(w in text for w in words):
                act = a
                break
        if not act:
            return
        now = _t.time()
        if self._last_voice[0] == act and now - self._last_voice[1] < 1.2:
            return
        self._last_voice = (act, now)

        step = self._s(10)
        if act == "stop":
            self.on_stop()
            self._append("语音：停止")
        elif act == "max":
            self._set_level(int(self.range_max * 0.95))
            self._append("语音：满档")
        elif act == "soft":
            self._set_level(self._s(30))
            self._append("语音：轻一点 → %d" % self._s(30))
        elif act == "up":
            self._set_level(max(self.ch) + step)
            self._append(f"语音：快点 → {max(self.ch)}")
        elif act == "down":
            self._set_level(max(self.ch) - step)
            self._append(f"语音：慢点 → {max(self.ch)}")
        elif act == "boost":
            self._set_level(max(self.ch) + int(step * 0.8))
            self._append(f"语音：好爽 → {max(self.ch)}")
        elif act == "heat":
            if not self.heat_on:
                self.on_heat()
            self._append("语音：加热")

    def on_pick_video(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频", "",
            "视频文件 (*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.m4v *.ts);;所有文件 (*.*)")
        if not path:
            return
        self._append(f"选择视频：{path}")
        self.vprog.setText("分析中… 0%")
        import threading

        def work():
            try:
                n = self.video.analyze(
                    path,
                    progress_cb=lambda r: self.q.put(
                        ("vprog", {"text": f"分析中… {r * 100:.0f}%"})),
                    gain=self.vgain.value() / 100.0)
                self.q.put(("vload", {"path": path,
                                      "text": f"就绪：{n} 格 / {self.video.duration:.1f} 秒"}))
                self.q.put(("vlog", {"text": f"视频分析完成，{n} 格曲线"}))
            except Exception as e:
                self.q.put(("vprog", {"text": f"分析失败：{e}"}))
                self.q.put(("vlog", {"text": f"视频分析失败：{e}"}))

        threading.Thread(target=work, daemon=True).start()

    def on_video_play(self):
        if not self.video.ts:
            self._append("请先选择并分析视频")
            self.btn_vplay.setChecked(False)
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.video_timer.stop()
            self.btn_vplay.setText("▶ 播放")
            self.btn_vplay.setChecked(False)
            self.ble.stop_stream()
            self.lbl_state.setText("视频已暂停")
            return
        self.player.play()
        self.video_timer.start(50)
        self.btn_vplay.setText("❚❚ 暂停")
        self.btn_vplay.setChecked(True)
        self.lbl_state.setText("视频联动中")
        if self.connected:
            self.ble.start_stream()

    def on_video_stop(self):
        try:
            self.player.stop()
        except Exception:
            pass
        self.video_timer.stop()
        self.btn_vplay.setText("▶ 播放")
        self.btn_vplay.setChecked(False)
        self.ble.stop_stream()
        self.ch = [0, 0, 0, 0, 0]
        self.ble.set_target(self.ch)
        self.ring.set_level(0)
        self.lbl_state.setText("视频已停止")
        self.ble.submit(self.ble.hard_stop())

    def on_video_seek(self, val):
        self._seeking = True
        self.player.setPosition(val)
        lv = self._curve_level(val)
        self.ch = [lv, lv, lv, lv, 0]
        self.ring.set_level(lv)
        if self.connected:
            self.ble.set_target(self.ch)
        self.vprog.setText(f"{val / 1000:.1f}s / {self.video.duration:.1f}s    强度 {lv}")

    def on_video_seek_end(self):
        self._seeking = False

    def on_video_fullscreen(self):
        if self.vw.isFullScreen():
            self.vw.setFullScreen(False)
        else:
            self.vw.setFullScreen(True)
            self.vw.setFocus()

    def set_mix(self, name, mix):
        for n, b in self.mix_btns.items():
            b.setChecked(n == name)
        self.video.set_mix(mix)
        self._append(f"视频玩法：{name}（动作 {mix * 100:.0f}% / 声音 {(1 - mix) * 100:.0f}%）")

    def on_vgain(self, v):
        self._vgain = v / 100.0

    def _curve_level(self, ms):
        lv = self.video.level_at(ms) * getattr(self, "_vgain", 1.0)
        return self._s(lv)

    def _on_pos(self, pos):
        if not getattr(self, "_seeking", False):
            self.vslider.setValue(pos)

    def _on_dur(self, dur):
        if dur > 0:
            self.vslider.setRange(0, dur)

    def _on_media_status(self, st):
        if st == QMediaPlayer.EndOfMedia:
            self._append("视频播放结束")
            self.on_video_stop()

    def _on_video_tick(self):
        if not self.video.ts:
            return
        pos = self.player.position()
        lv = self._curve_level(pos)
        self.ch = [lv, lv, lv, lv, 0]
        self.ring.set_level(lv)
        if self.connected:
            self.ble.set_target(self.ch)
        self.vprog.setText(f"{pos / 1000:.1f}s / {self.video.duration:.1f}s    强度 {lv}")

    def set_ai_style(self, name):
        self.ai_style = name
        for n, b in self.ai_btns.items():
            b.setChecked(n == name)

    def on_ai_test(self):
        self.ai_stat.setText("正在测延迟…")
        self._append(f"测试 {self.ai.model} 延迟…")

        def work():
            text = self.ai.chat("只回复两个字：就绪",
                                system="你是测试助手，回答越短越好。", max_tokens=8)
            if text:
                self.q.put(("aistat", {
                    "text": f"模型 {self.ai.model} · 首字 {self.ai.last_ttft:.0f}ms · "
                            f"总 {self.ai.last_total:.0f}ms"}))
            else:
                self.q.put(("aistat", {"text": f"失败：{self.ai.last_err}"}))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def on_ai_start(self):
        if not self.connected:
            self._append("未连接设备，无法让 AI 接管")
            return
        self.ai_playing = True
        self.ai_buf = []
        self.ble.start_stream()
        self.lbl_state.setText("AI 接管中")
        self.ai_stat.setText(f"模型 {self.ai.model} · 风格 {self.ai_style} · 正在生成…")
        self._append(f"AI 接管：风格 {self.ai_style}，上限 {self.ai_cap.value()}")
        self._ai_request()
        self.ai_timer.start(200)

    def on_ai_stop(self):
        self.ai_playing = False
        self.ai_buf = []
        self.ai_timer.stop()
        self.ble.stop_stream()
        self.ch = [0, 0, 0, 0, 0]
        self.ble.set_target(self.ch)
        self.ring.set_level(0)
        self.lbl_state.setText("AI 已停止")
        self.ble.submit(self.ble.hard_stop())
        self.ai_stat.setText("已停止")

    def _parse_steps(self, text):
        import json
        t = text.strip()
        if "```" in t:
            t = t.split("```")[1]
            if t.startswith("json"):
                t = t[4:]
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return []
        try:
            obj = json.loads(t[i:j + 1])
        except Exception:
            return []
        steps = obj.get("steps") or []
        out = []
        cap = self.ai_cap.value()
        for st in steps:
            try:
                lv = int(float(st.get("lvl", 50)))
                ms = int(float(st.get("ms", 1000)))
            except Exception:
                continue
            out.append({"lvl": max(0, min(cap, lv)), "ms": max(400, min(4000, ms))})
        return out

    def _ai_request(self):
        if self.ai_busy or not self.ai_playing:
            return
        self.ai_busy = True
        cap = self.ai_cap.value()
        cur = max(self.ch)
        prompt = (f"风格：{self.ai_style}（{AI_STYLES.get(self.ai_style, '')}）。"
                  f"强度上限 {cap}，当前强度 {cur}。生成 10 步节奏，每步 600-2000 毫秒，"
                  f'lvl 在 15-{cap} 之间。只输出 JSON：{{"steps":[{{"lvl":60,"ms":1200}}]}}')

        def work():
            text = self.ai.chat(prompt,
                                system="你是节奏设计师，为情趣设备生成节奏脚本。"
                                       "严格只输出 JSON，不要任何解释文字。")
            if not text:
                self.q.put(("aierr", {"text": self.ai.last_err or "无响应"}))
                return
            steps = self._parse_steps(text)
            if not steps:
                self.q.put(("aierr", {"text": f"解析失败：{text[:80]}"}))
                return
            self.q.put(("aisteps", {"steps": steps}))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _ai_fallback(self, n=10):
        """API 不可用时用本地随机节奏顶上，保证不断流。"""
        cap = self.ai_cap.value()
        out = []
        for _ in range(n):
            out.append({"lvl": random.randint(max(15, cap // 3), cap),
                        "ms": random.randint(700, 1800)})
        return out

    def _ai_tick(self):
        if not self.ai_playing:
            return
        if not self.ai_buf:
            self.ai_stat.setText("缓冲耗尽，等待 AI…")
            self._ai_request()
            self.ai_timer.start(500)
            return
        step = self.ai_buf.pop(0)
        self._set_level(step["lvl"])
        left = sum(x["ms"] for x in self.ai_buf) / 1000.0
        self.ai_stat.setText(
            f"模型 {self.ai.model} · 延迟 {self.ai.last_ttft:.0f}ms · 缓冲 {left:.1f}s · "
            f"当前 {step['lvl']}")
        if left < 4.0:
            self._ai_request()
        self.ai_timer.start(max(300, step["ms"]))

    def on_bpm(self, bpm):
        for b in (60, 90, 120, 150):
            getattr(self, f"bpm_btn_{b}").setChecked(b == bpm)
        half = 60000.0 / bpm / 2
        levels = [96, 0, 72, 0, 84, 0, 72, 0]
        self._append(f"节拍：{bpm} BPM（每拍 {60000//bpm}ms）")
        self._start([[v, v, v, v, 0] for v in levels], half)

    def on_gacha(self):
        name, tip, fn = random.choice(WAVE_DEFS)
        levels = fn()
        if getattr(self, "btn_soft", None) and self.btn_soft.isChecked():
            levels = soften(levels, max_step=20)
        self._append(f"盲盒：抽到「{name}」（{tip}）")
        self._start([[v, v, v, v, 0] for v in levels], 250)

    def on_ambush(self):
        levels = []
        while len(levels) < 140:
            levels += [6] * random.randint(6, 18)
            levels += [random.randint(80, 100)] * random.randint(2, 5)
        if getattr(self, "btn_soft", None) and self.btn_soft.isChecked():
            levels = soften(levels, max_step=20)
        self._append("突袭：安静中随机爆发，别放松")
        self._start([[v, v, v, v, 0] for v in levels], 250)

    def on_stop(self):
        self._stop_mic()
        if self.voice_on:
            self.voice.stop()
            self.voice_on = False
            self.btn_voice.setChecked(False)
            self.lbl_voice.setText("说出关键词即可控制（快点/慢点/停）")
        for b in (60, 90, 120, 150):
            getattr(self, f"bpm_btn_{b}").setChecked(False)
        self.tick.stop()
        self.clock.stop()
        self.playing = False
        self.ble.stop_stream()
        for sl in self.sliders:
            sl.blockSignals(True)
            sl.setValue(0)
            sl.blockSignals(False)
        self.ch = [0, 0, 0, 0, 0]
        self.ble.set_target(self.ch)
        for lb in self.val_labels:
            lb.setText("0")
        self.ring.set_level(0)
        self.lbl_state.setText("已停止")
        self.btn_play.setText("▶  开始玩")
        self.btn_power.setText("电源")
        for b in (list(self.skill_btns.values()) + list(self.wave_btns.values())
                  + list(getattr(self, "mode_btns", {}).values())
                  + list(getattr(self, "fw_btns", {}).values())):
            b.setChecked(False)
        self.active_skill = self.active_wave = None
        if self.heat_on:
            self.heat_on = False
            self.btn_heat.setChecked(False)
            self.ble.submit(self.ble.send(frame(CMD_HEAT, [0]), "HEAT OFF"))
        self.ble.submit(self.ble.hard_stop())

    # ---------- 节奏 ----------
    def _start(self, seq, ms):
        if not self.connected:
            self._append("未连接设备")
            return
        self.tick.stop()
        self.pattern = seq or [[40, 40, 40, 40, 0]]
        self.idx = 0
        self._step_ms = max(200, float(ms))
        self._acc = self._step_ms
        self.playing = True
        self.start_ts = dt.datetime.now()
        self.btn_play.setText("❚❚  暂停")
        self.btn_power.setText("电源 开")
        self.lbl_state.setText("运行中")
        self.ble.set_target(self.pattern[0])
        self.ble.start_stream()
        self.tick.start(250)
        self.clock.start(1000)
        self._on_tick()

    def _on_tick(self):
        if not self.playing or self.mic_on:
            return
        self._acc += 250
        if self._acc >= self._step_ms:
            self._acc -= self._step_ms
            v = self.pattern[self.idx % len(self.pattern)]
            self.idx += 1
            self.ch = list(v)
            self.ring.set_level(max(v))
            self.ble.set_target(v)

    def _on_clock(self):
        if not self.playing or not self.start_ts:
            return
        s = int((dt.datetime.now() - self.start_ts).total_seconds())
        self.lbl_time.setText(f"运行时长 {s // 60:02d}:{s % 60:02d}")
        if s > self.limit_min * 60:
            self._append(f"达到 {self.limit_min} 分钟上限，自动停止")
            self.on_stop()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and getattr(self, "vw", None) and self.vw.isFullScreen():
            self.vw.setFullScreen(False)
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        try:
            self.playing = False
            self.tick.stop()
            self.clock.stop()
            self.ble.submit(self.ble.hard_stop())
            self.ble.submit(self.ble.disconnect())
        except Exception:
            pass
        e.accept()


def _p(*a):
    try:
        if sys.stdout:
            print(*a, flush=True)
    except Exception:
        pass


def main():
    def _hook(t, v, tb):
        import traceback
        try:
            with open("mizzzee_error.log", "a", encoding="utf-8") as f:
                f.write(f"\n==== {dt.datetime.now()} ====\n"
                        + "".join(traceback.format_exception(t, v, tb)))
        except Exception:
            pass
        sys.__excepthook__(t, v, tb)

    sys.excepthook = _hook

    # 单实例保护：多开会导致多个进程争抢同一条 BLE 连接
    _lock = socket.socket()
    try:
        _lock.bind(("127.0.0.1", 52341))
    except OSError:
        from PySide6.QtWidgets import QMessageBox
        _app = QApplication(sys.argv)
        QMessageBox.warning(None, "已在运行",
                            "本程序已有一个实例在运行。\n多开会导致两个进程争抢蓝牙连接，"
                            "请先关闭已有窗口。")
        sys.exit(0)

    app = QApplication(sys.argv)
    w = Main()
    w.show()
    for _a in sys.argv:
        if _a.startswith("--tab") and _a[5:].isdigit():
            w.switch(int(_a[5:]))
    if "--selftest" in sys.argv:
        QTimer.singleShot(14000, lambda: (
            _p(f"SELFTEST connected={w.connected}"), w.close(), app.quit()))
    if "--autotest" in sys.argv:
        QTimer.singleShot(12000, lambda: (
            w.on_skill("水光环绕"), _p("AUTOTEST skill", w.connected, w.playing)))
        QTimer.singleShot(18000, lambda: (
            w.switch(2), w.slider.setValue(55), _p("AUTOTEST manual 55")))
        QTimer.singleShot(24000, lambda: (w.on_stop(), _p("AUTOTEST stop")))
        QTimer.singleShot(26000, lambda: (w.close(), app.quit()))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
