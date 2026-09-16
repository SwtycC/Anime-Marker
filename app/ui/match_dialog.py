"""手动匹配对话框（F11）。

用于自动匹配失败或结果错误时，由用户搜索并指定正确的 Bangumi 条目。
支持：关键词搜索、候选列表打分排序、手动查看条目信息。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from app.core.bangumi_api import BangumiClient, BangumiError
from app.core.matcher import SCORE_IRRELEVANT, SubjectMatcher
from app.core.database import Database, Subject
from app.utils import bgm_log

log = logging.getLogger(__name__)


class _SearchWorker(QThread):
    """后台搜索，避免阻塞对话框。"""

    finished_results = Signal(object)   # list[tuple[dict, int, str]]

    def __init__(
        self,
        api: BangumiClient,
        keyword: str,
        ep_count: int,
        subject_name: str = "",
    ) -> None:
        super().__init__()
        self.api = api
        self.keyword = keyword
        self.ep_count = ep_count
        self.subject_name = subject_name

    def run(self) -> None:
        # 让 Bangumi 请求日志带上「哪个动漫」
        bgm_log.set_current_name(self.subject_name)
        matcher = SubjectMatcher(self.api)
        try:
            results = self.api.search_subjects(self.keyword, limit=20)
        except BangumiError as e:
            log.warning("手动匹配搜索失败: %s", e)
            self.finished_results.emit([])
            return
        except Exception as e:
            log.exception("手动匹配搜索异常")
            self.finished_results.emit([])
            return

        # 手动匹配保留全部候选（用户可能确实要一部名字不同的作品），
        # 但把「被否决」的候选排在最后，并在列表里灰度显示以示区分。
        scored: list[tuple[dict, int, str]] = []
        for subj in results:
            # 先绑定 ID ↔ 名称，后续拉集数若失败可定位到条目
            bgm_log.bind_subject(subj.get("id"), subj.get("name_cn") or subj.get("name"))
            score, reason = matcher.score_with_reason(subj, self.keyword, self.ep_count)
            scored.append((subj, score, reason))
        scored.sort(key=lambda x: x[1], reverse=True)
        self.finished_results.emit(scored)


class MatchDialog(QDialog):
    """搜索并选择 Bangumi 条目。"""

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        subject: Subject,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.api = api
        self.subject = subject
        self._worker: Optional[_SearchWorker] = None
        self._results: list[tuple[dict, int, str]] = []
        self.selected_bangumi_id: Optional[int] = None

        self.setWindowTitle("重新匹配 Bangumi 条目")
        self.resize(720, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # 当前条目信息
        info = QLabel(
            f"本地目录：{subject.folder_path}\n"
            f"当前名称：{subject.name_cn or subject.name}"
            + (f"（{subject.match_state}）" if subject.match_state != "auto" else ""),
            self,
        )
        info.setProperty("role", "hint")
        info.setWordWrap(True)
        outer.addWidget(info)

        # 搜索行
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.keyword_edit = QLineEdit(self)
        # 默认填入：本地条目名 → 斜杠换成空格（Fate/strange Fake → Fate strange Fake）
        default_kw = (subject.name_cn or subject.name or "").replace("/", " ")
        self.keyword_edit.setText(default_kw)
        self.keyword_edit.returnPressed.connect(self._on_search)
        search_row.addWidget(self.keyword_edit, 1)

        self.search_btn = QPushButton("搜索", self)
        self.search_btn.setProperty("role", "primary")
        self.search_btn.clicked.connect(self._on_search)
        search_row.addWidget(self.search_btn)
        outer.addLayout(search_row)

        # 结果列表
        self.result_list = QListWidget(self)
        self.result_list.itemDoubleClicked.connect(lambda _i: self._on_accept())
        outer.addWidget(self.result_list, 1)

        # 底部按钮
        btn_row = QHBoxLayout()
        self.status_label = QLabel("", self)
        self.status_label.setProperty("role", "hint")
        btn_row.addWidget(self.status_label, 1)

        cancel_btn = QPushButton("取消", self)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self.accept_btn = QPushButton("使用选中条目", self)
        self.accept_btn.setProperty("role", "primary")
        self.accept_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(self.accept_btn)
        outer.addLayout(btn_row)

        # 打开即自动搜一次
        self._on_search()

    # ---------- 搜索 ----------
    def _on_search(self) -> None:
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            QMessageBox.warning(self, "搜索", "请输入关键词")
            return
        if self._worker is not None and self._worker.isRunning():
            return

        self.search_btn.setEnabled(False)
        self.status_label.setText("搜索中…")
        self.result_list.clear()

        ep_count = len(self.db.list_episodes(self.subject.id))
        subject_name = self.subject.name_cn or self.subject.name or ""
        self._worker = _SearchWorker(self.api, keyword, ep_count, subject_name)
        self._worker.finished_results.connect(self._on_results)
        self._worker.start()

    def _on_results(self, results: list) -> None:
        self.search_btn.setEnabled(True)
        self._results = results
        if not results:
            self.status_label.setText("没有结果")
            return

        rejected = 0
        for subj, score, reason in results:
            name_cn = subj.get("name_cn") or subj.get("name") or ""
            name = subj.get("name") or ""
            eps = subj.get("total_episodes") or subj.get("eps_count") or 0
            date = subj.get("date") or ""
            year = date[:4] if date else "----"

            is_rejected = score <= SCORE_IRRELEVANT
            if is_rejected:
                rejected += 1
                score_text = "不相关"
            else:
                score_text = str(score)

            text = f"{name_cn}"
            if name and name != name_cn:
                text += f"  ／  {name}"
            text += (
                f"\n      {year} · {eps} 集 · bgm {subj.get('id')}"
                f" · 匹配分 {score_text}（{reason}）"
            )

            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, subj.get("id"))
            # 否决项 / 低分项灰度显示（仍可选：用户可能确实要这部作品）
            if is_rejected or score < 60:
                item.setForeground(Qt.gray)
            self.result_list.addItem(item)

        self.result_list.setCurrentRow(0)
        total = len(results)
        normal = total - rejected
        msg = f"共 {total} 条结果（按匹配分排序）"
        if rejected:
            msg += f"，其中 {normal} 条相关、{rejected} 条被规则否决（灰度显示）"
        self.status_label.setText(msg)

    # ---------- 接受 ----------
    def _on_accept(self) -> None:
        item = self.result_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "重新匹配", "请先选择一个条目")
            return
        bangumi_id = item.data(Qt.UserRole)
        if not bangumi_id:
            return

        # 取回该候选的完整信息
        subj = next((s for s, _sc, _r in self._results if s.get("id") == bangumi_id), None)
        if subj is None:
            return

        self.selected_bangumi_id = int(bangumi_id)
        name = subj.get("name") or ""
        name_cn = subj.get("name_cn") or name
        cover_url = (subj.get("images") or {}).get("large", "")
        total_eps = subj.get("total_episodes") or subj.get("eps_count") or 0

        # 下载封面（失败不阻塞）
        cover_path = self.subject.cover_path or ""
        try:
            from app.utils.cover_cache import download as download_cover
            if cover_url:
                cover_path = str(download_cover(bangumi_id, cover_url, self.api.session))
        except Exception as e:
            log.warning("封面下载失败: %s", e)

        self.db.set_manual_match(
            subject_id=self.subject.id,
            bangumi_id=bangumi_id,
            name=name,
            name_cn=name_cn,
            cover_url=cover_url,
            cover_path=cover_path,
            total_eps=total_eps,
        )
        # 重拉集数并回填 bangumi_ep_id（本地文件不动）
        bgm_log.bind_subject(bangumi_id, name_cn or name)
        try:
            bgm_eps = self.api.get_episodes(bangumi_id)
            ep_map = {e.get("sort") or e.get("ep"): e for e in bgm_eps}
            for ep in self.db.list_episodes(self.subject.id):
                bgm = ep_map.get(ep.ep_index)
                if bgm:
                    self.db.update_episode_title(
                        ep.id,
                        bgm.get("name_cn") or bgm.get("name") or ep.title,
                        bgm.get("id"),
                    )
        except BangumiError as e:
            log.warning("拉取集数失败（不影响手动匹配）: %s", e)

        self.accept()
