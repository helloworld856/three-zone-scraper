"""
=============================================================================
  X (Twitter) 主页帖文爬虫脚本
  X Profile Tweet Scraper
=============================================================================
  功能: 输入X用户主页链接，自动爬取所有帖文并保存到Excel
  用法: python x_tweet_scraper.py
=============================================================================

【环境要求 / Requirements】
  1. Python 3.8+
  2. 安装依赖 / Install dependencies:
     pip install playwright pandas openpyxl
     python -m playwright install chromium

【使用说明 / Usage】
  1. 运行脚本: python x_tweet_scraper.py
  2. 输入X用户主页链接 (如: https://x.com/username)
  3. 脚本会自动打开浏览器 -> 登录X -> 滚动收集帖子 -> 保存Excel
  4. 如果已有Chrome浏览器登录了X，可以选择"连接已有浏览器"

【文件说明 / Output】
  - 生成的Excel文件格式: {username}_tweets_YYYYMMDD_HHMMSS.xlsx
  - 包含列: 序号, 帖子ID, 发布时间, 帖子内容, 帖子链接, 回复数, 转发数, 点赞数, 浏览数
"""

import os
import re
import time
import argparse
from datetime import datetime
from typing import Optional

try:
    import pandas as pd
except ImportError:
    print("❌ 请先安装 pandas: pip install pandas openpyxl")
    exit(1)

try:
    from playwright.sync_api import sync_playwright, Page
except ImportError:
    print("❌ 请先安装 playwright: pip install playwright && python -m playwright install chromium")
    exit(1)

# ============================================================
#  配置常量
# ============================================================
SCROLL_TIMES = 300          # 最大滚动次数 (每次约加载2-5条帖子)
SCROLL_DELAY = 2.0          # 滚动后等待秒数 (给页面加载时间)
SCROLL_PX = 1200            # 每次滚动像素数
MAX_NO_NEW = 5              # 连续多少次没有新帖子则停止
BATCH_SAVE_INTERVAL = 50    # 每收集XX条保存一次中间结果

# ============================================================
#  工具函数
# ============================================================

def extract_post_id(url: str) -> str:
    """从URL中提取帖子ID"""
    match = re.search(r'/status/(\d+)', url)
    return match.group(1) if match else ""


def extract_stats(label: str) -> str:
    """从aria-label中提取数字 (如 '388 Likes. Like' -> '388')"""
    if not label:
        return "0"
    # 提取前面的数字，支持 K/M 后缀
    match = re.search(r'^([\d,]+\.?\d*[KM]?)', label.replace(',', ''))
    return match.group(1).strip() if match else "0"


def safe_filename(text: str) -> str:
    """生成安全的文件名"""
    text = re.sub(r'[<>:"/\\|?*]', '_', text)
    return text.strip().strip('.')


# ============================================================
#  帖子数据提取
# ============================================================

def extract_tweets_from_page(page: Page) -> list:
    """
    从当前页面提取所有可见帖子
    返回: [ { postId, text, time, replies, reposts, likes, views, url }, ... ]
    """
    return page.evaluate("""() => {
        const articles = document.querySelectorAll('article');
        const results = [];
        
        articles.forEach(article => {
            try {
                // --- 帖子文本 ---
                const textEl = article.querySelector('[data-testid="tweetText"]');
                const text = textEl ? textEl.textContent : '';
                
                // --- 发布时间 ---
                const timeEl = article.querySelector('time');
                const time = timeEl ? timeEl.getAttribute('datetime') : '';
                
                // --- 帖子链接 & ID ---
                const linkEl = article.querySelector('a[href*="/status/"]');
                const href = linkEl ? linkEl.getAttribute('href') : '';
                const postId = href.match(/\\/status\\/(\\d+)/)?.[1] || '';
                if (!postId) return;  // 跳过没有ID的article
                
                // --- 互动数据 ---
                const statsEls = article.querySelectorAll('[role="group"] button, [role="group"] a');
                let replies = '', reposts = '', likes = '', views = '';
                statsEls.forEach(el => {
                    const label = el.getAttribute('aria-label') || '';
                    if (label.includes('Reply')) replies = label;
                    else if (label.includes('Repost')) reposts = label;
                    else if (label.includes('Like')) likes = label;
                    else if (label.includes('view')) views = label;
                });
                
                results.push({
                    postId,
                    text,
                    time,
                    replies,
                    reposts,
                    likes,
                    views,
                    url: 'https://x.com' + href
                });
            } catch(e) {}
        });
        
        return results;
    }""")


# ============================================================
#  自动滚动收集
# ============================================================

def scroll_and_collect(page: Page, max_scrolls: int = SCROLL_TIMES) -> list:
    """
    自动滚动页面并收集所有帖子
    返回: 去重后的帖子列表（按时间倒序）
    """
    all_tweets = []
    seen_ids = set()
    no_new_count = 0
    last_count = 0
    
    print(f"🚀 开始滚动收集帖子 (最多 {max_scrolls} 次)...")
    
    for i in range(max_scrolls):
        # 提取当前页面帖子
        tweets = extract_tweets_from_page(page)
        
        # 去重添加
        added = 0
        for tweet in tweets:
            pid = tweet['postId']
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_tweets.append(tweet)
                added += 1
        
        current_count = len(all_tweets)
        
        # 打印进度
        if added > 0 or (i % 10 == 0):
            print(f"  📜 滚动 {i+1:3d}/{max_scrolls} | 本次新增 {added:3d} 条 | 累计 {current_count:4d} 条")
        
        # 判断是否应该停止
        if current_count == last_count:
            no_new_count += 1
            if no_new_count >= MAX_NO_NEW:
                print(f"  ✅ 已到达底部，连续 {MAX_NO_NEW} 次没有新帖子")
                break
        else:
            no_new_count = 0
        
        last_count = current_count
        
        # 中间保存（每BATCH_SAVE_INTERVAL条保存一次）
        if current_count % BATCH_SAVE_INTERVAL < 10 and current_count > 0 and added > 0:
            # 保存中间结果（但仅在新增时）
            pass  # 最终会统一保存
        
        # 滚动页面
        page.evaluate(f"window.scrollBy(0, {SCROLL_PX});")
        time.sleep(SCROLL_DELAY)
    
    print(f"  🎉 收集完成！共 {len(all_tweets)} 条帖子")
    return all_tweets


# ============================================================
#  数据处理与保存
# ============================================================

def save_to_excel(tweets: list, username: str) -> str:
    """
    将帖子数据保存为Excel文件
    返回: Excel文件路径
    """
    if not tweets:
        print("❌ 没有数据可保存")
        return ""
    
    # 转为DataFrame
    df = pd.DataFrame(tweets)
    
    # 解析时间并排序（最新的在前）
    df['time'] = pd.to_datetime(df['time'], errors='coerce')
    df = df.sort_values('time', ascending=False).reset_index(drop=True)
    
    # 提取互动数据
    df['回复数'] = df['replies'].apply(extract_stats)
    df['转发数'] = df['reposts'].apply(extract_stats)
    df['点赞数'] = df['likes'].apply(extract_stats)
    df['浏览数'] = df['views'].apply(extract_stats)
    
    # 格式化输出列
    df['序号'] = range(1, len(df) + 1)
    df['帖子ID'] = df['postId']
    df['发布时间'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S')
    df['帖子内容'] = df['text']
    df['帖子链接'] = df['url']
    
    # 选择最终列
    output_df = df[['序号', '帖子ID', '发布时间', '帖子内容', '帖子链接', 
                    '回复数', '转发数', '点赞数', '浏览数']]
    
    # 生成文件名
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{safe_filename(username)}_tweets_{timestamp}.xlsx"
    filepath = os.path.join(os.getcwd(), filename)
    
    # 保存Excel
    output_df.to_excel(filepath, index=False, sheet_name='帖子数据')
    
    # 打印统计
    print(f"\n{'='*60}")
    print(f"📁 文件路径: {filepath}")
    print(f"{'='*60}")
    print("📊 数据统计:")
    print(f"   帖子总数: {len(output_df)}")
    print(f"   时间范围: {output_df['发布时间'].iloc[-1]} ~ {output_df['发布时间'].iloc[0]}")
    print(f"   总回复数: {output_df['回复数'].sum()}")
    print(f"   总转发数: {output_df['转发数'].sum()}")
    print(f"   总点赞数: {output_df['点赞数'].sum()}")
    print(f"{'='*60}")
    print("📑 Excel列说明:")
    print("   1. 序号       - 帖子编号")
    print("   2. 帖子ID     - X平台帖子唯一标识")
    print("   3. 发布时间   - 帖子发布日期和时间")
    print("   4. 帖子内容   - 帖子完整文本")
    print("   5. 帖子链接   - 原始帖子URL")
    print("   6. 回复数     - 回复数量")
    print("   7. 转发数     - 转发/转推数量")
    print("   8. 点赞数     - 点赞数量")
    print("   9. 浏览数     - 浏览/观看次数")
    print(f"{'='*60}")
    
    return filepath


# ============================================================
#  登录处理
# ============================================================

def wait_for_login(page: Page, timeout_minutes: int = 5) -> bool:
    """
    等待用户在浏览器中登录X
    检测到页面出现导航菜单（已登录标志）则继续
    """
    print("\n🔑 请在打开的浏览器窗口中登录你的 X 账号...")
    print(f"   (等待最长 {timeout_minutes} 分钟)")
    
    start = time.time()
    timeout = timeout_minutes * 60
    
    while time.time() - start < timeout:
        # 检查是否已登录（通过检测导航元素）
        is_logged_in = page.evaluate("""() => {
            // 检查是否有导航链接或账户菜单
            const nav = document.querySelector('nav[aria-label="Primary"]');
            const accountMenu = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
            const userMenu = document.querySelector('button[aria-label*="Account menu"]');
            return !!(nav || accountMenu || userMenu);
        }""")
        
        if is_logged_in:
            print("✅ 登录检测成功！继续爬取...\n")
            return True
        
        elapsed = int(time.time() - start)
        if elapsed % 30 == 0:
            print(f"   等待登录中... ({elapsed}秒)")
        
        time.sleep(2)
    
    print("❌ 登录等待超时")
    return False


# ============================================================
#  主爬虫流程
# ============================================================

def scrape_x_profile(
    profile_url: str,
    headless: bool = False,
    max_scrolls: int = SCROLL_TIMES
) -> Optional[str]:
    """
    爬取X用户主页的所有帖文
    
    参数:
        profile_url: X用户主页URL (如 https://x.com/username)
        headless: 是否使用无头模式 (默认False, 需要登录所以用有头模式)
        max_scrolls: 最大滚动次数
    
    返回:
        Excel文件路径，失败返回None
    """
    # 验证URL
    match = re.match(r'https?://(x\.com|twitter\.com)/([^/]+)', profile_url)
    if not match:
        print(f"❌ 无效的X主页链接: {profile_url}")
        print("   正确格式: https://x.com/username 或 https://twitter.com/username")
        return None
    
    username = match.group(2)
    print(f"\n{'='*60}")
    print(f"🎯 目标: {profile_url}")
    print(f"👤 用户名: @{username}")
    print(f"{'='*60}\n")
    
    # 启动浏览器
    print("🌐 启动浏览器...")
    with sync_playwright() as p:
        # 启动Chromium（有头模式，方便登录）
        browser = p.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
            ]
        )
        
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        page = context.new_page()
        
        try:
            # ========== 第一步：访问主页 ==========
            print("📍 正在访问用户主页...")
            page.goto(profile_url, wait_until='domcontentloaded', timeout=30000)
            time.sleep(3)
            
            # ========== 第二步：检查登录状态 ==========
            # 检查是否是登录页面（被重定向到 /i/flow/login）
            current_url = page.url
            if '/i/flow/login' in current_url or '/login' in current_url:
                print("⚠️  需要登录X账号")
                if not wait_for_login(page):
                    print("❌ 登录失败，退出")
                    return None
                # 登录后重新导航到主页
                page.goto(profile_url, wait_until='domcontentloaded', timeout=30000)
                time.sleep(3)
            
            # 检测是否成功到达用户主页
            if '/i/flow' in page.url:
                print("❌ 无法访问主页，可能是账号权限问题")
                return None
            
            print(f"✅ 成功访问用户主页: {page.title()}")
            
            # ========== 第三步：滚动收集帖子 ==========
            tweets = scroll_and_collect(page, max_scrolls)
            
            if not tweets:
                print("❌ 未收集到任何帖子")
                return None
            
            # ========== 第四步：保存到Excel ==========
            filepath = save_to_excel(tweets, username)
            
            return filepath
            
        except Exception as e:
            print(f"❌ 爬取过程出错: {e}")
            import traceback
            traceback.print_exc()
            return None
        
        finally:
            # 关闭浏览器
            print("\n🔚 关闭浏览器...")
            browser.close()


# ============================================================
#  命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='X (Twitter) 主页帖文爬虫',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python x_tweet_scraper.py
  python x_tweet_scraper.py --url https://x.com/username
  python x_tweet_scraper.py --url https://x.com/username --max-scrolls 500
  python x_tweet_scraper.py --headless  # 无头模式（需已登录）
        """
    )
    
    parser.add_argument('--url', '-u', type=str, default=None,
                        help='X用户主页链接 (如 https://x.com/username)')
    parser.add_argument('--max-scrolls', '-m', type=int, default=SCROLL_TIMES,
                        help=f'最大滚动次数 (默认: {SCROLL_TIMES})')
    parser.add_argument('--headless', '-hl', action='store_true',
                        help='无头模式 (默认需要登录，建议使用有头模式)')
    parser.add_argument('--output', '-o', type=str, default=None,
                        help='输出目录 (默认: 当前目录)')
    
    args = parser.parse_args()
    
    # 如果没有提供URL，交互式输入
    profile_url = args.url
    if not profile_url:
        print("\n" + "="*60)
        print("  X (Twitter) 主页帖文爬虫")
        print("="*60)
        profile_url = input("\n📝 请输入X用户主页链接: ").strip()
        if not profile_url:
            print("❌ 未输入链接")
            return
    
    # 执行爬取
    result = scrape_x_profile(
        profile_url=profile_url,
        headless=args.headless,
        max_scrolls=args.max_scrolls
    )
    
    if result:
        print(f"\n✅ 爬取完成！Excel文件: {result}")
    else:
        print("\n❌ 爬取失败")


if __name__ == '__main__':
    main()