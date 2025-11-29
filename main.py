from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright
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
    
    captured_data = {
        "m3u8": [],
        "mp4": [],
        "flv": []
    }
    
    async with async_playwright() as p:
        # 使用 iPhone 12 的设备配置，这样能自动处理 UserAgent, Viewport, DPR, 和 Touch 支持
        device = p.devices['iPhone 12']
        
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process", # 关键：允许跨域 iframe 访问
                "--mute-audio" # 静音，防止音频相关报错
            ]
        )
        
        # 创建上下文：使用移动端配置
        context = await browser.new_context(
            **device, # 自动应用 iPhone 配置
            locale="zh-CN",
            timezone_id="Asia/Shanghai"
        )
        
        # 再次注入防检测脚本
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.navigator.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        # 监听请求
        def handle_request(request):
            req_url = request.url
            resource_type = request.resource_type
            
            # 过滤不需要的资源以节省日志
            if resource_type in ['image', 'font', 'stylesheet']:
                return

            # 检查 URL 或 Content-Type
            is_video = False
            
            if ".m3u8" in req_url or "application/vnd.apple.mpegurl" in str(request.headers.get("content-type", "")):
                captured_data["m3u8"].append(req_url)
                logger.info(f"✅ CAPTURED M3U8: {req_url}")
                is_video = True
            elif ".mp4" in req_url:
                captured_data["mp4"].append(req_url)
                is_video = True
            elif ".flv" in req_url:
                captured_data["flv"].append(req_url)
                is_video = True
            
            if is_video:
                # 打印详细 Headers 方便调试 auth_key 来源
                logger.info(f"Headers: {request.headers}")

        page.on("request", handle_request)

        try:
            logger.info("Navigating to page...")
            # 缩短超时时间，有些页面一直在加载广告，我们不需要等完全加载
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                logger.warning(f"Page load timeout (continuing): {e}")

            # 等待 2 秒让播放器初始化
            await asyncio.sleep(2)

            # --- 交互策略 ---
            
            # 1. 暴力点击：模拟用户点击屏幕中心（通常是播放大按钮的位置）
            logger.info("Attempting blind center tap...")
            try:
                # 获取视口大小
                viewport = page.viewport_size
                if viewport:
                    cx, cy = viewport['width'] / 2, viewport['height'] / 3 # 点击偏上一点，避开底部控制栏
                    await page.mouse.click(cx, cy)
                    await asyncio.sleep(1)
                    await page.mouse.click(cx, cy) # 双击有时能触发
            except Exception as e:
                logger.warning(f"Blind tap failed: {e}")

            # 2. 智能查找并点击
            async def interact_with_frames():
                frames = page.frames
                play_selectors = [
                    "video",
                    ".vjs-big-play-button", 
                    "div[class*='play']",
                    "button[class*='play']",
                    "img[src*='play']",
                    ".dplayer-mobile-play"
                ]
                
                for frame in frames:
                    try:
                        # 尝试点击 video 标签本身
                        videos = frame.locator("video")
                        count = await videos.count()
                        if count > 0:
                            logger.info(f"Found video tag in frame {frame.url}, tapping...")
                            await videos.first.click(force=True)
                            await asyncio.sleep(0.5)
                        
                        # 尝试点击播放按钮
                        for sel in play_selectors:
                            btn = frame.locator(sel).first
                            if await btn.count() > 0 and await btn.is_visible():
                                logger.info(f"Clicking selector {sel}")
                                await btn.click(force=True)
                    except:
                        pass
            
            await interact_with_frames()
            
            # 3. 等待捕获
            for i in range(8):
                if captured_data["m3u8"]: break
                logger.info(f"Waiting for traffic... {i}")
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Global error: {e}")
        finally:
            await browser.close()

    # 优先返回 szsummer.cn 的链接
    target = None
    all_urls = captured_data["m3u8"] + captured_data["mp4"] + captured_data["flv"]
    
    if not all_urls:
         raise HTTPException(status_code=404, detail="Could not find any video request. Site might be blocking IP or using WebSocket.")

    # 筛选逻辑
    for u in captured_data["m3u8"]:
        if "szsummer.cn" in u:
            target = u
            break
            
    if not target and all_urls:
        target = all_urls[0]

    return {
        "code": 200,
        "m3u8_url": target,
        "all_candidates": all_urls
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
