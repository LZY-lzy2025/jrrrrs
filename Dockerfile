# 1. 使用轻量级的 Python Slim 镜像 (Debian based)
# 体积比 Ubuntu 基础镜像小很多
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量，防止 Python 生成 pyc 文件和缓冲输出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 复制依赖文件
COPY requirements.txt .

# 2. 合并运行命令以减少镜像层数 (Layer)
# - 更新 apt 源
# - 安装 Python 依赖
# - 使用 Playwright 安装 Chromium 及其系统依赖 (--with-deps)
# - 清理 apt 缓存和 pip 缓存以减小体积
RUN apt-get update && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /root/.cache/pip

# 复制源代码
COPY main.py .

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
