# 三平台数据爬取工具

一个 PyQt 桌面工具台，用于集中启动 YouTube、TikTok、X/Twitter 三个平台的数据采集工具，并提供 AIGC 标题判断、关键词 XLSX 合并等数据处理功能。

## 环境要求

- Python 3.10+，建议使用 3.11 或 3.12。
- Windows + Chrome 或 Chromium。
- TikTok 和 X/Twitter 工具依赖 Playwright 接管浏览器。
- YouTube 工具需要 Google API Key。
- AIGC 判断工具需要 DeepSeek 兼容接口配置。

## 安装和启动

首次运行先安装依赖和 Playwright 浏览器：

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

之后启动只需要：

```bash
python main.py
```

TikTok 和 X/Twitter 工具会自动使用项目根目录下的 `user_data/` 启动 Chrome 调试浏览器。首次使用时，需要在自动打开的浏览器里登录对应平台。登录态会保存在 `user_data/`，后续通常不用重复登录。

## AIGC 配置

AIGC 判断工具需要提前配置 `.env`。推荐在项目根目录创建 `.env`：

```env
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_NAME=deepseek-chat
```

同时兼容旧变量名：`API_KEY`、`BASE_URL`、`MODEL_NAME`。

不要把真实 API Key 提交到代码仓库。

## 目录结构

- `main.py`：桌面工具台入口。
- `requirements.txt`：运行依赖。
- `src/studio/`：PyQt 主工具台、工具注册表、独立工具进程启动器。
- `src/ui/`：工具窗口公共基类。
- `src/core/`：输出路径、XLSX 写入、数字转换、文本清洗、Chrome CDP、等待机制等公共能力。
- `src/platforms/youtube/`：YouTube 采集工具。
- `src/platforms/tiktok/`：TikTok 采集工具。
- `src/platforms/x_twitter/`：X/Twitter 采集工具。
- `src/processing/`：AIGC 标题判断、关键词 XLSX 合并。
- `src/x_tweet_scraper.py`：独立的 X 博主主页帖文采集脚本，可作为备用脚本使用。
- `user_data/`：TikTok 和 X/Twitter 的浏览器登录态目录。
- `output/`：默认输出目录。

## 通用输入规则

- TXT 输入文件默认每行一条记录。
- 空行会跳过。
- 以 `#` 开头的行会跳过。
- 链接可以带参数，程序会尽量清理为标准链接。
- 多字段 TXT 通常用空格或制表符分隔。
- 输出文件默认是 `.xlsx`，写入 `output/` 下对应平台目录。
- 长任务通常会分批保存，减少中途失败造成的数据损失。

## YouTube 工具

### YouTube 关键词视频基础信息

用途：按关键词和日期范围搜索 YouTube 视频，导出基础指标。

输入：
- `Google API Key`
- 每个关键词最多视频数
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 关键词：每行一个

输出字段包括：
- 标题
- 时长
- 播放量
- 点赞数
- 发布时间
- 视频链接
- 作者主页链接

### YouTube 作者信息提取

用途：从作者主页链接 TXT 中批量提取频道资料。

TXT 格式：

```txt
https://www.youtube.com/@example
https://www.youtube.com/channel/UCxxxx
```

输出字段包括：
- 作者主页链接
- 作者名称
- 作者 ID
- 粉丝量
- 作者简介

### YouTube 目标视频前后指标

用途：读取目标视频和博主主页，定位目标视频，并导出目标前后各 5 条视频指标。

TXT 格式：

```txt
视频链接 博主主页链接
https://www.youtube.com/watch?v=xxxx https://www.youtube.com/@example
```

### YouTube 视频高赞主楼评论

用途：读取视频链接 TXT，导出每个视频点赞量最高的前 100 条主楼评论。

TXT 格式：

```txt
https://www.youtube.com/watch?v=xxxx
https://youtu.be/yyyy
```

可设置：
- 每个视频最多扫描主楼评论数

## TikTok 工具

TikTok 工具需要先登录自动打开的浏览器。若页面打不开、评论不可见或加载异常，先确认浏览器登录态和网络环境。

### TikTok 关键词视频基础信息

用途：按关键词搜索 TikTok，并按日期范围过滤视频发布时间。

输入：
- 每个关键词最多视频数
- 每个关键词最多检查候选数
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 关键词：每行一个

输出字段包括：
- 视频标题
- 播放量
- 点赞数
- 收藏量
- 评论数
- 发布时间
- 视频链接
- 作者信息

### TikTok 博主信息提取

用途：从博主主页 TXT 批量提取博主资料。

TXT 格式：

```txt
https://www.tiktok.com/@username
```

输出字段包括：
- 博主主页链接
- 博主名称
- 博主 ID
- 粉丝量
- 作者简介

### TikTok 目标视频前后指标

用途：读取目标视频和博主主页，在博主主页定位目标视频，并导出目标前后视频指标。

TXT 格式：

```txt
视频链接 博主主页链接
https://www.tiktok.com/@user/video/123 https://www.tiktok.com/@user
```

### TikTok 视频高赞主楼评论

用途：读取视频链接 TXT，抓取每个视频的主楼评论，并导出点赞量最高的评论。

TXT 格式：

```txt
https://www.tiktok.com/@user/video/123
```

规则：
- 只保留主楼评论。
- 二级回复不作为主楼评论写入。
- emoji 和文本会保留。
- 非文本内容会用类似 `[图片]` 的占位写入。
- 每爬完一个视频就保存一次。

## X/Twitter 工具

X/Twitter 工具需要先登录自动打开的浏览器。若没有登录或账号被风控，页面 DOM 可能加载不完整，采集结果会受影响。

### X 关键词媒体推文搜索

用途：按关键词和日期范围搜索 X 推文，导出含视频或图片的原创媒体推文。

输入：
- 关键词：每行一个
- 目标语言：不限、中文、英文、日文、韩文、俄文、西语、法语、德语
- 最低点赞量
- 最低评论量
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 切片跨度：按天切分搜索范围，默认 7 天

说明：
- 会跳过转推。
- 会跳过引用或嵌套推文。
- 主要保留含图片或视频的原创推文。

### X 推文作者资料提取

用途：读取推文链接 TXT，提取推文作者资料。

TXT 格式：

```txt
https://x.com/user/status/123
https://twitter.com/user/status/456
```

输出字段包括：
- 推文链接
- 作者主页
- 作者名称
- 账号 ID
- 粉丝数

### X 目标推文前后指标

用途：读取目标推文和博主主页，导出目标推文前后各 5 条推文指标。

TXT 格式：

```txt
推文链接 博主主页链接
https://x.com/user/status/123 https://x.com/user
```

说明：
- 中间可以用空格或制表符分隔。
- 如果目标推文链接能识别作者，会优先用链接里的作者信息辅助定位。

### X 指定推文指标采集

用途：读取推文链接 TXT，逐条打开推文详情页，采集指定推文的内容和互动指标。

TXT 格式：

```txt
https://x.com/user/status/123
https://x.com/user/status/456
```

输出字段：
- 序号
- 推文链接
- 推文的内容
- 浏览量
- 评论数
- 点赞数
- 转发量

规则：
- 每处理完一条推文就写入并保存。
- 默认每处理 3 条推文后随机等待 3-8 秒，降低访问频率。
- 非文本内容会用类似 `[图片]`、`[视频]`、`[GIF]`、`[卡片]` 的占位写入。

### X 博主主页帖子采集

用途：输入博主主页链接，滚动采集该博主主页公开展示的帖子。

输入：
- 博主主页链接：每行一个
- 每个主页最大滚动次数：默认 300

示例：

```txt
https://x.com/username
https://twitter.com/another_user
```

输出字段：
- 序号
- 帖子 ID
- 发布时间
- 帖子内容
- 帖子链接

说明：
- 只保留链接作者本人发布的帖子。
- 会跳过广告标记。
- 无文本但有媒体内容时，会写 `[图片]`、`[视频]`、`[GIF]`、`[卡片]`。
- 为了提高速度，脚本使用较大的滚动距离和较短等待；如果网络慢，可适当增大最大滚动次数后重跑。

### X 推文高赞主楼评论

用途：读取推文链接 TXT，扫描主楼评论，并导出点赞量最高的前 100 条评论。

TXT 格式：

```txt
https://x.com/user/status/123
```

可设置：
- 每条推文最多扫描主楼评论数

规则：
- 主推文本身不会作为评论保存。
- 只保存直接回复主推文的一级评论。
- 不保存二级回复、三级回复。
- 跳过广告或推广推文。
- 遇到 `Discover more`、`More posts`、`Relevant people`、`Who to follow` 等推荐区后停止继续抓取推荐内容。
- emoji 和文本会保留。
- 非文本内容会用类似 `[图片]` 的占位写入。

## 数据处理工具

### AIGC 标题判断

用途：读取 TXT 中的序号和标题，判断是否为 AIGC 内容，并识别主要语言。

运行前需要配置 `.env`。

### 关键词 XLSX 合并

用途：选择文件夹，默认合并文件名包含 `keyword` 的 `.xlsx` 文件，并重新生成连续序号。

## 输出文件

默认输出目录：

```txt
output/
```

按平台分目录保存，例如：

```txt
output/youtube/
output/tiktok/
output/x/
```

常见文件名示例：

```txt
x_profile_tweets_YYYYMMDD.xlsx
x_tweet_metrics_YYYYMMDD.xlsx
x_top_comments_YYYYMMDD.xlsx
tiktok_top_comments_YYYYMMDD.xlsx
```

## 运行建议

- 首次使用 TikTok 或 X/Twitter 前，先运行任意对应平台工具，让程序打开浏览器，然后完成登录。
- 不要频繁并发运行多个 X/Twitter 或 TikTok 工具，容易触发平台限制。
- 长列表任务建议分批输入，便于排查失败链接。
- 如果采集到 0 条，先手动打开对应链接确认页面是否公开可见、账号是否登录、评论区是否存在。
- 如果 X/Twitter 页面结构变化，评论层级、广告标记或推荐区边界可能需要同步调整选择器。

## 常见问题

### 缺少 Playwright

报错类似：

```txt
缺少依赖：playwright。请先安装 requirements.txt 中的依赖。
```

处理：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### X/Twitter 采集不到内容

优先检查：
- 浏览器是否已经登录 X/Twitter。
- 链接是否公开可见。
- 页面是否显示登录弹窗、验证码、风控提示。
- 目标推文或主页是否已删除、受限或私密。

### TikTok 评论为空

优先检查：
- 视频页面是否真的有评论。
- 评论区是否被关闭。
- 是否需要登录才能看到评论。
- 页面是否出现地区、年龄、敏感内容或风控限制。

### YouTube 工具报 API 错误

优先检查：
- Google API Key 是否有效。
- YouTube Data API v3 是否启用。
- API 配额是否用完。
- 日期格式是否为 `YYYY-MM-DD`。
