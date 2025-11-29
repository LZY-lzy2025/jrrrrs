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
            "domains_contacted": set(),
            "all_video_candidates": []
        }
    }
    
    async with async_playwright() as p:
        # 使用 iPhone 13 Pro 配置，伪装更彻底
        device = p.devices['iPhone 13 Pro']
        
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--ignore-certificate-errors", # 忽略证书错误
                "--mute-audio"
            ]
        )
        
        context = await browser.new_context(
            **device,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            # 强制指定高版本 UserAgent
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
        
        # 注入反爬脚本
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

        # 1. 监听 WebSocket (新增)
        def handle_websocket(ws):
            url = ws.url
            logger.info(f"WebSocket detected: {url}")
            result["debug_info"]["domains_contacted"].add(urlparse(url).netloc)
            if "szsummer" in url or ".flv" in url or ".m3u8" in url:
                result["debug_info"]["all_video_candidates"].append(url)

        page.on("websocket", handle_websocket)

        # 2. 监听 HTTP 请求
        def handle_request(request):
            req_url = request.url
            
            # 记录访问过的域名，用于排查是否被重定向或加载了什么
            try:
                result["debug_info"]["domains_contacted"].add(urlparse(req_url).netloc)
            except:
                pass

            # 视频特征匹配
            is_video = False
            content_type = request.headers.get("content-type", "").lower()
            
            if ".m3u8" in req_url or "mpegurl" in content_type:
                is_video = True
            elif ".mp4" in req_url:
                is_video = True
            elif ".flv" in req_url or "video/x-flv" in content_type:
                is_video = True
            elif ".ts" in req_url and "segment" not in req_url: # 或者是 .ts 切片
                # 记录 ts 切片但不作为首选，除非没别的
                pass 

            if is_video:
                logger.info(f"Captured: {req_url}")
                result["debug_info"]["all_video_candidates"].append(req_url)

        page.on("request", handle_request)

        try:
            logger.info("Navigating...")
            # 增加 waitUntil: 'commit' 只要服务器响应就开始交互，防止卡在加载圈
            try:
                response = await page.goto(url, wait_until="commit", timeout=25000)
                if response:
                    logger.info(f"Response status: {response.status}")
            except Exception as e:
                logger.warning(f"Navigation soft timeout: {e}")

            # 等待 DOM 加载
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            # 获取调试信息：标题和页面内容
            try:
                result["debug_info"]["page_title"] = await page.title()
                # 获取 body 的前 500 个字符，看看是不是显示 "Access Denied" 或者 "403"
                content = await page.evaluate("document.body.innerText")
                result["debug_info"]["page_text_preview"] = content[:500] if content else "No content"
            except:
                pass

            # --- 交互策略 ---
            
            # 1. 查找 iframe
            frames = page.frames
            logger.info(f"Found {len(frames)} frames")

            # 2. 暴力点击中心 + 寻找 Play 按钮
            viewport = page.viewport_size
            if viewport:
                # 点击屏幕中心
                await page.mouse.click(viewport['width'] / 2, viewport['height'] / 2)
                await asyncio.sleep(0.5)
                # 点击屏幕上方 1/3 处 (视频通常在这里)
                await page.mouse.click(viewport['width'] / 2, viewport['height'] / 3)
            
            # 3. 遍历点击
            for frame in frames:
                try:
                    # 尝试点击 video 标签
                    await frame.locator("video").first.click(timeout=1000, force=True)
                except:
                    pass
                try:
                    # 尝试点击任何看起来像播放按钮的东西
                    btns = frame.locator("div[class*='play'], button, img[src*='play']")
                    count = await btns.count()
                    for i in range(min(count, 3)): # 只点前3个，省时间
                        try:
                            await btns.nth(i).click(timeout=500, force=True)
                        except:
                            pass
                except:
                    pass

            # 等待请求捕获
            for _ in range(5):
                if result["debug_info"]["all_video_candidates"]: break
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Error: {e}")
            result["error"] = str(e)
        finally:
            await browser.close()

    # --- 结果处理 ---
    candidates = result["debug_info"]["all_video_candidates"]
    
    # 1. 优先找 szsummer
    for c in candidates:
        if "szsummer.cn" in c:
            result["video_url"] = c
            result["source_type"] = "szsummer_match"
            break
            
    # 2. 其次找 m3u8
    if not result["video_url"]:
        for c in candidates:
            if ".m3u8" in c:
                result["video_url"] = c
                result["source_type"] = "generic_m3u8"
                break
    
    # 3. 兜底
    if not result["video_url"] and candidates:
        result["video_url"] = candidates[0]
        result["source_type"] = "fallback"

    # 4. 转换 set 为 list 以便 JSON 序列化
    result["debug_info"]["domains_contacted"] = list(result["debug_info"]["domains_contacted"])

    if result["video_url"]:
        result["status"] = "success"
        # 成功时简化返回，但也保留 debug_info 供查看
        return result
    else:
        # 失败时返回 200 但 status=failed，这样你能看到 debug_info
        result["status"] = "failed"
        result["message"] = "No video traffic detected. Check debug_info for page status."
        return JSONResponse(content=result)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
