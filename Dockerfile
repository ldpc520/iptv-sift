# 使用官方 Python 轻量镜像
FROM python:3.11-slim

# 设置环境变量（避免无缓冲输出、设置时区）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 安装系统依赖（部分 Python 包可能需要编译）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app.py .
COPY config.ini .
COPY templates/ ./templates/

# 创建持久化目录（config.ini 实际读写位置，需挂载卷以持久化数据）
RUN mkdir -p /data
ENV CONFIG_DIR=/data

# 暴露端口（可通过 -e PORT=xxxx 覆盖）
EXPOSE 6604

# 容器启动时运行 Flask 应用
CMD ["python", "app.py"]
