from __future__ import annotations

from pathlib import Path

from src.ui.base import FieldSpec, SimpleToolWindow


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class YouTubeKeywordWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "YouTube 关键词视频基础信息",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("max_results", "每个关键词最多视频数", kind="int", default=1000, minimum=1, maximum=5000),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="multiline", required=True),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=660,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.youtube.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.youtube.keyword import run_youtube_spider

        return run_youtube_spider(
            values["api_key"],
            _lines(values["keywords"]),
            int(values["max_results"]),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            log_callback,
            finish_callback,
            stop_event,
        )


class YouTubeProfilesWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "YouTube 作者信息提取",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("txt_path", "作者主页 TXT", kind="file", required=True),
            ],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.youtube.profiles import run_channel_spider

        return run_channel_spider(values["api_key"], values["txt_path"], log_callback, finish_callback, stop_event)


class YouTubeContextWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "YouTube 目标视频前后指标",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("txt_path", "视频链接 + 博主主页 TXT", kind="file", required=True),
            ],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.youtube.context import run_youtube_paired_context_spider

        return run_youtube_paired_context_spider(values["api_key"], values["txt_path"], log_callback, finish_callback, stop_event)


class YouTubeChannelWorksWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "YouTube 作者主页作品采集",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec(
                    "channel_urls",
                    "作者主页链接，每行一个",
                    kind="multiline",
                    placeholder="https://www.youtube.com/@username",
                    required=True,
                ),
                FieldSpec("max_video_items", "每个作者最多视频/Shorts数", kind="int", default=500, minimum=1, maximum=5000),
                FieldSpec("max_post_scrolls", "Posts 最大滚动次数", kind="int", default=120, minimum=1, maximum=5000),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=660,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        if not _lines(values["channel_urls"]):
            raise ValueError("至少需要输入一个 YouTube 作者主页链接。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.youtube.channel_works import run_youtube_channel_works_spider

        return run_youtube_channel_works_spider(
            values["api_key"],
            values["channel_urls"],
            int(values["max_video_items"]),
            int(values["max_post_scrolls"]),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            log_callback,
            finish_callback,
            stop_event,
        )


class YouTubeCommentsWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "YouTube 视频高赞主楼评论",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("max_scan_comments", "每个视频最多扫描主楼评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("txt_path", "视频链接 TXT", kind="file", required=True),
            ],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.youtube.comments import run_youtube_top_comments_spider

        return run_youtube_top_comments_spider(
            values["api_key"],
            values["txt_path"],
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
        )
