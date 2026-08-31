"""用 Playwright 打开微信视频号登录页，截图并 dump HTML 结构"""
import os, sys
from playwright.sync_api import sync_playwright

dump_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_dumps")
os.makedirs(dump_dir, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    # 1. 打开登录页
    print("=== 打开 login.html ===", flush=True)
    page.goto("https://channels.weixin.qq.com/login.html", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(5000)
    print(f"URL: {page.url}", flush=True)
    print(f"Title: {page.title()}", flush=True)
    
    page.screenshot(path=os.path.join(dump_dir, "login_page.png"), full_page=True)
    with open(os.path.join(dump_dir, "login_page.html"), "w") as f:
        f.write(page.content())
    print("截图和HTML已保存", flush=True)

    # 提取所有可见文本
    try:
        texts = page.evaluate("""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            const texts = [];
            while (walker.nextNode()) {
                const txt = walker.currentNode.textContent.trim();
                if (txt && txt.length > 1) texts.push(txt);
            }
            return texts.join('\\n');
        }""")
        with open(os.path.join(dump_dir, "login_text.txt"), "w") as f:
            f.write(texts)
        print(f"可见文本: {len(texts)} 字符", flush=True)
        print("---前500字符---", flush=True)
        print(texts[:500], flush=True)
    except Exception as e:
        print(f"文本提取失败: {e}", flush=True)

    # 也看看首页
    print("\n=== 打开首页 ===", flush=True)
    page.goto("https://channels.weixin.qq.com/", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(5000)
    print(f"URL: {page.url}", flush=True)
    print(f"Title: {page.title()}", flush=True)
    page.screenshot(path=os.path.join(dump_dir, "home_page.png"), full_page=True)

    browser.close()
    print("\n=== 完成 ===", flush=True)
