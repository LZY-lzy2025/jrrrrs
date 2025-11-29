# 使用微软官方的 Playwright Python 镜像，包含浏览器环境
# 这是一个较大的镜像，但最稳定
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright 浏览器 (Chromium)
RUN playwright install chromium

# 复制源代码
COPY main.py .

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
