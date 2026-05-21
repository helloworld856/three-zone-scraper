from __future__ import annotations

from pathlib import Path

from src.core import DEFAULT_X_CDP_URL, debug_port_from_cdp_url
from src.ui.base import FieldSpec, SimpleToolWindow


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class XKeywordWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "X 关键词媒体推文搜索",
            [
                FieldSpec("keywords", "关键词，每行一个", kind="multiline", default="AI生成动画\nAI animation", required=True),
                FieldSpec(
                    "lang",
                    "目标语言",
                    kind="combo",
                    default="日文 (ja)",
                    options=("不限 (Any)", "中文 (zh)", "英文 (en)", "日文 (ja)", "韩文 (ko)", "俄文 (ru)", "西语 (es)", "法语 (fr)", "德语 (de)"),
                ),
                FieldSpec("min_faves", "最低点赞量"),
                FieldSpec("min_replies", "最低评论量"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE, required=True),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE, required=True),
                FieldSpec("slice_days", "切片跨度（天）", kind="int", default=7, minimum=1, maximum=365),
            ],
            height=620,
        )

    def validate_values(self, values):
        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if not values["start_date"] or not values["end_date"]:
            raise ValueError("开始日期和结束日期不能为空。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
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
            "min_faves": values["min_faves"],
            "min_replies": values["min_replies"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "slice_days": str(values["slice_days"]),
        }
        return run_x_spider(_lines(values["keywords"]), adv_params, debug_port_from_cdp_url(DEFAULT_X_CDP_URL), log_callback, finish_callback, stop_event)


class XProfilesWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "X 推文作者资料提取",
            [
                FieldSpec(
                    "input_mode",
                    "输入方式",
                    kind="combo",
                    default="推文链接",
                    options=("推文链接", "博主链接"),
                ),
                FieldSpec("txt_path", "TXT 文件", kind="file", required=True),
            ],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.x_twitter.profiles import run_scraper

        return run_scraper(
            values["txt_path"],
            values["input_mode"],
            DEFAULT_X_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
        )


class XContextWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "X 目标推文前后指标",
            [FieldSpec("txt_path", "推文链接 + 博主主页 TXT", kind="file", required=True)],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.x_twitter.context import run_scraper

        return run_scraper(values["txt_path"], DEFAULT_X_CDP_URL, log_callback, finish_callback, stop_event)


class XTweetMetricsWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "X 指定推文指标采集",
            [FieldSpec("txt_path", "推文链接 TXT", kind="file", required=True)],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.x_twitter.tweet_metrics import run_x_tweet_metrics_spider

        return run_x_tweet_metrics_spider(values["txt_path"], DEFAULT_X_CDP_URL, log_callback, finish_callback, stop_event)


class XProfileTweetsWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "X 博主主页帖子采集",
            [
                FieldSpec(
                    "profile_urls",
                    "博主主页链接，每行一个",
                    kind="multiline",
                    placeholder="https://x.com/username",
                    required=True,
                ),
                FieldSpec("max_scrolls", "每个主页最大滚动次数", kind="int", default=300, minimum=1, maximum=5000),
            ],
        )

    def validate_values(self, values):
        if not _lines(values["profile_urls"]):
            raise ValueError("至少需要输入一个 X 博主主页链接。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.x_twitter.profile_tweets import run_x_profile_tweets_spider

        return run_x_profile_tweets_spider(
            values["profile_urls"],
            DEFAULT_X_CDP_URL,
            int(values["max_scrolls"]),
            log_callback,
            finish_callback,
            stop_event,
        )


class XCommentsWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "X 推文高赞主楼评论",
            [
                FieldSpec("max_scan_comments", "每条推文最多扫描主楼评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("txt_path", "推文链接 TXT", kind="file", required=True),
            ],
        )

    def validate_values(self, values):
        if not Path(values["txt_path"]).exists():
            raise ValueError("TXT 文件不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event):
        from src.platforms.x_twitter.comments import run_x_top_comments_spider

        return run_x_top_comments_spider(
            values["txt_path"],
            DEFAULT_X_CDP_URL,
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
        )
