"""设置页：Token、路径、阈值、UI 风格等。"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from app.core.config import Config
from app.ui_styles import REGISTRY

log = logging.getLogger(__name__)


class SettingsPage(QWidget):
    """设置页。"""

    save_requested = Signal()
    scan_requested = Signal()

    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        title = QLabel("设置", self)
        title.setProperty("role", "title")
        outer.addWidget(title)

        form = QFormLayout()
        form.setSpacing(12)

        # ---- Bangumi ----
        self.token_edit = QLineEdit(self.config.get("bangumi", "token"), self)
        self.token_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Bangumi Token", self.token_edit)

        self.api_base_edit = QLineEdit(
            self.config.get("bangumi", "api_base", "https://api.bgm.tv"), self
        )
        form.addRow("API 地址", self.api_base_edit)

        self.proxy_edit = QLineEdit(self.config.get("bangumi", "proxy"), self)
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7890（可选）")
        form.addRow("代理", self.proxy_edit)

        # ---- 路径 ----
        self.library_edit = QLineEdit(self.config.get("general", "library_path"), self)
        lib_row = QHBoxLayout()
        lib_row.addWidget(self.library_edit, 1)
        lib_btn = QPushButton("浏览…", self)
        lib_btn.clicked.connect(self._pick_library)
        lib_row.addWidget(lib_btn)
        form.addRow("媒体库根目录", lib_row)

        self.player_edit = QLineEdit(self.config.get("general", "player_path"), self)
        player_row = QHBoxLayout()
        player_row.addWidget(self.player_edit, 1)
        player_btn = QPushButton("浏览…", self)
        player_btn.clicked.connect(lambda: self._pick_file(self.player_edit, "PotPlayer"))
        player_row.addWidget(player_btn)
        form.addRow("PotPlayer 路径", player_row)

        self.ls_edit = QLineEdit(self.config.get("general", "ls_path"), self)
        ls_row = QHBoxLayout()
        ls_row.addWidget(self.ls_edit, 1)
        ls_btn = QPushButton("浏览…", self)
        ls_btn.clicked.connect(lambda: self._pick_file(self.ls_edit, "Lossless Scaling"))
        ls_row.addWidget(ls_btn)
        form.addRow("小黄鸭路径", ls_row)

        # ---- 启动器 ----
        self.enable_ls_check = QCheckBox("启用插帧", self)
        self.enable_ls_check.setChecked(self.config.getbool("launcher", "enable_ls", True))
        form.addRow("", self.enable_ls_check)

        self.ls_shortcut_edit = QLineEdit(
            self.config.get("launcher", "ls_shortcut", "ctrl+alt+l"), self
        )
        form.addRow("插帧快捷键", self.ls_shortcut_edit)

        # ---- 监控 ----
        self.poll_spin = QSpinBox(self)
        self.poll_spin.setRange(1, 60)
        self.poll_spin.setValue(self.config.getint("monitor", "poll_interval", 3))
        self.poll_spin.setSuffix(" 秒")
        form.addRow("轮询间隔", self.poll_spin)

        self.threshold_spin = QDoubleSpinBox(self)
        self.threshold_spin.setRange(0.5, 1.0)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.setValue(
            self.config.getfloat("monitor", "trigger_threshold", 0.95)
        )
        form.addRow("触发阈值", self.threshold_spin)

        # ---- UI 风格 ----
        self.style_combo = QComboBox(self)
        current_style = self.config.get("ui", "style", "fusion_dark")
        for key, style in REGISTRY.items():
            self.style_combo.addItem(f"{style.name}（{key}）", userData=key)
            idx = self.style_combo.count() - 1
            self.style_combo.setItemData(idx, style.description, role=Qt.ToolTipRole)
            if key == current_style:
                self.style_combo.setCurrentIndex(idx)
        form.addRow("UI 风格", self.style_combo)

        hint = QLabel("风格切换需重启应用生效", self)
        hint.setProperty("role", "hint")
        form.addRow("", hint)

        outer.addLayout(form)
        outer.addStretch(1)

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.scan_btn = QPushButton("保存并扫描", self)
        self.scan_btn.setProperty("role", "primary")
        self.scan_btn.clicked.connect(self._on_save_and_scan)
        btn_row.addWidget(self.scan_btn)

        self.save_btn = QPushButton("保存", self)
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.save_btn)

        outer.addLayout(btn_row)

    # ---------- 动作 ----------
    def _on_save(self) -> None:
        self._write_back()
        QMessageBox.information(self, "保存", "配置已保存。\nUI 风格将在下次启动时生效。")
        self.save_requested.emit()

    def _on_save_and_scan(self) -> None:
        self._write_back()
        self.scan_requested.emit()

    def _write_back(self) -> None:
        c = self.config
        c.set("bangumi", "token", self.token_edit.text().strip())
        c.set("bangumi", "api_base", self.api_base_edit.text().strip() or "https://api.bgm.tv")
        c.set("bangumi", "proxy", self.proxy_edit.text().strip())
        c.set("general", "library_path", self.library_edit.text().strip())
        c.set("general", "player_path", self.player_edit.text().strip())
        c.set("general", "ls_path", self.ls_edit.text().strip())
        c.set("launcher", "enable_ls", "true" if self.enable_ls_check.isChecked() else "false")
        c.set("launcher", "ls_shortcut", self.ls_shortcut_edit.text().strip())
        c.set("monitor", "poll_interval", str(self.poll_spin.value()))
        c.set("monitor", "trigger_threshold", str(self.threshold_spin.value()))
        c.set("ui", "style", self.style_combo.currentData())
        c.save()

    def _pick_library(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择媒体库根目录")
        if path:
            self.library_edit.setText(path)

    def _pick_file(self, target: QLineEdit, label: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"选择{label}", "", "可执行文件 (*.exe)")
        if path:
            target.setText(path)
