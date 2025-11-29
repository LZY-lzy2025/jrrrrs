from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
import uvicorn
import asyncio
import logging
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
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--ignore-certificate-errors",
                    "--mute-audio"
                ]
            )
            
            context = await browser.new_context(
                **device,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            )
            
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {} };
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    Promise.resolve({ state: 'granted' })
                );
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
            # 使用 try-except 包裹 goto 防止导航超时导致整个程序崩溃
            try:
                response = await page.goto(url, wait_until="commit", timeout=25000)
                if response:
                    logger.info(f"Response status: {response.status}")
            except Exception as e:
                logger.warning(f"Navigation warning (continuing): {e}")

            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            # 获取调试信息
            try:
                result["debug_info"]["page_title"] = await page.title()
                content = await page.evaluate("document.body.innerText")
                result["debug_info"]["page_text_preview"] = content[:500] if content else "No content"
            except:
                pass

            # --- 交互策略 ---
            logger.info("Starting interaction...")
            
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
            for _ in range(5):
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
        result["message"] = "No video traffic detected."

    # 始终返回 200 OK 和 JSON，即使失败，以便前端能看到 debug_info
    return JSONResponse(content=result)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
