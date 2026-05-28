from __future__ import annotations

from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class YouTubeKeywordWindow(SimpleToolWindow):
    tool_id = "youtube_keyword_mining"

    def __init__(self) -> None:
        super().__init__(
            "YouTube 关键词搜索",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", required=True, placeholder="每行一个关键词"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=760,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.youtube.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_results", "最多搜索结果数", kind="int", default=5000, minimum=1, maximum=5000),
            ConfigParam("youtube_search_batch_size", "搜索每页条数", kind="int", default=50, minimum=1, maximum=50),
            ConfigParam("youtube_video_batch_size", "视频详情每批条数", kind="int", default=50, minimum=1, maximum=50),
            ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.keyword import run_youtube_spider

        config = {k: v for k, v in values.items() if k.startswith("youtube_") or k in ("max_results", "comment_top_limit")}
        return run_youtube_spider(
            values["api_key"],
            _lines(values["keywords"]),
            int(values.get("max_results", 5000)),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class YouTubeProfilesWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "YouTube 博主信息",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.youtube.com/@username"),
            ],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.profiles import run_channel_spider

        return run_channel_spider(values["api_key"], self._text_to_tempfile(values["txt_path"]), log_callback, finish_callback, stop_event, pause_event=pause_event)


class YouTubeContextWindow(SimpleToolWindow):
    tool_id = "youtube_paired_context_metrics"

    def __init__(self) -> None:
        super().__init__(
            "YouTube 视频上下文数据",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("txt_path", "视频链接 + 博主主页，每行一对", kind="text_or_file", required=True, placeholder="视频链接 博主主页链接"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("context_size", "目标视频前后各取几条", kind="int", default=5, minimum=1, maximum=20),
            ConfigParam("max_upload_pages", "最多翻页数", kind="int", default=200, minimum=10, maximum=1000),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.context import run_youtube_paired_context_spider

        config = {k: v for k, v in values.items() if k in ("context_size", "max_upload_pages")}
        return run_youtube_paired_context_spider(values["api_key"], self._text_to_tempfile(values["txt_path"]), log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class YouTubeChannelWorksWindow(SimpleToolWindow):
    tool_id = "youtube_channel_works"

    def __init__(self) -> None:
        super().__init__(
            "YouTube 博主作品采集",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec(
                    "channel_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://www.youtube.com/@username",
                    required=True,
                ),
                FieldSpec("collect_target", "采集目标", kind="combo", options=("全部", "仅视频与Shorts", "仅帖子 (Posts)"), default="全部"),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=760,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        if not _lines(values["channel_urls"]):
            raise ValueError("至少需要输入一个 YouTube 博主主页链接。")
        if values.get("limit_time") == "是":
            from src.platforms.youtube.keyword import parse_date_range
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_video_items", "最多作品数", kind="int", default=5000, minimum=1, maximum=5000),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_delay", "滚动间隔(秒)", kind="float", default=0.8, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=6, minimum=2, maximum=50),
            ConfigParam("scroll_px", "每次滚动像素(px)", kind="int", default=2800, minimum=500, maximum=10000, step=100),
            ConfigParam("max_post_scrolls", "帖子最大滚动次数", kind="int", default=200, minimum=1, maximum=5000),
            ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.channel_works import run_youtube_channel_works_spider

        config = {k: v for k, v in values.items() if k in ("max_video_items", "page_load_timeout", "scroll_delay", "no_new_scroll_limit", "scroll_px", "max_post_scrolls", "save_batch_size")}
        return run_youtube_channel_works_spider(
            values["api_key"],
            values["channel_urls"],
            values.get("collect_target", "全部"),
            int(values.get("max_video_items", 5000)),
            int(values.get("max_post_scrolls", 200)),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class YouTubeCommentsWindow(SimpleToolWindow):
    tool_id = "youtube_top_comments"

    def __init__(self) -> None:
        super().__init__(
            "YouTube 视频数据与评论采集",
            [
                FieldSpec("api_key", "Google API Key", required=True),
                FieldSpec("txt_path", "视频链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.youtube.com/watch?v=xxxx"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_scan_comments", "最多获取评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("check_type", "是否精确检测视频长短类型？", kind="combo", options=("是", "否"), default="否"),
            ],
        )
        self.bind_field_visibility("get_comments", "是", ["max_scan_comments"])

    def tool_config_params(self):
        return [
            ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
            ConfigParam("youtube_api_page_size", "评论每页条数", kind="int", default=100, minimum=1, maximum=100),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.comments import run_youtube_video_metrics_spider

        config = {k: v for k, v in values.items() if k.startswith("youtube_") or k in ("comment_top_limit",)}
        return run_youtube_video_metrics_spider(
            values["api_key"],
            self._text_to_tempfile(values["txt_path"]),
            values.get("get_comments", "否"),
            values.get("check_type", "否"),
            int(values.get("max_scan_comments", 500)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )
