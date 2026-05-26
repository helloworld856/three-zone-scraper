from __future__ import annotations

from src.core import DEFAULT_X_CDP_URL, debug_port_from_cdp_url
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class XKeywordWindow(SimpleToolWindow):
    tool_id = "x_keyword_video_search"

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "关键词爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="同时处理几个关键词。1=顺序处理。最大3。"),
            ConfigParam("max_comment_tabs", "评论爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="每个关键词同时用几个tab采集评论。1=顺序。最大3。"),
            ConfigParam("max_queue_size", "评论队列最大长度", kind="int", default=5000, minimum=10, maximum=10000,
                        tooltip="待爬评论链接的缓冲上限。满了则暂停采集新推文。"),
            ConfigParam("slice_days", "时间切片跨度(天)", kind="int", default=7, minimum=1, maximum=365),
            ConfigParam("search_page_timeout", "页面加载超时(毫秒)", kind="int", default=40000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("cooldown_min", "冷却等待最小(秒)", kind="float", default=5.0, minimum=0.5, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("cooldown_max", "冷却等待最大(秒)", kind="float", default=7.0, minimum=0.5, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=5, minimum=2, maximum=30),
            ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=999999),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 关键词搜索",
            [
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", default="AI生成动画\nAI animation", required=True, placeholder="每行一个关键词"),
                FieldSpec(
                    "lang",
                    "目标语言",
                    kind="combo",
                    default="日文 (ja)",
                    options=("不限 (Any)", "中文 (zh)", "英文 (en)", "日文 (ja)", "韩文 (ko)", "俄文 (ru)", "西语 (es)", "法语 (fr)", "德语 (de)"),
                ),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("get_comments", "是否获取推文评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=500, minimum=10, maximum=10000),
            ],
            height=820,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            if not values.get("start_date") or not values.get("end_date"):
                raise ValueError("开始日期和结束日期不能为空。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.keyword import run_x_spider

        lang_map = {
            "不限 (Any)": "any",
            "中文 (zh)": "zh",
            "英文 (en)": "en",
            "日文 (ja)": "ja",
            "韩文 (ko)": "ko",
            "俄文 (ru)": "ru",
            "西语 (es)": "es",
            "法语 (fr)": "fr",
            "德语 (de)": "de",
        }
        adv_params = {
            "lang": lang_map.get(values["lang"], "any"),
            "limit_time": values["limit_time"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "get_comments": values["get_comments"],
            "max_comments": int(values["max_comments"]),
        }
        config = {k: v for k, v in values.items() if k in ("slice_days", "search_page_timeout", "cooldown_min", "cooldown_max", "no_new_scroll_limit", "max_scrolls", "max_parallel_tabs", "max_comment_tabs", "max_queue_size")}
        return run_x_spider(_lines(values["keywords"]), adv_params, debug_port_from_cdp_url(DEFAULT_X_CDP_URL), log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class XProfilesWindow(SimpleToolWindow):
    tool_id = "x_tweet_author_profiles"

    def tool_config_params(self):
        return [
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("tweet_ready_timeout", "推文渲染等待(毫秒)", kind="int", default=12000, minimum=3000, maximum=60000, step=1000),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 博主信息",
            [
                FieldSpec(
                    "input_mode",
                    "输入方式",
                    kind="combo",
                    default="推文链接",
                    options=("推文链接", "博主链接"),
                ),
                FieldSpec("txt_path", "链接列表，每行一个", kind="text_or_file", required=True, placeholder="每行一个链接"),
            ],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.profiles import run_scraper

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "tweet_ready_timeout")}
        return run_scraper(
            self._text_to_tempfile(values["txt_path"]),
            values["input_mode"],
            DEFAULT_X_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class XContextWindow(SimpleToolWindow):
    tool_id = "x_paired_context_metrics"

    def tool_config_params(self):
        return [
            ConfigParam("context_size", "目标推文前后各取几条", kind="int", default=5, minimum=1, maximum=20),
            ConfigParam("max_profile_scrolls", "主页最大滚动次数", kind="int", default=45, minimum=5, maximum=300),
            ConfigParam("scroll_interval", "主页滚动间隔(秒)", kind="float", default=3.8, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 推文上下文数据",
            [FieldSpec("txt_path", "推文链接 + 博主主页，每行一对", kind="text_or_file", required=True, placeholder="推文链接 博主主页链接")],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.context import run_scraper

        config = {k: v for k, v in values.items() if k in ("context_size", "max_profile_scrolls", "scroll_interval", "page_load_timeout")}
        return run_scraper(self._text_to_tempfile(values["txt_path"]), DEFAULT_X_CDP_URL, log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class XTweetMetricsWindow(SimpleToolWindow):
    tool_id = "x_tweet_metrics"

    def tool_config_params(self):
        return [
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=30000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 推文详情采集",
            [
                FieldSpec("txt_path", "推文链接，每行一个", kind="text_or_file", required=True, placeholder="https://x.com/user/status/123"),
                FieldSpec("get_comments", "是否获取推文评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=500, minimum=10, maximum=10000),
            ],
        )
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.tweet_metrics import run_x_tweet_metrics_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "comment_top_limit")}
        return run_x_tweet_metrics_spider(self._text_to_tempfile(values["txt_path"]), values["get_comments"], int(values["max_comments"]), DEFAULT_X_CDP_URL, log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class XProfileTweetsWindow(SimpleToolWindow):
    tool_id = "x_profile_tweets"

    def tool_config_params(self):
        return [
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=30000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "滚动间隔(秒)", kind="float", default=3.2, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=10, minimum=2, maximum=50),
            ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=5000),
            ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
            ConfigParam("cooldown_min", "冷却等待最小(秒)", kind="float", default=6.0, minimum=0.0, maximum=60.0, step=1.0, decimals=1),
            ConfigParam("cooldown_max", "冷却等待最大(秒)", kind="float", default=15.0, minimum=0.0, maximum=120.0, step=1.0, decimals=1),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 博主推文采集",
            [
                FieldSpec(
                    "profile_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://x.com/username",
                    required=True,
                ),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("get_comments", "是否获取推文评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=500, minimum=10, maximum=10000),
            ],
            height=760,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        if not _lines(values["profile_urls"]):
            raise ValueError("至少需要输入一个 X 博主主页链接。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.profile_tweets import run_x_profile_tweets_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "scroll_interval", "no_new_scroll_limit", "max_scrolls", "save_batch_size", "cooldown_min", "cooldown_max")}
        return run_x_profile_tweets_spider(
            values["profile_urls"],
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            DEFAULT_X_CDP_URL,
            int(values.get("max_scrolls", 200)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class XCommentsWindow(SimpleToolWindow):
    tool_id = "x_top_comments"

    def tool_config_params(self):
        return [
            ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=30000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "评论滚动间隔(秒)", kind="float", default=4.0, minimum=0.1, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=5, minimum=2, maximum=30),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 热门评论",
            [
                FieldSpec("max_scan_comments", "每条推文最多扫描主楼评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("txt_path", "推文链接，每行一个", kind="text_or_file", required=True, placeholder="https://x.com/user/status/123"),
            ],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.comments import run_x_top_comments_spider

        config = {k: v for k, v in values.items() if k in ("comment_top_limit", "page_load_timeout", "scroll_interval", "no_new_scroll_limit")}
        return run_x_top_comments_spider(
            self._text_to_tempfile(values["txt_path"]),
            DEFAULT_X_CDP_URL,
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )
