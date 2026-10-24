# -*- coding: utf-8 -*-
"""机型配置获取：填入 App 的 token 即可拉取服务端 deviceTypeConfig。"""
import json
import os
import sys
import threading
import urllib.request

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QFileDialog,
)

BASE = "https://akn.ankeni.net/zqf/api"
CFG = "ai_config.json"


def load_cfg():
    try:
        return json.load(open(CFG, encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(d):
    try:
        json.dump(d, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


class ConfigDlg(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("机型配置 / 账号")
        self.resize(620, 520)
        self.setStyleSheet(parent.styleSheet() if parent else "")

        v = QVBoxLayout(self)
        v.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("deviceType"))
        self.dtype = QLineEdit("mizzzee_0008")
        row.addWidget(self.dtype)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("token"))
        self.token = QLineEdit()
        self.token.setPlaceholderText("粘贴醉清风 App 里的登录 token")
        row2.addWidget(self.token)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn = QPushButton("获取机型配置")
        self.btn.clicked.connect(self.fetch)
        self.btn_save = QPushButton("保存")
        self.btn_save.clicked.connect(self.save)
        self.btn_file = QPushButton("导出 JSON")
        self.btn_file.clicked.connect(self.export)
        row3.addWidget(self.btn)
        row3.addWidget(self.btn_save)
        row3.addWidget(self.btn_file)
        row3.addStretch()
        v.addLayout(row3)

        tip = QLabel("token 获取方式：手机安装 HttpCanary/Stream 抓包，"
                     "或在 App 内任意请求头里找 token 字段。仅保存在本机。",
                     objectName="hint")
        tip.setWordWrap(True)
        v.addWidget(tip)

        self.out = QPlainTextEdit(readOnly=True)
        v.addWidget(self.out, 1)

        cfg = load_cfg()
        if cfg.get("token"):
            self.token.setText(cfg["token"])
        if cfg.get("deviceType"):
            self.dtype.setText(cfg["deviceType"])

    def save(self):
        save_cfg({"token": self.token.text().strip(),
                  "deviceType": self.dtype.text().strip()})
        self.out.setPlainText("已保存到 ai_config.json")

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出", "device_config.json",
                                              "JSON (*.json)")
        if path:
            open(path, "w", encoding="utf-8").write(self.out.toPlainText())

    def fetch(self):
        tok = self.token.text().strip()
        dt = self.dtype.text().strip()
        if not tok:
            self.out.setPlainText("请先填入 token")
            return
        self.out.setPlainText("请求中…")
        self.btn.setEnabled(False)

        def work():
            body = json.dumps({"deviceType": dt, "deviceId": dt}).encode()
            req = urllib.request.Request(
                BASE + "/device/info/getDeviceLastStatus", data=body,
                headers={"Content-Type": "application/json", "token": tok})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read().decode("utf-8", "ignore")
                try:
                    j = json.loads(raw)
                except Exception:
                    self.out.setPlainText(raw[:4000])
                    return
                if j.get("code") == 200 and j.get("data"):
                    cfg = (j["data"] or {}).get("deviceTypeConfig") or {}
                    text = json.dumps(j["data"], ensure_ascii=False, indent=2)
                    self.out.setPlainText(
                        f"=== 完整响应 ===\n{text}\n\n"
                        f"=== 关键字段 ===\n"
                        f"prefix     = {cfg.get('prefix')}\n"
                        f"motorNum   = {cfg.get('motorNum')}\n"
                        f"protocolType = {cfg.get('protocolType')}\n"
                        f"haveTelescopic = {cfg.get('haveTelescopic')}\n"
                        f"haveSucking = {cfg.get('haveSucking')}\n"
                        f"haveRotate = {cfg.get('haveRotate')}\n"
                        f"haveHot    = {cfg.get('haveHot')}\n"
                        f"deviceName = {cfg.get('deviceName')}")
                else:
                    self.out.setPlainText(json.dumps(j, ensure_ascii=False, indent=2))
            except Exception as e:
                self.out.setPlainText(f"请求失败：{type(e).__name__}: {e}")
            finally:
                self.btn.setEnabled(True)

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    d = ConfigDlg()
    d.show()
    sys.exit(app.exec())
