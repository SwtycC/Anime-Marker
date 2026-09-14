"""媒体库扫描 + Bangumi 匹配。

- QThread 后台执行，通过信号汇报进度
- 已存在的 file_path 不重复入库（UNIQUE + ON CONFLICT）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from app.core.bangumi_api import BangumiClient, BangumiError
from app.core.database import Database
from app.utils.cover_cache import download as download_cover

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".rmvb", ".ts", ".flv", ".mov", ".wmv"}

NOISE_PATTERNS = [
    r"\[.*?\]",
    r"【.*?】",
    r"\(.*?\)",
    r"(?i)\b(BDRip|WEB[- ]?DL|WEBRip|BluRay|HDTV|x264|x265|HEVC|AVC|10bit|8bit|1080p|720p|2160p|4K)\b",
    r"(?i)\b(简体|繁体|简繁|内嵌|外挂|中字|日配|国配|合集|完全版|无修)\b",
    r"_+",
]


def clean_title(name: str) -> str:
    """清洗文件夹/文件名，提取搜索关键词。"""
    s = Path(name).stem
    for pat in NOISE_PATTERNS:
        s = re.sub(pat, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_ep_index(name: str) -> float | None:
    """从文件名中提取集数序号。支持 01 / 第1话 / EP01 / 12.5。"""
    stem = Path(name).stem
    m = re.search(r"第\s*(\d+(?:\.\d+)?)\s*[话集回]", stem)
    if m:
        return float(m.group(1))
    m = re.search(r"(?i)\bE[P]?\s*0*(\d+(?:\.\d+)?)\b", stem)
    if m:
        return float(m.group(1))
    m = re.search(r"[\s\-\_\[\]]0*(\d+(?:\.\d+)?)(?:v\d+)?\s*$", stem)
    if m:
        return float(m.group(1))
    m = re.search(r"[\s\-\_\[\]]0*(\d+(?:\.\d+)?)(?:v\d+)?[\s\-\_\[\]]", stem)
    if m:
        return float(m.group(1))
    return None


@dataclass
class ScanCandidate:
    folder_path: Path
    video_files: list[Path]
    keyword: str


class ScanWorker(QThread):
    """后台扫描线程。"""

    progress_changed = Signal(int, int)        # current, total
    item_matched = Signal(int, str)            # subject_id, name
    log_message = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        library_paths: Iterable[Path],
        api: BangumiClient,
        db: Database,
    ) -> None:
        super().__init__()
        self.library_paths = [Path(p) for p in library_paths]
        self.api = api
        self.db = db

    def run(self) -> None:
        try:
            self._run()
            self.finished_ok.emit()
        except Exception as e:
            log.exception("扫描失败")
            self.failed.emit(str(e))

    def _run(self) -> None:
        candidates = list(self._collect_candidates())
        total = len(candidates)
        if total == 0:
            self.log_message.emit("未发现任何视频文件")
            return
        for i, cand in enumerate(candidates, 1):
            self.progress_changed.emit(i, total)
            self.log_message.emit(f"[{i}/{total}] 匹配：{cand.keyword}")
            try:
                self._process(cand)
            except BangumiError as e:
                log.warning("匹配失败 keyword=%s err=%s", cand.keyword, e)
                self.log_message.emit(f"  Bangumi 错误：{e}")
            except Exception as e:
                log.exception("处理失败 %s", cand.folder_path)
                self.log_message.emit(f"  处理失败：{e}")

    # ---------- 步骤 ----------
    def _collect_candidates(self) -> Iterable[ScanCandidate]:
        for root in self.library_paths:
            if not root.exists():
                log.warning("根目录不存在：%s", root)
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    vids = [p for p in child.rglob("*")
                            if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
                    if vids:
                        yield ScanCandidate(child, vids, clean_title(child.name))
                elif child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                    # 根目录散落文件：剧场版单文件
                    yield ScanCandidate(child.parent, [child], clean_title(child.stem))

    def _process(self, cand: ScanCandidate) -> None:
        results = self.api.search_subjects(cand.keyword, limit=1)
        if not results:
            self.log_message.emit(f"  未找到匹配：{cand.keyword}")
            return
        subj = results[0]
        bangumi_id = subj["id"]
        name = subj.get("name", "")
        name_cn = subj.get("name_cn", "") or name
        cover_url = (subj.get("images") or {}).get("large", "")
        total_eps = subj.get("total_episodes") or subj.get("eps_count") or 0

        cover_path = ""
        if cover_url:
            try:
                cover_path = str(download_cover(bangumi_id, cover_url, self.api.session))
            except Exception as e:
                log.warning("封面下载失败 %s: %s", name_cn, e)

        subject_id = self.db.upsert_subject(
            bangumi_id=bangumi_id,
            name=name,
            name_cn=name_cn,
            cover_url=cover_url,
            cover_path=cover_path,
            total_eps=total_eps,
            folder_path=str(cand.folder_path),
            match_state="auto",
        )
        self.item_matched.emit(subject_id, name_cn)

        # 拉集数（失败则用本地文件名）
        bgm_eps: list[dict] = []
        try:
            bgm_eps = self.api.get_episodes(bangumi_id)
        except BangumiError:
            pass
        ep_map = {e.get("sort") or e.get("ep"): e for e in bgm_eps}

        for v in sorted(cand.video_files):
            ep_idx = extract_ep_index(v.name) or 0.0
            bgm = ep_map.get(ep_idx) or {}
            self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=bgm.get("id"),
                ep_index=ep_idx,
                title=bgm.get("name_cn") or bgm.get("name") or v.stem,
                file_path=str(v),
            )
