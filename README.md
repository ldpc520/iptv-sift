# IPTV-Sift Docker

测绘空间 IP 筛选工具合集的 Docker 镜像版本，包含：
- 酒店 IP 筛选（IPTV IP:Port + 频道 + 拉流验证）
- 组播 IP 筛选（连通性 + 组播验证 + 测速）
- IPTV 频道查询（并发拉流验证 + 导出）
- 代理 IP 检测

## 目录结构

```
iptv-sift/
├── app.py                 # Flask 后端（配置文件读写走 CONFIG_DIR 环境变量）
├── config.ini             # 镜像内置默认配置（种子，仅首次初始化用）
├── requirements.txt       # Python 依赖
├── templates/index.html   # 前端页面
├── Dockerfile
├── .dockerignore
├── .github/workflows/docker.yml   # 自动构建推送 Docker Hub
└── README.md
```

## 配置文件持久化机制（重要）

- 镜像内置的 `config.ini` 只是**默认种子**，位于 `/app/config.ini`。
- 应用运行时实际读写的配置文件位于 **`/data/config.ini`**（由环境变量 `CONFIG_DIR` 指定）。
- 容器首次启动时，若 `/data/config.ini` 不存在，会自动从镜像默认配置复制过去作为初始配置。
- **`/data` 必须挂载为卷**，否则容器重启后你在网页里改的配置（密码、组播列表等）会全部丢失——因为容器临时层的改动不会保留。

> 即：所有"改动"都发生在 `/data/config.ini`，只要 `/data` 挂了卷，重启、升级镜像都不丢数据。

## 一、本地构建并运行（推荐带持久化卷）

```bash
cd iptv-sift
docker build -t iptv-sift:local .

# 推荐：用命名卷持久化配置（重启不丢）
docker run -d -p 6604:6604 -v iptv-sift-data:/data --name iptv-sift iptv-sift:local

# 浏览器访问 http://localhost:6604
```

查看当前配置内容：
```bash
docker exec -it iptv-sift cat /data/config.ini
```

直接改宿主机上的配置（用绑定挂载方式启动）：
```bash
# 先把镜像里的默认配置导出来
docker run --rm iptv-sift:local cat /app/config.ini > ./config.ini
# 用宿主机文件挂载（改本地 ./config.ini 即生效）
docker run -d -p 6604:6604 -v "$PWD/config.ini":/data/config.ini --name iptv-sift iptv-sift:local
```

自定义端口（容器内 `PORT` 环境变量）：
```bash
docker run -d -p 8080:8080 -e PORT=8080 -v iptv-sift-data:/data --name iptv-sift iptv-sift:local
```

停止 / 删除容器（注意：删除容器不会删除命名卷 `iptv-sift-data`，数据仍在）：
```bash
docker stop iptv-sift && docker rm iptv-sift
```

## 二、通过 GitHub Actions 自动推送到 Docker Hub

1. 在 GitHub 上新建仓库（如 `iptv-sift`），将上述文件推送到 `main` 分支。
2. 在仓库 **Settings → Secrets and variables → Actions** 中添加两个 Secret：
   - `DOCKERHUB_USERNAME`：你的 Docker Hub 用户名
   - `DOCKERHUB_TOKEN`：Docker Hub 的 Access Token（在 https://hub.docker.com/settings/security 创建，不要用密码）
3. 推送代码到 `main` 分支（或手动在 Actions 页面触发），工作流会自动：
   - 构建 `linux/amd64` 和 `linux/arm64` 双架构镜像
   - 推送到 `docker.io/<你的用户名>/iptv-sift:latest` 以及 `:sha-xxxx` 标签

## 三、拉取并运行官方镜像（带持久化）

```bash
docker pull <你的用户名>/iptv-sift:latest

# 命名卷方式（推荐，最简单）
docker run -d -p 6604:6604 -v iptv-sift-data:/data --name iptv-sift <你的用户名>/iptv-sift:latest
```

## 四、如何修改配置

两种方式，任选其一，且都会持久化：

**方式 A：网页配置入口（最方便）**
在页面「组播 IP 筛选 → 第二步」点击 ⚙️ 配置 按钮，输入密码（默认 `123456`）后即可在线编辑。密码本身也在 `[system]` 段里，改完保存即生效并持久化到卷。

**方式 B：直接改挂载的配置文件**
- 命名卷方式：用 `docker exec -it iptv-sift vi /data/config.ini` 或先把卷挂到临时容器拷出来改。
- 绑定挂载方式（第一节的 `$PWD/config.ini`）：直接编辑宿主机上的 `./config.ini`，重启容器生效。

修改后无需重建镜像，重启容器即可加载新配置（`/data/config.ini` 优先于镜像内置默认配置）。
