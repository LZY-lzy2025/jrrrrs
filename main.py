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
    return {"status": "running", "message": "Use /?url=YOUR_TARGET_URL to extract m3u8"}

@app.get("/extract")
async def extract_m3u8(url: str = Query(..., description="The target video page URL")):
    """
    访问目标页面，模拟点击，并嗅探包含 szsummer.cn 的 m3u8 请求。
    """
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL format")

    logger.info(f"Start sniffing: {url}")
    
    found_m3u8 = None
    
    async with async_playwright() as p:
        # 启动 Chromium 浏览器 (headless模式)
        # args参数是为了在Docker容器中更稳定地运行
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        # 创建上下文，模拟手机或桌面UserAgent，防止被反爬
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 定义请求拦截/监听函数
        def handle_request(request):
            nonlocal found_m3u8
            req_url = request.url
            # 核心过滤逻辑：寻找包含 szsummer.cn 且是 m3u8 的链接
            if "szsummer.cn" in req_url and ".m3u8" in req_url:
                logger.info(f"CAPTURED: {req_url}")
                found_m3u8 = req_url

        # 开启网络监听
        page.on("request", handle_request)

        try:
            # 1. 访问页面，设置超时时间为 15秒
            logger.info("Navigating to page...")
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)

            # 2. 等待一小会儿，看是否自动捕获
            for _ in range(5):
                if found_m3u8: break
                await asyncio.sleep(1)

            # 3. 如果还没找到，尝试模拟点击播放
            if not found_m3u8:
                logger.info("Attempting to click play buttons...")
                # 尝试点击常见的播放器覆盖层或按钮
                try:
                    # 常见的播放按钮选择器，根据实际情况可能需要调整
                    potential_buttons = [
                        "video", 
                        ".vjs-big-play-button", 
                        ".dplayer-mobile-play",
                        "button[class*='play']", 
                        ".play-icon"
                    ]
                    
                    for selector in potential_buttons:
                        if await page.locator(selector).count() > 0:
                            if await page.locator(selector).is_visible():
                                await page.locator(selector).first.click(timeout=1000)
                                logger.info(f"Clicked {selector}")
                                await asyncio.sleep(2) # 点击后等待请求发出
                                if found_m3u8: break
                except Exception as e:
                    logger.warning(f"Click interaction error: {e}")

            # 4. 最终检查
            if not found_m3u8:
                # 给最后一点时间加载
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Error during processing: {e}")
            # 即使出错，如果已经抓到了链接也返回
            pass
        finally:
            await browser.close()

    if found_m3u8:
        # 直接返回纯文本URL，或者JSON，根据你的需求
        # 这里返回JSON方便查看，如果只需要纯文本，可以改 return Response(content=found_m3u8, media_type="text/plain")
        return {
            "code": 200,
            "original_url": url,
            "m3u8_url": found_m3u8
        }
    else:
        raise HTTPException(status_code=404, detail="Could not find the target m3u8 request within timeout.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
