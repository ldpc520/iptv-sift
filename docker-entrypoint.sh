#!/bin/sh
set -e

CONFIG="/app/config.ini"
DEFAULT="/app/config.ini.default"

# Docker 在宿主机绑定文件缺失时，会把挂载点创建为「目录」。
# 这里先清除该目录，才能把默认配置生成为真正的「文件」
# （这样生成的 config.ini 会出现在宿主机、与 docker-compose.yml 同一目录）。
if [ -d "$CONFIG" ]; then
  rm -rf "$CONFIG"
fi

# 首次运行：从镜像内置默认配置生成 config.ini
if [ ! -f "$CONFIG" ]; then
  cp "$DEFAULT" "$CONFIG"
  echo "[entrypoint] 已从镜像默认配置生成 $CONFIG"
fi

# 启动应用（由 CMD 传入的参数决定）
exec "$@"
