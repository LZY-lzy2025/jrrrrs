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
            # 使用 iPhone 13 Pro 配置
            device = p.devices['iPhone 13 Pro']
            
            logger.info("Launching browser...")
            # <<< 修改点 1: 使用 'headless=new' 模式，反检测能力更强
            browser = await p.chromium.launch(
                headless="new", # 使用新的 headless 模式
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
            
            # 合并配置，避免参数冲突
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
            
            # 注入更深层的反指纹脚本 (这部分保持不变，已经很完善了)
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (parameters.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : originalQuery(parameters));
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

            # --- 监听器设置 (监听器从一开始就开启，不会错过任何请求) ---
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
                        logger.info(f"Captured: {req_url}")
                        # 捕获到的URL存入列表
                        result["debug_info"]["all_video_candidates"].append(req_url)
                except Exception:
                    pass # 静默处理监听器中的小错误

            page.on("request", handle_request)

            logger.info(f"Navigating to {url}...")
            # <<< 修改点 2: 先 'commit' 导航，进行 Cloudflare 检测
            response = await page.goto(url, wait_until="commit", timeout=45000)
            if response:
                logger.info(f"Response status: {response.status}")

            # --- Cloudflare 绕过逻辑 (保持不变，很健壮) ---
            await asyncio.sleep(3)
            page_title = await page.title()
            result["debug_info"]["page_title"] = page_title
            
            if "Cloudflare" in page_title or "Attention" in page_title or "Just a moment" in page_title:
                logger.info("Cloudflare detected! Attempting specific bypass...")
                await asyncio.sleep(6)
                try:
                    frames = page.frames
                    for frame in frames:
                        try:
                            box = await frame.bounding_box()
                            if box:
                                x = box['x'] + 10 + random.randint(0, 20)
                                y = box['y'] + 10 + random.randint(0, 20)
                                await page.mouse.click(x, y)
                        except: pass
                        try:
                            checkbox = frame.locator("input[type='checkbox'], #challenge-stage, .ctp-checkbox-label").first
                            if await checkbox.count() > 0:
                                await checkbox.click(force=True)
                        except: pass
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.warning(f"Bypass interaction failed: {e}")
            
            # <<< 修改点 3: 核心流程重构 -> 等待页面完全加载，再交互，再等待
            logger.info("Waiting for the page to be fully loaded (including videos, scripts, etc.)...")
            # 'load' 状态确保所有资源（图片、CSS、JS）都已下载完成，播放器脚本大概率已初始化
            await page.wait_for_load_state("load", timeout=15000)

            # 模拟真实用户，等待片刻后再操作
            await asyncio.sleep(random.uniform(1, 3))

            # 获取页面内容预览
            try:
                content = await page.evaluate("document.body.innerText")
                result["debug_info"]["page_text_preview"] = content[:500] if content else "No content"
            except:
                pass

            logger.info("Attempting to interact with the video player...")
            # --- 视频交互策略 (保持不变，已经很全面) ---
            try:
                # 1. 暴力点击页面中心
                viewport = page.viewport_size
                if viewport:
                    await page.mouse.click(viewport['width'] / 2, viewport['height'] / 2)
                    await asyncio.sleep(1) # 给反应时间

                # 2. 遍历所有 Frame 并尝试点击播放器元素
                frames = page.frames
                for frame in frames:
                    try:
                        await frame.locator("video").first.click(timeout=1000, force=True)
                    except:
                        pass
                    try:
                        # 尝试点击各种可能的播放按钮/图标
                        btns = frame.locator("div[class*='play'], button[aria-label*='play'], button[class*='play'], img[src*='play']")
                        count = await btns.count()
                        for i in range(min(count, 5)):
                            try:
                                await btns.nth(i).click(timeout=500, force=True)
                            except:
                                pass
                    except:
                        pass
            except Exception as e:
                logger.warning(f"Click interaction failed: {e}")

            # <<< 修改点 4: 播放后，明确等待3秒，让视频请求发出
            logger.info("Video clicked. Waiting 3 seconds for video stream requests to be initiated...")
            await asyncio.sleep(3)

    except Exception as e:
        logger.error(f"Critical Process Error: {e}")
        result["error"] = str(e)
        result["status"] = "error"
    finally:
        if browser:
            await browser.close()
            logger.info("Browser closed.")

    # --- 结果整理 (保持不变) ---
    result["debug_info"]["domains_contacted"] = list(domains_set)
    candidates = result["debug_info"]["all_video_candidates"]
    
    for c in candidates:
        if "szsummer.cn" in c: # 你的特定规则
            result["video_url"] = c
            result["source_type"] = "szsummer_match"
            break
            
    if not result["video_url"] and candidates:
        m3u8s = [c for c in candidates if ".m3u8" in c]
        if m3u8s:
            result["video_url"] = m3u8s[0]
            result["source_type"] = "generic_m3u8"
        else:
            result["video_url"] = candidates[0]
            result["source_type"] = "fallback"

    if result["video_url"]:
        result["status"] = "success"
        logger.info(f"Successfully found video URL: {result['video_url']}")
    elif result["status"] != "error":
        result["status"] = "failed"
        # 调整一下失败信息，更具体
        result["message"] = "No video traffic detected after interaction. The site may have changed its player or protection."
        logger.warning(result['message'])

    return JSONResponse(content=result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)

