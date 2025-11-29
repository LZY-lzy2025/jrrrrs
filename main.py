from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright, Page
import uvicorn
import asyncio
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("m3u8_sniffer")

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "running", "message": "Use /extract?url=... to extract m3u8"}

@app.get("/extract")
async def extract_m3u8(url: str = Query(..., description="The target video page URL")):
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL format")

    logger.info(f"Start sniffing: {url}")
    
    # 存储所有捕获到的 m3u8，以便做兜底策略
    captured_m3u8s = []
    
    async with async_playwright() as p:
        # 增加防检测参数
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", # 关键：隐藏自动化特征
            ]
        )
        
        # 模拟真实浏览器特征
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN"
        )
        
        # 注入脚本以规避 webdriver 检测
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = await context.new_page()

        # 监听请求
        def handle_request(request):
            req_url = request.url
            # 只要是 m3u8 就记录下来
            if ".m3u8" in req_url or "application/x-mpegURL" in request.headers.get("content-type", ""):
                logger.info(f"CAPTURED CANDIDATE: {req_url}")
                captured_m3u8s.append(req_url)

        page.on("request", handle_request)

        try:
            # 1. 访问页面 (增加超时时间到 30秒)
            logger.info("Navigating to page...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"Navigation timeout or error (continuing anyway): {e}")

            # 2. 尝试点击播放 (递归查找所有 Frame)
            # 定义一个点击函数，用于在任意 frame 中寻找播放按钮
            async def click_play_in_frames():
                # 常见的播放器选择器
                selectors = [
                    ".vjs-big-play-button", 
                    ".dplayer-mobile-play",
                    ".dplayer-play-icon",
                    "button[class*='play']", 
                    "div[class*='play']",
                    "svg[class*='play']",
                    "video",
                    "#player",
                    ".poster" # 点击封面图
                ]
                
                # 遍历主页面和所有 iframe
                frames = page.frames
                logger.info(f"Scanning {len(frames)} frames for play buttons...")
                
                for frame in frames:
                    for sel in selectors:
                        try:
                            # 检查元素是否存在且可见
                            if await frame.locator(sel).count() > 0:
                                if await frame.locator(sel).first.is_visible():
                                    logger.info(f"Clicking '{sel}' in frame: {frame.url}")
                                    await frame.locator(sel).first.click(timeout=500, force=True)
                                    await asyncio.sleep(0.5) # 点击后稍微缓冲
                        except Exception:
                            continue

            # 第一轮点击
            await click_play_in_frames()
            
            # 等待 5 秒看是否有请求
            for _ in range(5):
                if len(captured_m3u8s) > 0: break
                await asyncio.sleep(1)
            
            # 如果还没抓到，尝试模拟鼠标移动和再次点击（有些播放器需要 hover）
            if not captured_m3u8s:
                logger.info("Retry interaction...")
                try:
                    # 模拟鼠标移动到屏幕中心点击
                    await page.mouse.click(960, 540)
                except:
                    pass
                await click_play_in_frames()
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Error during processing: {e}")
        finally:
            await browser.close()

    # 结果筛选逻辑
    target_m3u8 = None
    
    # 策略 1: 优先寻找包含 szsummer.cn 的链接
    for m in captured_m3u8s:
        if "szsummer.cn" in m:
            target_m3u8 = m
            break
            
    # 策略 2: 如果没找到指定域名的，但抓到了其他 m3u8，返回第一个
    if not target_m3u8 and captured_m3u8s:
        logger.info("Specific domain not found, returning fallback m3u8.")
        target_m3u8 = captured_m3u8s[0]

    if target_m3u8:
        return {
            "code": 200,
            "original_url": url,
            "m3u8_url": target_m3u8,
            "all_candidates": captured_m3u8s  # 返回所有候选项供调试
        }
    else:
        # 只有在列表完全为空时才报 404
        raise HTTPException(status_code=404, detail="Could not find any m3u8 request. The site might be blocking headless browsers or the video failed to load.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
