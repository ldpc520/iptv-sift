# 测绘空间 IP 筛选工具合集 (iptv-sift)

基于 Flask 的 IPTV / 组播 / 酒店 IP 筛选与检测 Web 工具，支持：

- **酒店 IP 筛选**：IPTV `ip:port` 筛选 + 频道提取 + 拉流验证
- **组播 IP 筛选**：IP 连通性检测 + 组播地址验证 + 测速
- **IPTV 频道查询**：频道列表 + M3U/CSV 导出 + 播放器调用
- **代理 IP 检测**：SOCKS5/SOCKS4/HTTP/HTTPS + 地理位置

Web 服务监听 `0.0.0.0:6604`。

> 说明：代码中「唤起本机播放器（PotPlayer）测试」的按钮依赖宿主机 GUI，在容器内不生效，但不影响其余功能。

---

## 一、目录结构（需纳入镜像的文件）

```
.
├── Dockerfile
├── .dockerignore
├── .github/workflows/docker-image.yml   # GitHub Actions 自动构建推送
├── requirements.txt
├── app.py
├── config.ini                           # 默认组播配置（运行时只读）
├── templates/
│   └── index.html
└── README.md
```

---

## 二、本地用 Docker 运行（可选，用于自测）

```bash
# 构建
docker build -t ken01982/iptv-sift .

# 运行（映射 6604 端口）
docker run -d --name iptv-sift -p 6604:6604 ken01982/iptv-sift

# 访问
# http://<宿主机IP>:6604
```

### 自定义组播列表 / 持久化数据

`config.ini` 在镜像内是只读默认配置。如需自定义，把你的 `config.ini` 挂进去即可（应用检测到文件存在就不会再自动生成）：

```bash
docker run -d --name iptv-sift \
  -p 6604:6604 \
  -v /your/path/config.ini:/app/config.ini:ro \
  -v iptv-sift-data:/app/data \
  ken01982/iptv-sift
```

---

## 三、用 GitHub Actions 自动构建并推送到 Docker Hub

本仓库已内置 `.github/workflows/docker-image.yml`。只要把代码推到 GitHub，GitHub 云端就会自动构建镜像并推送到 `hub.docker.com` 的 `ken01982/iptv-sift`，**不需要你本机装 Docker**。

### 步骤 1：在 GitHub 创建仓库

到 https://github.com/new 新建一个仓库，名称建议 `iptv-sift`（公开或私有均可）。

### 步骤 2：配置 Docker Hub 密钥

在仓库页面：`Settings → Secrets and variables → Actions → New repository secret`，添加两个密钥：

| 名称 | 值 |
|------|----|
| `DOCKERHUB_USERNAME` | `ken01982` |
| `DOCKERHUB_TOKEN` | Docker Hub **访问令牌**（不是登录密码） |

> 令牌生成位置：登录 https://hub.docker.com → `Account Settings → Security → Access Tokens → Generate New Token`（权限选 `Read/Write`）。

### 步骤 3：推送代码触发构建

在你本机（已 `git init` 并提交好的目录下）执行：

```powershell
# 若尚未初始化（本仓库已帮你 init 并提交）
git init
git add .
git commit -m "init: iptv-sift docker 构建"

# 关联远程仓库并推送（把 <你的GitHub用户名> 替换成实际用户名）
git remote add origin https://github.com/<你的GitHub用户名>/iptv-sift.git
git branch -M main
git push -u origin main
```

推送后到仓库 `Actions` 标签页即可看到构建进度；成功后镜像出现在：
https://hub.docker.com/r/ken01982/iptv-sift

### 镜像标签规则

| 触发场景 | 生成的标签 |
|----------|-----------|
| 推送到 `main` | `latest` |
| 打标签 `v1.2.3` | `1.2.3` |
| 每次构建 | `sha-<短commit>` |

### 构建架构

默认仅构建 `linux/amd64`（适配你的 x86 服务器）。如需 ARM（树莓派 / ARM 软路由），编辑 `.github/workflows/docker-image.yml` 中的 `platforms` 为 `linux/amd64,linux/arm64` 即可。

---

## 四、从 Docker Hub 拉取并运行

```bash
docker pull ken01982/iptv-sift:latest
docker run -d --name iptv-sift -p 6604:6604 ken01982/iptv-sift
```

---

## 依赖

`flask`、`aiohttp`、`requests`、`PySocks`（见 `requirements.txt`）。
