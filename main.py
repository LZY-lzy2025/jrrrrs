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

@app.get("/")
async def root():
    return {"status": "running", "message": "Use /extract?url=... to extract m3u8"}

@app.get("/extract")
async def extract_m3u8(url: str = Query(..., description="The target video page URL")):
    logger.info(f"Start sniffing: {url}")
    
    # 数据容器
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
    
    # 临时集合用于去重域名
    domains_set = set()
    browser = None

    try:
        async with async_playwright() as p:
            # 使用 iPhone 13 Pro 配置 (模拟手机端通常能获得更简单的播放器结构)
            device = p.devices['iPhone 13 Pro']
            
            logger.info("Launching browser...")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", 
                    "--disable-setuid-sandbox", 
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled", 
                    "--disable-features=IsolateOrigins,site-per-process",
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
            
            # 反指纹注入 (保持不变)
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                try {
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        if (parameter === 37445) return 'Apple Inc.';
                        if (parameter === 37446) return 'Apple GPU';
                        return getParameter(parameter);
                    };
                } catch(e) {}
            """)

            page = await context.new_page()

            # --- 监听器设置 ---
            # 这个监听器是全程后台运行的，所以我们在后面 sleep 等待的时候，它依然在工作
            def handle_request(request):
                try:
                    req_url = request.url
                    domains_set.add(urlparse(req_url).netloc)

                    is_video = False
                    content_type = request.headers.get("content-type", "").lower()
                    
                    # 宽松的判定条件，确保能抓到
                    if ".m3u8" in req_url or "application/vnd.apple.mpegurl" in content_type or "application/x-mpegurl" in content_type:
                        is_video = True
                    elif ".mp4" in req_url:
                        is_video = True
                    elif ".flv" in req_url or "video/x-flv" in content_type:
                        is_video = True
                    
                    if is_video:
                        logger.info(f"Captured video request: {req_url}")
                        result["debug_info"]["all_video_candidates"].append(req_url)
                except Exception as e:
                    pass

            page.on("request", handle_request)

            logger.info("Navigating...")
            try:
                # 1. 访问页面
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                
                # 2. 关键修改：等待页面完全静止 (Network Idle)
                # 这对应你要求的“加载完成之后”
                logger.info("Waiting for network idle (page fully loaded)...")
                try:
                    # 等待直到网络连接变为空闲（至少500ms内没有新的网络请求），或者最长等待 10 秒
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    logger.warning("Network idle timeout, continuing anyway...")

            except Exception as e:
                logger.warning(f"Navigation warning: {e}")

            # --- Cloudflare 简单处理 ---
            page_title = await page.title()
            result["debug_info"]["page_title"] = page_title
            
            if "Cloudflare" in page_title or "Just a moment" in page_title:
                logger.info("Cloudflare detected, waiting extra time...")
                await asyncio.sleep(5)
                # 尝试点击盾牌
                try:
                    frames = page.frames
                    for frame in frames:
                        checkbox = frame.locator("input[type='checkbox'], #challenge-stage").first
                        if await checkbox.count() > 0:
                            await checkbox.click(force=True)
                            await asyncio.sleep(3)
                except:
                    pass

            # --- 视频交互与抓取 ---
            if not result["debug_info"]["all_video_candidates"]:
                logger.info("Starting interaction sequence...")
                
                # 遍历所有 iframe 和主页面寻找播放按钮
                frames = page.frames
                interaction_happened = False
                
                for frame in frames:
                    try:
                        # 尝试定位常见的播放按钮或视频元素
                        # 1. 视频标签
                        video_tag = frame.locator("video").first
                        if await video_tag.count() > 0:
                            logger.info(f"Found video tag in frame {frame.url}, clicking...")
                            await video_tag.click(force=True, timeout=1000)
                            interaction_happened = True
                        
                        # 2. 常见的播放按钮类名/ID
                        play_btns = frame.locator("div[class*='play'], button[class*='play'], .vjs-big-play-button, img[src*='play']")
                        count = await play_btns.count()
                        if count > 0:
                            logger.info(f"Found {count} play buttons in frame {frame.url}, clicking first one...")
                            await play_btns.first.click(force=True, timeout=1000)
                            interaction_happened = True
                            
                    except Exception as e:
                        pass
                
                # 如果没有找到明显的按钮，尝试点击屏幕中心（很多H5播放器整个区域都是点击播放）
                if not interaction_happened:
                    logger.info("No buttons found, trying center screen click...")
                    try:
                        viewport = page.viewport_size
                        if viewport:
                            await page.mouse.click(viewport['width'] / 2, viewport['height'] / 2)
                    except:
                        pass

                # 3. 关键修改：点击后，显式等待 3 秒
                # 这对应你要求的“播放三秒后再... 抓取”
                # 在这 3 秒内，后台的 handle_request 依然在运行，会捕获视频流请求
                logger.info("Clicked. Waiting 3-5 seconds for video stream to start...")
                await asyncio.sleep(4) 

    except Exception as e:
        logger.error(f"Critical Process Error: {e}")
        result["error"] = str(e)
        result["status"] = "error"
    finally:
        if browser:
            await browser.close()

    # --- 结果整理 ---
    result["debug_info"]["domains_contacted"] = list(domains_set)
    candidates = result["debug_info"]["all_video_candidates"]
    
    # 简单的去重
    candidates = list(set(candidates))
    result["debug_info"]["all_video_candidates"] = candidates

    # 筛选逻辑
    for c in candidates:
        if "szsummer" in c: # 针对特定源的保留逻辑
            result["video_url"] = c
            result["source_type"] = "szsummer_match"
            break
            
    if not result["video_url"] and candidates:
        # 优先找 m3u8
        m3u8s = [c for c in candidates if ".m3u8" in c]
        if m3u8s:
            result["video_url"] = m3u8s[0]
            result["source_type"] = "generic_m3u8"
        else:
            result["video_url"] = candidates[0]
            result["source_type"] = "fallback"

    if result["video_url"]:
        result["status"] = "success"
    elif result["status"] != "error":
        result["status"] = "failed"
        result["message"] = "No video traffic detected."

    return JSONResponse(content=result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
