# 1. 使用轻量级基础镜像 (Debian Slim)，体积更小
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 2. 关键环境变量设置
# PYTHONDONTWRITEBYTECODE: 防止生成 .pyc 文件
# PYTHONUNBUFFERED: 保证日志实时输出，方便调试
# PLAYWRIGHT_BROWSERS_PATH: 指定浏览器安装路径
# PLAYWRIGHT_DOWNLOAD_HOST: !!!关键!!! 使用淘宝镜像源下载浏览器，解决国内下载卡死问题
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/

# 3. 复制依赖文件
COPY requirements.txt .

# 4. 组合命令安装依赖 (国内源加速)
# - 修改 Debian 系统源为中科大源 (USTC)
# - 更新 apt 缓存
# - 使用清华源安装 Python 依赖
# - 使用 Playwright 安装 Chromium (会自动走上面的淘宝镜像)
# - 清理缓存减小体积
RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /root/.cache/pip

# 复制源代码
COPY main.py .

# 暴露端口 (仅作声明)
EXPOSE 8000

# 启动命令
# 使用 python 直接启动，让代码中的逻辑去处理端口
CMD ["python", "main.py"]
