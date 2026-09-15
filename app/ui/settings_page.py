"""设置页：Token、路径、阈值、UI 风格等。"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLayout, QLineEdit, QMessageBox, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from app.core.config import Config
from app.ui.info_tip import InfoTip
from app.ui.num_inputs import NumberField, NoWheelComboBox
from app.ui_styles import REGISTRY

log = logging.getLogger(__name__)

# Bangumi Token 获取页（点击提示气泡会在浏览器打开）
TOKEN_HELP_URL = "https://next.bgm.tv/demo/access-token"
# Token 行图标区的实际占用宽度，构成：
#   InfoTip 按钮宽（ICON_SIZE = 18）
#   + token_row spacing（6）
#   + 按钮与输入框之间的 addSpacing（2）
#   = 26
# 其余各行按此值左缩进，保证输入框左边缘对齐（实测校准）
ROW_INDENT = 26
# 「文本框 + 浏览…按钮」之间的间距（被 _indent_layout 包裹后需显式设置，
# 否则容器 spacing=0 会让按钮紧贴文本框）
PATH_ROW_SPACING = 6


def _section_divider() -> QFrame:
    """区块分隔线：1px 灰线（颜色由 QSS 的 #sectionDivider 控制）。"""
    line = QFrame()
    line.setObjectName("sectionDivider")
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)
    line.setFixedHeight(1)
    return line


def _indent(widget: QWidget) -> QWidget:
    """把控件包进一个带左缩进的容器，使其左边缘与 Token 输入框对齐。

    Token 行的结构是：[图标 28px][输入框]，本函数为其他行补上等宽左占位。
    """
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    row.addSpacing(ROW_INDENT)
    row.addWidget(widget, 1)
    return host


def _indent_layout(layout: QLayout) -> QWidget:
    """同上，但作用于已有的 QHBoxLayout（如「媒体库根目录」那行）。"""
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    row.addSpacing(ROW_INDENT)
    row.addLayout(layout, 1)
    return host


class SettingsPage(QWidget):
    """设置页。"""

    save_requested = Signal()
    scan_requested = Signal()

    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config

        # 边距为 0：滚动条才能像海报墙那样贴住窗口右边缘
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 标题放在滚动区外？——不行，标题也需随内容滚动，故一并放进内容区
        # 唯一保留在滚动区之外的是底部按钮行
        scroll = QScrollArea(self)
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        # 与海报墙保持一致：只留垂直滚动条
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll, 1)

        form_host = QWidget()
        scroll.setWidget(form_host)

        host_layout = QVBoxLayout(form_host)
        # 24px 内边距移到内容里，滚动条仍贴边
        host_layout.setContentsMargins(24, 24, 24, 8)
        host_layout.setSpacing(12)

        title = QLabel("设置", form_host)
        title.setProperty("role", "title")
        host_layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(12)

        # ---- Bangumi ----
        self.token_edit = QLineEdit(self.config.get("bangumi", "token"), self)
        self.token_edit.setEchoMode(QLineEdit.Password)

        # 感叹号插在标签与输入框之间：Bangumi Token  (i)  [ 输入框 ]
        # 其余各行的控件左侧会加等宽占位（见 _indent），使输入框左边缘对齐
        self.token_tip = InfoTip(
            "获取 Bangumi Access Token：\n"
            "https://next.bgm.tv/demo/access-token\n\n"
            "生成后请勾选「读取收藏」权限，否则「在看」列表无法拉取。",
            self,
        )
        self.token_tip.set_link(TOKEN_HELP_URL)

        # 按钮尺寸变化会影响 ROW_INDENT，此处保持与常量一致
        token_row = QHBoxLayout()
        token_row.setSpacing(6)
        token_row.addWidget(self.token_tip, 0, Qt.AlignVCenter)
        token_row.addSpacing(2)
        token_row.addWidget(self.token_edit, 1)

        form.addRow("Bangumi Token", token_row)

        self.username_edit = QLineEdit(self.config.get("bangumi", "username"), self)
        self.username_edit.setPlaceholderText("在看列表需要；留空则尝试用 Token 解析")
        form.addRow("Bangumi 用户名", _indent(self.username_edit))

        self.api_base_edit = QLineEdit(
            self.config.get("bangumi", "api_base", "https://api.bgm.tv"), self
        )
        form.addRow("API 地址", _indent(self.api_base_edit))

        self.proxy_edit = QLineEdit(self.config.get("bangumi", "proxy"), self)
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7890（可选）")
        form.addRow("代理", _indent(self.proxy_edit))

        # ---- 路径 ----
        self.library_edit = QLineEdit(self.config.get("general", "library_path"), self)
        lib_row = QHBoxLayout()
        lib_row.setSpacing(PATH_ROW_SPACING)
        lib_row.addWidget(self.library_edit, 1)
        lib_btn = QPushButton("浏览…", self)
        lib_btn.clicked.connect(self._pick_library)
        lib_row.addWidget(lib_btn)
        form.addRow("媒体库根目录", _indent_layout(lib_row))

        self.player_edit = QLineEdit(self.config.get("general", "player_path"), self)
        player_row = QHBoxLayout()
        player_row.setSpacing(PATH_ROW_SPACING)
        player_row.addWidget(self.player_edit, 1)
        player_btn = QPushButton("浏览…", self)
        player_btn.clicked.connect(lambda: self._pick_file(self.player_edit, "PotPlayer"))
        player_row.addWidget(player_btn)
        form.addRow("PotPlayer 路径", _indent_layout(player_row))

        self.ls_edit = QLineEdit(self.config.get("general", "ls_path"), self)
        ls_row = QHBoxLayout()
        ls_row.setSpacing(PATH_ROW_SPACING)
        ls_row.addWidget(self.ls_edit, 1)
        ls_btn = QPushButton("浏览…", self)
        ls_btn.clicked.connect(lambda: self._pick_file(self.ls_edit, "Lossless Scaling"))
        ls_row.addWidget(ls_btn)
        form.addRow("小黄鸭路径", _indent_layout(ls_row))

        # ---- 启动器 ----
        self.enable_ls_check = QCheckBox("启用插帧", self)
        self.enable_ls_check.setChecked(self.config.getbool("launcher", "enable_ls", True))
        form.addRow("", _indent(self.enable_ls_check))

        self.ls_shortcut_edit = QLineEdit(
            self.config.get("launcher", "ls_shortcut", "ctrl+alt+l"), self
        )
        form.addRow("插帧快捷键", _indent(self.ls_shortcut_edit))

        # ---- 监控 ----
        self.poll_spin = NumberField(
            value=self.config.getint("monitor", "poll_interval", 3),
            minimum=1, maximum=60, step=1, suffix=" 秒", parent=self,
        )
        form.addRow("轮询间隔", _indent(self.poll_spin))

        self.threshold_spin = NumberField(
            value=self.config.getfloat("monitor", "trigger_threshold", 0.95),
            minimum=0.5, maximum=1.0, step=0.05, decimals=2, parent=self,
        )
        form.addRow("触发阈值", _indent(self.threshold_spin))

        # ---- qBittorrent（F19）----
        form.addRow(_section_divider())          # 分隔线
        qb_title = QLabel("qBittorrent（订阅下载）", self)
        qb_title.setProperty("role", "subtitle")
        form.addRow(qb_title)

        self.qb_host_edit = QLineEdit(self.config.get("qbittorrent", "host", "127.0.0.1"), self)
        form.addRow("Web UI 地址", _indent(self.qb_host_edit))

        self.qb_port_spin = NumberField(
            value=self.config.getint("qbittorrent", "port", 8080),
            minimum=1, maximum=65535, step=1, parent=self,
        )
        form.addRow("Web UI 端口", _indent(self.qb_port_spin))

        self.qb_user_edit = QLineEdit(self.config.get("qbittorrent", "username", "admin"), self)
        form.addRow("Web UI 用户名", _indent(self.qb_user_edit))

        self.qb_pass_edit = QLineEdit(self.config.get("qbittorrent", "password"), self)
        self.qb_pass_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Web UI 密码", _indent(self.qb_pass_edit))

        # ---- RSS（F19）----
        form.addRow(_section_divider())          # 分隔线
        rss_title = QLabel("RSS 订阅", self)
        rss_title.setProperty("role", "subtitle")
        form.addRow(rss_title)

        self.rss_poll_spin = NumberField(
            value=self.config.getint("rss", "poll_interval", 30),
            minimum=5, maximum=720, step=5, suffix=" 分钟", parent=self,
        )
        form.addRow("RSS 轮询间隔", _indent(self.rss_poll_spin))

        self.rss_rule_combo = NoWheelComboBox(self)
        current_rule = self.config.get("rss", "rule", "new_only")
        for label, key in [
            ("只下新集", "new_only"),
            ("补缺集", "fill_gap"),
            ("完结整包", "complete_pack"),
            ("仅通知", "manual"),
        ]:
            self.rss_rule_combo.addItem(label, userData=key)
            if key == current_rule:
                self.rss_rule_combo.setCurrentIndex(self.rss_rule_combo.count() - 1)
        form.addRow("默认下载规则", _indent(self.rss_rule_combo))

        self.rss_auto_check = QCheckBox("启用自动下载（关闭时命中新集仅入库为待确认）", self)
        self.rss_auto_check.setChecked(self.config.getbool("rss", "auto_download", False))
        form.addRow("", _indent(self.rss_auto_check))

        # ---- UI 风格 ----
        form.addRow(_section_divider())          # 分隔线
        ui_title = QLabel("界面", self)
        ui_title.setProperty("role", "subtitle")
        form.addRow(ui_title)

        self.style_combo = NoWheelComboBox(self)
        current_style = self.config.get("ui", "style", "fusion_dark")
        for key, style in REGISTRY.items():
            self.style_combo.addItem(f"{style.name}（{key}）", userData=key)
            idx = self.style_combo.count() - 1
            self.style_combo.setItemData(idx, style.description, role=Qt.ToolTipRole)
            if key == current_style:
                self.style_combo.setCurrentIndex(idx)
        form.addRow("UI 风格", _indent(self.style_combo))

        hint = QLabel("风格切换需重启应用生效", self)
        hint.setProperty("role", "hint")
        form.addRow("", _indent(hint))

        host_layout.addLayout(form)
        host_layout.addStretch(1)

        # 底部按钮（固定在滚动区之外，始终可见）
        # 包一层容器补回 24px 内边距，避免按钮贴到窗口边缘
        btn_host = QWidget(self)
        btn_row = QHBoxLayout(btn_host)
        btn_row.setContentsMargins(24, 8, 24, 16)
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self.scan_btn = QPushButton("保存并扫描", btn_host)
        self.scan_btn.setProperty("role", "primary")
        self.scan_btn.clicked.connect(self._on_save_and_scan)
        btn_row.addWidget(self.scan_btn)

        self.save_btn = QPushButton("保存", btn_host)
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.save_btn)

        outer.addWidget(btn_host)

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
        c.set("bangumi", "username", self.username_edit.text().strip())
        c.set("bangumi", "api_base", self.api_base_edit.text().strip() or "https://api.bgm.tv")
        c.set("bangumi", "proxy", self.proxy_edit.text().strip())
        c.set("general", "library_path", self.library_edit.text().strip())
        c.set("general", "player_path", self.player_edit.text().strip())
        c.set("general", "ls_path", self.ls_edit.text().strip())
        c.set("launcher", "enable_ls", "true" if self.enable_ls_check.isChecked() else "false")
        c.set("launcher", "ls_shortcut", self.ls_shortcut_edit.text().strip())
        c.set("monitor", "poll_interval", str(self.poll_spin.value()))
        c.set("monitor", "trigger_threshold", str(self.threshold_spin.value()))
        c.set("qbittorrent", "host", self.qb_host_edit.text().strip() or "127.0.0.1")
        c.set("qbittorrent", "port", str(self.qb_port_spin.value()))
        c.set("qbittorrent", "username", self.qb_user_edit.text().strip())
        c.set("qbittorrent", "password", self.qb_pass_edit.text())
        c.set("rss", "poll_interval", str(self.rss_poll_spin.value()))
        c.set("rss", "rule", self.rss_rule_combo.currentData())
        c.set("rss", "auto_download",
              "true" if self.rss_auto_check.isChecked() else "false")
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
