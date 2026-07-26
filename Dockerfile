# ============================================================
# 测绘空间 IP 筛选工具合集 (iptv-sift) - Docker 镜像
# 基于 Python 3.13 slim，纯 Python 依赖，无需外部二进制
# ============================================================

FROM python:3.13-slim

# 镜像元数据
LABEL org.opencontainers.image.title="iptv-sift" \
      org.opencontainers.image.description="测绘空间 IP 筛选工具合集：酒店/组播 IP 筛选、IPTV 频道查询、代理检测" \
      org.opencontainers.image.source="https://github.com/ken01982/iptv-sift" \
      org.opencontainers.image.licenses="MIT"

# 时区（沿用项目习惯 Asia/Shanghai）+ 关闭 Python 输出缓冲
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 安装时区数据并设置时区
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用 Docker 层缓存（依赖不变时不会重新安装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码：后端、默认配置、前端模板
COPY app.py config.ini ./
COPY templates ./templates

# 可选：运行时数据目录（如后续版本写入结果文件，可挂载卷持久化）
VOLUME ["/app/data"]

# Flask 监听端口
EXPOSE 6604

# 启动应用（host 0.0.0.0 已在 app.py 内设置，端口 6604）
CMD ["python", "app.py"]
