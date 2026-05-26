from __future__ import annotations

from src.core import DEFAULT_TIKTOK_CDP_URL
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class TikTokKeywordWindow(SimpleToolWindow):
    tool_id = "tiktok_keyword_metrics"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 关键词搜索",
            [
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", required=True, placeholder="每行一个关键词"),
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

    def tool_config_params(self):
        return [
            ConfigParam("max_videos", "最多搜索结果数", kind="int", default=1000, minimum=1, maximum=5000),
            ConfigParam("max_candidates", "最多检查数", kind="int", default=3000, minimum=1, maximum=20000),
            ConfigParam("scroll_interval", "搜索滚动间隔(秒)", kind="float", default=0.7, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("max_search_scrolls", "最大滚动次数", kind="int", default=360, minimum=30, maximum=2000, step=10),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=12, minimum=3, maximum=50),
            ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.keyword import run_tiktok_spider

        config = {k: v for k, v in values.items() if k.startswith("tiktok_") or k in ("max_videos", "max_candidates", "scroll_interval", "max_search_scrolls", "no_new_scroll_limit", "comment_top_limit")}
        return run_tiktok_spider(
            _lines(values["keywords"]),
            int(values.get("max_videos", 1000)),
            int(values.get("max_candidates", 3000)),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokProfilesWindow(SimpleToolWindow):
    tool_id = "tiktok_profile_directory"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主信息",
            [FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@username")],
        )

    def tool_config_params(self):
        return [
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=35000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("captcha_wait", "验证码等待时间(秒)", kind="int", default=12, minimum=5, maximum=120),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.profiles import run_tiktok_profile_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "captcha_wait")}
        return run_tiktok_profile_spider(self._text_to_tempfile(values["txt_path"]), DEFAULT_TIKTOK_CDP_URL, log_callback, finish_callback, stop_event, pause_event=pause_event, config=config)


class TikTokProfileVideosWindow(SimpleToolWindow):
    tool_id = "tiktok_profile_videos"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主视频采集",
            [
                FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@username"),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("fetch_play_counts", "是否爬取播放量？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=820,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.tiktok.profile_videos import parse_date_range

        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "滚动间隔(秒)", kind="float", default=2.5, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=10, minimum=2, maximum=50),
            ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=999999),
            ConfigParam("link_batch_size", "每批处理视频数", kind="int", default=50, minimum=5, maximum=200),
            ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
            ConfigParam("cooldown_min", "冷却等待最小(秒)", kind="float", default=10.0, minimum=0.0, maximum=60.0, step=1.0, decimals=1),
            ConfigParam("cooldown_max", "冷却等待最大(秒)", kind="float", default=20.0, minimum=0.0, maximum=120.0, step=1.0, decimals=1),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.profile_videos import run_tiktok_profile_videos_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "scroll_interval", "no_new_scroll_limit", "max_scrolls", "link_batch_size", "save_batch_size", "cooldown_min", "cooldown_max")}
        return run_tiktok_profile_videos_spider(
            self._text_to_tempfile(values["txt_path"]),
            values["start_date"],
            values["end_date"],
            values["limit_time"],
            int(values.get("max_scrolls", 200)),
            "是",
            values["get_comments"],
            int(values["max_comments"]),
            values.get("fetch_play_counts", "否"),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokProfilePlayCountsWindow(SimpleToolWindow):
    tool_id = "tiktok_profile_play_counts"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主视频播放量",
            [
                FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@username"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "滚动间隔(秒)", kind="float", default=2.5, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=10, minimum=2, maximum=50),
            ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=999999),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.profile_play_counts import run_tiktok_profile_play_counts_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "scroll_interval", "no_new_scroll_limit", "max_scrolls")}
        return run_tiktok_profile_play_counts_spider(
            self._text_to_tempfile(values["txt_path"]),
            DEFAULT_TIKTOK_CDP_URL,
            int(values.get("max_scrolls", 200)),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokContextWindow(SimpleToolWindow):
    tool_id = "tiktok_paired_context_metrics"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 视频上下文数据",
            [FieldSpec("txt_path", "视频链接 + 博主主页，每行一对", kind="text_or_file", required=True, placeholder="视频链接 博主主页链接")],
        )

    def tool_config_params(self):
        return [
            ConfigParam("context_size", "目标视频前后各取几条", kind="int", default=5, minimum=1, maximum=20),
            ConfigParam("api_page_size", "每页条数", kind="int", default=35, minimum=10, maximum=100),
            ConfigParam("max_api_pages", "最多翻页数", kind="int", default=10, minimum=1, maximum=100),
            ConfigParam("max_profile_scrolls", "主页最大滚动次数", kind="int", default=80, minimum=10, maximum=500),
            ConfigParam("scroll_interval", "主页滚动间隔(秒)", kind="float", default=0.8, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.context import run_scraper

        config = {k: v for k, v in values.items() if k in ("context_size", "api_page_size", "max_api_pages", "max_profile_scrolls", "scroll_interval")}
        return run_scraper(self._text_to_tempfile(values["txt_path"]), DEFAULT_TIKTOK_CDP_URL, log_callback, finish_callback, stop_event, pause_event=pause_event, config=config)


class TikTokCommentsWindow(SimpleToolWindow):
    tool_id = "tiktok_top_comments"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 热门评论",
            [
                FieldSpec("max_scan_comments", "每个视频最多扫描评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("txt_path", "视频链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@user/video/123"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "评论滚动间隔(秒)", kind="float", default=1.4, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("max_scroll_rounds", "最大滚动次数", kind="int", default=80, minimum=5, maximum=500),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.comments import run_tiktok_top_comments_spider

        config = {k: v for k, v in values.items() if k.startswith("tiktok_") or k in ("page_load_timeout", "scroll_interval", "max_scroll_rounds", "comment_top_limit")}
        return run_tiktok_top_comments_spider(
            self._text_to_tempfile(values["txt_path"]),
            DEFAULT_TIKTOK_CDP_URL,
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )
