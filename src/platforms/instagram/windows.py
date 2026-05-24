from __future__ import annotations

from src.core import DEFAULT_X_CDP_URL
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class InstagramProfileWorksWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "Instagram 作者主页作品采集",
            [
                FieldSpec(
                    "profile_urls",
                    "作者主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://www.instagram.com/username/",
                    required=True,
                ),
            ],
            height=620,
        )

    def validate_values(self, values):
        if not _lines(values["profile_urls"]):
            raise ValueError("至少需要输入一个 Instagram 作者主页链接。")

    def tool_config_params(self):
        return [
            ConfigParam("max_works", "每个作者最多作品数", kind="int", default=5000, minimum=1, maximum=100000),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_delay", "滚动间隔(秒)", kind="float", default=3.0, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("scroll_px", "每次滚动像素", kind="int", default=2600, minimum=500, maximum=10000, step=100),
            ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=8, minimum=2, maximum=50),
            ConfigParam("max_scrolls", "每个主页最大滚动次数", kind="int", default=200, minimum=1, maximum=5000),
            ConfigParam("save_batch_size", "每N条保存一次", kind="int", default=10, minimum=1, maximum=100),
            ConfigParam("cooldown_min", "批量等待最小(秒)", kind="float", default=10.0, minimum=0.0, maximum=60.0, step=1.0, decimals=1),
            ConfigParam("cooldown_max", "批量等待最大(秒)", kind="float", default=25.0, minimum=0.0, maximum=120.0, step=1.0, decimals=1),
            ConfigParam("detail_delay_min", "详情页间隔最小(秒)", kind="float", default=3.0, minimum=0.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("detail_delay_max", "详情页间隔最大(秒)", kind="float", default=7.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.instagram.works import run_instagram_profile_works_spider

        config = {k: v for k, v in values.items() if k in ("max_works", "page_load_timeout", "scroll_delay", "scroll_px", "no_new_scroll_limit", "max_scrolls", "save_batch_size", "cooldown_min", "cooldown_max", "detail_delay_min", "detail_delay_max")}
        return run_instagram_profile_works_spider(
            values["profile_urls"],
            DEFAULT_X_CDP_URL,
            int(values.get("max_works", 5000)),
            int(values.get("max_scrolls", 200)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )
