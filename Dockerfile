# 1. 使用轻量级基础镜像 (Debian Slim)
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 2. 设置环境变量
# PYTHONDONTWRITEBYTECODE: 不生成 .pyc 文件
# PYTHONUNBUFFERED: 实时输出日志
# PLAYWRIGHT_BROWSERS_PATH: 指定浏览器安装路径
# PLAYWRIGHT_DOWNLOAD_HOST: !!!关键!!! 使用国内镜像下载 Playwright 浏览器
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/

# 3. 复制依赖文件
COPY requirements.txt .

# 4. 安装依赖 (使用清华源加速)
# 这一步将 apt 换源、pip 安装、Playwright 安装合并，减少层数
RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources || true && \
    sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list || true && \
    apt-get update && \
    pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /root/.cache/pip

# 复制源代码
COPY main.py .

# 暴露端口 (仅供参考，实际由平台分配)
EXPOSE 8000

# 启动命令
# 这里的端口实际上会由 main.py 中的 os.environ.get("PORT") 覆盖
CMD ["python", "main.py"]
