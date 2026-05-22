from __future__ import annotations

from pathlib import Path

from src.core import DEFAULT_TIKTOK_CDP_URL
from src.ui.base import FieldSpec, SimpleToolWindow


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class TikTokKeywordWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "TikTok 关键词视频基础信息",
            [
                FieldSpec("max_videos", "每个关键词最多视频数", kind="int", default=1000, minimum=1, maximum=5000),
                FieldSpec("max_candidates", "每个关键词最多检查候选数", kind="int", default=3000, minimum=1, maximum=20000),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="multiline", required=True),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.tiktok.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.tiktok.keyword import run_tiktok_spider

        return run_tiktok_spider(
            _lines(values["keywords"]),
            int(values["max_videos"]),
            int(values["max_candidates"]),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
        )


class TikTokProfilesWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主信息提取",
            [FieldSpec("txt_path", "博主主页 TXT", kind="file", required=True)],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.tiktok.profiles import run_tiktok_profile_spider

        return run_tiktok_profile_spider(values["txt_path"], DEFAULT_TIKTOK_CDP_URL, log_callback, finish_callback, stop_event)


class TikTokProfileVideosWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主主页视频指标采集",
            [
                FieldSpec("txt_path", "博主主页 TXT", kind="file", required=True),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("max_scrolls", "最大滚动上限 (-1为无限)", kind="int", default=-1, minimum=-1, maximum=999999),
                FieldSpec("get_video_info", "是否获取视频信息？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=660,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.tiktok.profile_videos import parse_date_range

        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.tiktok.profile_videos import run_tiktok_profile_videos_spider

        return run_tiktok_profile_videos_spider(
            values["txt_path"],
            values["start_date"],
            values["end_date"],
            values["limit_time"],
            int(values["max_scrolls"]),
            values["get_video_info"],
            values["get_comments"],
            int(values["max_comments"]),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
        )


class TikTokContextWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "TikTok 目标视频前后指标",
            [FieldSpec("txt_path", "视频链接 + 博主主页 TXT", kind="file", required=True)],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.tiktok.context import run_scraper

        return run_scraper(values["txt_path"], DEFAULT_TIKTOK_CDP_URL, log_callback, finish_callback, stop_event)


class TikTokCommentsWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "TikTok 视频高赞主楼评论",
            [
                FieldSpec("max_scan_comments", "每个视频最多扫描主楼评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("txt_path", "视频链接 TXT", kind="file", required=True),
            ],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.tiktok.comments import run_tiktok_top_comments_spider

        return run_tiktok_top_comments_spider(
            values["txt_path"],
            DEFAULT_TIKTOK_CDP_URL,
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
        )
