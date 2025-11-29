from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
import uvicorn
import asyncio
import logging
import random
import os  # 新增：用于读取环境变量
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
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", 
                    "--disable-setuid-sandbox", 
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled", # 关键：隐藏自动化特征
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
                # 更新更现代的 iOS UserAgent
                "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
                # 添加 Referer 尝试欺骗
                "extra_http_headers": {
                    "Referer": url,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                }
            })
            
            context = await browser.new_context(**context_options)
            
            # 注入更深层的反指纹脚本
            await context.add_init_script("""
                // 1. 隐藏 WebDriver
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                
                // 2. 伪造 Chrome 对象
                window.navigator.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                
                // 3. 伪造 Permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
                
                // 4. 伪造 Plugins (iOS 通常没有插件，但为了覆盖默认的空数组特征)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                // 5. 伪造 WebGL Vendor
                try {
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        // UNMASKED_VENDOR_WEBGL
                        if (parameter === 37445) return 'Apple Inc.';
                        // UNMASKED_RENDERER_WEBGL
                        if (parameter === 37446) return 'Apple GPU';
                        return getParameter(parameter);
                    };
                } catch(e) {}
            """)

            page = await context.new_page()

            # --- 监听器设置 ---

            def handle_websocket(ws):
                try:
                    url = ws.url
                    domains_set.add(urlparse(url).netloc)
                    if "szsummer" in url or ".flv" in url or ".m3u8" in url:
                        result["debug_info"]["all_video_candidates"].append(url)
                except Exception as e:
                    logger.error(f"WS Error: {e}")

            page.on("websocket", handle_websocket)

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
                        result["debug_info"]["all_video_candidates"].append(req_url)
                except Exception as e:
                    pass

            page.on("request", handle_request)

            logger.info("Navigating...")
            # 使用 try-except 包裹 goto 防止导航超时
            try:
                # 增加超时时间到 45000ms (45秒)，应对可能的网络延迟
                response = await page.goto(url, wait_until="commit", timeout=45000)
                if response:
                    logger.info(f"Response status: {response.status}")
            except Exception as e:
                logger.warning(f"Navigation warning (continuing): {e}")

            # --- Cloudflare 绕过逻辑 ---
            # 等待一小会儿检查标题
            await asyncio.sleep(3)
            page_title = await page.title()
            result["debug_info"]["page_title"] = page_title
            
            if "Cloudflare" in page_title or "Attention" in page_title or "Just a moment" in page_title:
                logger.info("Cloudflare detected! Attempting specific bypass...")
                
                # 1. 等待可能的自动跳转 (有些只是5秒盾)
                await asyncio.sleep(6)
                
                # 2. 尝试寻找并点击 iframe 中的 checkbox (Turnstile)
                try:
                    # 查找所有 iframe
                    frames = page.frames
                    for frame in frames:
                        # 尝试点击 frame 中心
                        try:
                            box = await frame.bounding_box()
                            if box:
                                # 随机偏移一点，模拟人类点击
                                x = box['x'] + 10 + random.randint(0, 20)
                                y = box['y'] + 10 + random.randint(0, 20)
                                await page.mouse.click(x, y)
                        except:
                            pass
                        
                        # 尝试点击特定的 challenge 元素
                        try:
                            checkbox = frame.locator("input[type='checkbox'], #challenge-stage, .ctp-checkbox-label").first
                            if await checkbox.count() > 0:
                                await checkbox.click(force=True)
                        except:
                            pass
                    
                    # 再等待跳转
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.warning(f"Bypass interaction failed: {e}")

            await page.wait_for_load_state("domcontentloaded")

            # 获取页面内容预览
            try:
                content = await page.evaluate("document.body.innerText")
                result["debug_info"]["page_text_preview"] = content[:500] if content else "No content"
            except:
                pass

            # --- 视频交互策略 ---
            if not result["debug_info"]["all_video_candidates"]:
                logger.info("Starting video interaction...")
                
                # 1. 暴力点击
                try:
                    viewport = page.viewport_size
                    if viewport:
                        await page.mouse.click(viewport['width'] / 2, viewport['height'] / 2)
                        await asyncio.sleep(0.5)
                        await page.mouse.click(viewport['width'] / 2, viewport['height'] / 3)
                except Exception as e:
                    logger.warning(f"Click interaction failed: {e}")
                
                # 2. 遍历 Frame 点击
                frames = page.frames
                for frame in frames:
                    try:
                        await frame.locator("video").first.click(timeout=500, force=True)
                    except:
                        pass
                    try:
                        btns = frame.locator("div[class*='play'], button, img[src*='play']")
                        count = await btns.count()
                        for i in range(min(count, 3)):
                            try:
                                await btns.nth(i).click(timeout=300, force=True)
                            except:
                                pass
                    except:
                        pass

                # 等待结果
                for i in range(8): # 延长等待时间
                    if result["debug_info"]["all_video_candidates"]: break
                    await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Critical Process Error: {e}")
        result["error"] = str(e)
        result["status"] = "error"
    finally:
        # 安全关闭浏览器
        if browser:
            await browser.close()

    # --- 结果整理 ---
    result["debug_info"]["domains_contacted"] = list(domains_set)
    candidates = result["debug_info"]["all_video_candidates"]
    
    # 筛选逻辑
    for c in candidates:
        if "szsummer.cn" in c:
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
        result["message"] = "No video traffic detected. Likely blocked by Cloudflare."

    return JSONResponse(content=result)

if __name__ == "__main__":
    # --- 关键修改：读取环境变量 PORT ---
    # Leaflow 等云平台会通过 PORT 环境变量告诉应用应该监听哪个端口
    # 如果没有读取到，则默认使用 8000
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
