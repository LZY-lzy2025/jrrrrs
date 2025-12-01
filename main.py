from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
import uvicorn
import asyncio
import logging
import random
import os
from urllib.parse import urlparse

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("m3u8_sniffer")

app = FastAPI()

# --- 1. 防止 favicon.ico 404 干扰 ---
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return JSONResponse(content={})

@app.get("/")
async def root():
    # --- 2. 增加版本号，方便你确认代码是否生效 ---
    return {
        "status": "running", 
        "version": "V2_Wait_Logic", 
        "message": "Use /extract?url=... to extract m3u8"
    }

@app.get("/extract")
async def extract_m3u8(url: str = Query(..., description="The target video page URL")):
    logger.info(f"Start sniffing: {url}")
    
    result = {
        "status": "processing",
        "video_url": None,
        "source_type": None,
        "debug_info": {
            "page_title": "Unknown",
            "page_text_preview": "",
            "domains_contacted": [],
            "all_video_candidates": []
        },
        "error": None
    }
    
    domains_set = set()
    browser = None

    try:
        async with async_playwright() as p:
            # 模拟 iPhone 13 Pro
            device = p.devices['iPhone 13 Pro']
            
            logger.info("Launching browser...")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", 
                    "--disable-setuid-sandbox", 
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled", 
                    "--ignore-certificate-errors",
                    "--mute-audio"
                ]
            )
            
            context_options = device.copy()
            context_options.update({
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
                "extra_http_headers": {
                    "Referer": url,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                }
            })
            
            context = await browser.new_context(**context_options)
            
            # 注入反检测脚本
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            """)

            page = await context.new_page()

            # --- 监听器：全程后台录制 ---
            # 只要浏览器发起请求，这里就会记录。即使主程序在 sleep，这里也在工作。
            def handle_request(request):
                try:
                    req_url = request.url
                    domains_set.add(urlparse(req_url).netloc)

                    is_video = False
                    content_type = request.headers.get("content-type", "").lower()
                    
                    if ".m3u8" in req_url or "mpegurl" in content_type:
                        is_video = True
                    elif ".mp4" in req_url:
                        is_video = True
                    elif ".flv" in req_url or "video/x-flv" in content_type:
                        is_video = True
                    
                    if is_video:
                        logger.info(f"Captured video request: {req_url}")
                        result["debug_info"]["all_video_candidates"].append(req_url)
                except:
                    pass

            page.on("request", handle_request)

            logger.info("Navigating...")
            try:
                # 1. 访问页面
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                
                # --- 核心逻辑 1：等待页面加载完成 ---
                logger.info("Waiting for network idle (page loading)...")
                try:
                    # networkidle 表示至少500ms内没有新的网络请求，代表加载完毕
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except:
                    logger.warning("Network idle timeout, continuing anyway...")

            except Exception as e:
                logger.warning(f"Navigation warning: {e}")

            # Cloudflare 简单跳过
            try:
                title = await page.title()
                result["debug_info"]["page_title"] = title
                if "Cloudflare" in title:
                    await asyncio.sleep(5)
            except:
                pass

            # --- 核心逻辑 2：点击播放 ---
            if not result["debug_info"]["all_video_candidates"]:
                logger.info("Trying to click play button...")
                
                # 尝试点击常见播放按钮
                clicked = False
                try:
                    # 优先找 video 标签
                    frames = page.frames
                    for frame in frames:
                        # 策略A: 点击 video 元素
                        video = frame.locator("video").first
                        if await video.count() > 0:
                            await video.click(force=True, timeout=1000)
                            clicked = True
                            logger.info(f"Clicked video tag in frame: {frame.url}")
                        
                        # 策略B: 点击播放按钮图标
                        btns = frame.locator("div[class*='play'], button[class*='play'], img[src*='play']")
                        if await btns.count() > 0:
                            await btns.first.click(force=True, timeout=1000)
                            clicked = True
                            logger.info("Clicked play button icon")

                    # 策略C: 如果都没找到，点击屏幕中心 (针对 H5 遮罩层)
                    if not clicked:
                        viewport = page.viewport_size
                        if viewport:
                            await page.mouse.click(viewport['width'] / 2, viewport['height'] / 2)
                            logger.info("Clicked center of screen")
                except Exception as e:
                    logger.warning(f"Click interaction error: {e}")

                # --- 核心逻辑 3：点击后等待 3+ 秒抓取 ---
                # 在这 4 秒内，如果有 m3u8 请求产生，handle_request 会自动记录
                logger.info("Clicked. Waiting 4 seconds for video traffic...")
                await asyncio.sleep(4)

    except Exception as e:
        logger.error(f"Critical Error: {e}")
        result["error"] = str(e)
        result["status"] = "error"
    finally:
        if browser:
            await browser.close()

    # 结果去重与选择
    candidates = list(set(result["debug_info"]["all_video_candidates"]))
    result["debug_info"]["all_video_candidates"] = candidates
    result["debug_info"]["domains_contacted"] = list(domains_set)

    # 优先选 m3u8
    m3u8s = [c for c in candidates if ".m3u8" in c]
    if m3u8s:
        result["video_url"] = m3u8s[0]
        result["source_type"] = "m3u8_found"
    elif candidates:
        result["video_url"] = candidates[0]
        result["source_type"] = "fallback_video"
    
    if result["video_url"]:
        result["status"] = "success"
    else:
        result["status"] = "failed"
        result["message"] = "No video traffic detected after interaction."

    return JSONResponse(content=result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    
    # --- 调试：打印所有注册的路由，确保 /extract 存在 ---
    logger.info("--- Registering Routes ---")
    for route in app.routes:
        logger.info(f"Route: {route.path} [{route.name}]")
    logger.info("--------------------------")
    
    logger.info(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
```

### 2. 关键操作建议

要解决 `Not Found` 问题，请务必执行以下操作：

1.  **强制重新构建（重要）**：
    Docker 有时候会缓存旧的代码层。请在部署时使用 `--no-cache` 参数，或者先删除旧镜像。
    ```bash
    # 如果你是用 Docker Compose
    docker-compose build --no-cache
    docker-compose up -d

    # 如果你是直接用 Docker build
    docker build --no-cache -t your-image-name .
