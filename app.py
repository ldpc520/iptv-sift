#!/usr/bin/env python3
"""
测绘空间 IP 筛选工具合集 - Flask 后端
功能一：酒店 IP 筛选（IPTV IP:Port 筛选 + 频道提取 + 拉流验证）
功能二：组播 IP 筛选（IP 连通性检测 + 组播地址验证 + 测速）
功能三：IPTV 频道查询（频道列表 + M3U/CSV导出 + 播放器调用）
"""

import re
import json
import os
import sys
import socket
import ssl
import time
import subprocess
import asyncio
import concurrent.futures
import threading
import shutil
import urllib.request
import urllib.error
import configparser

from flask import Flask, render_template, request, jsonify, Response
import requests as http_requests

# 尝试导入 PySocks
try:
    import socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

# 尝试导入 aiohttp
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 配置文件存放目录：默认指向镜像内置位置；Docker 部署时通过环境变量指向挂载卷（如 /data）
CONFIG_DIR = os.environ.get('CONFIG_DIR', BASE_DIR)
# 持久化配置文件路径（运行时读写都走这里，便于挂载卷实现数据持久化）
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.ini')
# 镜像内置的默认配置（仅作首次种子，容器重启不会覆盖已有配置）
DEFAULT_CONFIG_FILE = os.path.join(BASE_DIR, 'config.ini')

# ==================== 全局进度 ====================

progress_lock = threading.Lock()
scan_progress = {"total": 0, "checked": 0, "valid": 0, "status": "idle", "phase": ""}

# ==================== 配置文件（组播用） ====================

# 首次运行：若持久化目录中不存在 config.ini，则从镜像内置默认配置复制（便于挂载卷持久化）
if not os.path.exists(CONFIG_FILE):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(DEFAULT_CONFIG_FILE) and os.path.abspath(DEFAULT_CONFIG_FILE) != os.path.abspath(CONFIG_FILE):
        # 用户已挂载卷（如 /data）但卷内为空：用镜像内置默认配置作种子
        shutil.copyfile(DEFAULT_CONFIG_FILE, CONFIG_FILE)
        print("已从镜像默认配置初始化: " + CONFIG_FILE)
    else:
        # 完全首次运行（未挂载卷），生成内置默认配置
        default_content = """[multicast]
# 格式: 名称|组播地址:端口
# 一行一个
广东电信|239.77.0.1:5146
安徽电信|238.1.78.166:7200
江苏电信|239.49.8.129:6000
成都电信|239.94.0.1:5140
四川电信|239.93.1.9:2192
四川成都电信|239.94.2.52:5140
"""
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write(default_content)
        print("已自动生成 config.ini 配置文件")


def load_multicast_config():
    """读取 config.ini 中的组播配置（仅读取 [multicast] 段）"""
    items = []
    if not os.path.exists(CONFIG_FILE):
        return items
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('['):
                continue
            parts = line.split('|')
            if len(parts) == 2:
                items.append({
                    'name': parts[0].strip(),
                    'addr': parts[1].strip()
                })
    return items


def get_config_password():
    """从 config.ini 的 [system] 段读取编辑密码，默认 123456"""
    default_pwd = '123456'
    if not os.path.exists(CONFIG_FILE):
        return default_pwd
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE, encoding='utf-8')
        return cfg.get('system', 'password', fallback=default_pwd)
    except Exception:
        return default_pwd


def read_full_config():
    """读取 config.ini 全部文本内容"""
    if not os.path.exists(CONFIG_FILE):
        return ''
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return f.read()


def save_full_config(content):
    """保存 config.ini 全部内容（原子写入）"""
    tmp_file = CONFIG_FILE + '.tmp'
    with open(tmp_file, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp_file, CONFIG_FILE)
    return True


# ==================== 工具函数 ====================

def extract_ips(text):
    """从文本中提取所有 ip:port 格式（去重）"""
    pattern = re.compile(
        r'(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\s*[:：]\s*(?P<port>\d{1,5})'
    )
    seen = set()
    results = []
    for m in pattern.finditer(text):
        ip = m.group('ip')
        port = int(m.group('port'))
        if 1 <= port <= 65535:
            key = f"{ip}:{port}"
            if key not in seen:
                seen.add(key)
                results.append((ip, port))
    return results


def parse_ip_ports(text):
    """从输入文本中解析所有 ip:port，去重后返回列表（组播用）"""
    text = text.replace('，', ',').replace('；', ',').replace(';', ',').replace('\r', '')
    lines = text.split('\n')
    pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)')
    seen = set()
    result = []
    for line in lines:
        matches = pattern.findall(line)
        for m in matches:
            if m not in seen:
                seen.add(m)
                result.append(m)
    return result


def is_valid_ip_port(ip_port):
    """校验 ip:port 格式是否合法"""
    parts = ip_port.rsplit(':', 1)
    if len(parts) != 2:
        return False
    ip, port = parts
    ip_segments = ip.split('.')
    if len(ip_segments) != 4:
        return False
    for seg in ip_segments:
        if not seg.isdigit():
            return False
        num = int(seg)
        if num < 0 or num > 255:
            return False
    if not port.isdigit():
        return False
    p = int(port)
    if p < 1 or p > 65535:
        return False
    return True


def http_get(url, timeout=8, encoding=None):
    """发起 HTTP GET，返回 (status_code, text)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        raw = resp.read()
        if encoding is None:
            charset = resp.headers.get_content_charset()
            if charset:
                try:
                    return resp.status, raw.decode(charset)
                except Exception:
                    pass
            for enc in ['utf-8', 'gb2312', 'gbk', 'gb18030', 'latin-1']:
                try:
                    return resp.status, raw.decode(enc)
                except Exception:
                    continue
            return resp.status, raw.decode('utf-8', errors='replace')
        else:
            return resp.status, raw.decode(encoding, errors='replace')
    except Exception as e:
        return None, str(e)


# ==================== 酒店 IP 筛选模块 ====================

def check_socket(ip, port, timeout=3):
    """socket 连接测试"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def verify_ips_concurrent(ip_port_list, max_workers=80, timeout=3):
    """并发检测 IP:Port 可达性"""
    global scan_progress
    with progress_lock:
        scan_progress["total"] = len(ip_port_list)
        scan_progress["checked"] = 0
        scan_progress["valid"] = 0
        scan_progress["status"] = "scanning"
        scan_progress["phase"] = "正在验证连通性..."

    valid = []
    lock = threading.Lock()

    def _check_one(item):
        ip, port = item
        ok = check_socket(ip, port, timeout)
        with lock:
            with progress_lock:
                scan_progress["checked"] += 1
            if ok:
                with progress_lock:
                    scan_progress["valid"] += 1
                valid.append(item)
        return ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(_check_one, ip_port_list)

    with progress_lock:
        scan_progress["status"] = "idle"

    return valid


def parse_json_source(ip, port, text):
    """解析 /iptv/live/1000.json?key=txiptv 返回的 JSON 数据"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    channels = []
    base_url = f"http://{ip}:{port}"

    def _ensure_full_url(raw_url):
        raw_url = str(raw_url).strip()
        if not raw_url:
            return raw_url
        if raw_url.startswith('http://') or raw_url.startswith('https://'):
            raw_url = re.sub(
                r'http://[\d.]+(:\d+)?(?=/)',
                f'http://{ip}:{port}',
                raw_url
            )
            return raw_url
        if raw_url.startswith('/'):
            return base_url + raw_url
        else:
            return base_url + '/' + raw_url

    def _extract(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, dict):
            name = obj.get('name') or obj.get('title') or obj.get('channelName') or ''
            m3u8 = obj.get('url') or obj.get('m3u8') or obj.get('playUrl') or obj.get('source') or ''
            if m3u8 and '.m3u8' in str(m3u8):
                full_url = _ensure_full_url(m3u8)
                channels.append((str(name), full_url))
            for v in obj.values():
                _extract(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item, depth + 1)

    _extract(data)
    return channels


def parse_txt_source(ip, port, text):
    """解析 /ZHGXTV/Public/json/live_interface.txt 返回的文本数据"""
    channels = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = None
        for sep in [',', '，', '\t']:
            if sep in line:
                parts = line.split(sep, 1)
                break
        if parts and len(parts) == 2:
            name = parts[0].strip()
            url = parts[1].strip()
            url = re.sub(
                r'http://[\d.]+(:\d+)?(?=/)',
                f'http://{ip}:{port}',
                url
            )
            if '.m3u8' in url.lower():
                channels.append((name, url))
    return channels


def test_url_source(ip, port, source_type='auto'):
    """对单个有效 IP:Port 测试数据源（自动匹配：先 JSON 后 TXT）"""
    result = {"ip": ip, "port": port, "channels": [], "error": None, "source_type": source_type}

    # ---------- 接口 1：JSON ----------
    url = f"http://{ip}:{port}/iptv/live/1000.json?key=txiptv"
    code, resp = http_get(url, timeout=10)
    if code == 200 and resp is not None and len(resp) >= 100:
        channels = parse_json_source(ip, port, resp)
        if channels:
            result["channels"] = channels
            result["source_type"] = "json"
            return result

    # ---------- 接口 2：TXT（自动兜底）----------
    url = f"http://{ip}:{port}/ZHGXTV/Public/json/live_interface.txt"
    code, resp = http_get(url, timeout=10)
    if code == 200 and resp is not None and len(resp) >= 100:
        channels = parse_txt_source(ip, port, resp)
        if channels:
            result["channels"] = channels
            result["source_type"] = "txt"
            return result

    # 两个都失败
    result["error"] = "两个数据源接口均无有效频道数据"
    return result


# ---------- 流验证（aiohttp 异步拉流检测）----------

async def _verify_single_stream(session, name, url, timeout=8):
    """拉取单个 m3u8 流的前几KB数据，验证是否真实可用
    返回: (name, url, is_valid, speed_kbps, speed_ms)"""
    try:
        start_time = time.time()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                               ssl=False) as resp:
            if resp.status != 200:
                return (name, url, False, 0, 0)

            content_type = resp.headers.get('Content-Type', '').lower()

            data = b''
            async for chunk in resp.content.iter_chunked(8192):
                data += chunk
                elapsed = time.time() - start_time
                if elapsed > 2.5 or len(data) > 65536:
                    break
            elapsed = time.time() - start_time
            if elapsed < 0.01:
                elapsed = 0.01
            speed_kbps = len(data) / elapsed / 1024
            speed_ms = round(elapsed * 1000, 1)

            text = ''
            try:
                text = data.decode('utf-8', errors='replace')
            except Exception:
                pass

            # 检查是否为 m3u8 内容
            has_m3u8_marker = (
                '#EXTM3U' in text or
                '#EXTINF' in text or
                '#EXT-X-' in text
            )

            # 检查是否为 TS 视频流
            # TS 包以 0x47 同步字节开头，且内容较大
            is_ts_stream = (
                data[:1] == b'\x47' and len(data) > 188
            )

            # 检查 content-type 是否为视频相关
            is_video_content_type = any(
                ct in content_type for ct in
                ['video/', 'application/vnd.apple.mpegurl',
                 'application/x-mpegurl', 'audio/']
            )

            is_valid = (
                has_m3u8_marker or
                is_ts_stream or
                (is_video_content_type and len(data) > 1024)
            )

            return (name, url, is_valid, round(speed_kbps, 1), speed_ms)

    except asyncio.TimeoutError:
        return (name, url, False, 0, 0)
    except Exception:
        return (name, url, False, 0, 0)


async def _verify_streams_batch(channels, max_concurrent=20, timeout=8):
    """异步并发验证一批频道流"""
    if not HAS_AIOHTTP:
        return [(name, url, True, 0) for name, url in channels]

    connector = aiohttp.TCPConnector(limit=max_concurrent, limit_per_host=5)
    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout_obj,
        headers=headers
    ) as session:
        tasks = [_verify_single_stream(session, name, url, timeout) for name, url in channels]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = []
    for r in results:
        if isinstance(r, Exception):
            processed.append(("", "", False, 0, 0))
        else:
            processed.append(r)
    return processed


def verify_streams_sync(channels, max_concurrent=20, timeout=8):
    """同步包装器，在线程中运行 asyncio 流验证"""
    if not channels:
        return []

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(
            _verify_streams_batch(channels, max_concurrent, timeout)
        )
        return results
    finally:
        loop.close()


def run_full_scan(valid_ips, source_type, verify_stream=False):
    """对所有有效 IP:Port 测试数据源，可选流验证"""
    global scan_progress
    with progress_lock:
        scan_progress["status"] = "testing"
        scan_progress["phase"] = "正在获取频道列表..."
        scan_progress["total"] = len(valid_ips)
        scan_progress["checked"] = 0
        scan_progress["valid"] = 0

    results = []
    lock = threading.Lock()

    def _test_one(item):
        ip, port = item
        res = test_url_source(ip, port, source_type)
        with lock:
            with progress_lock:
                scan_progress["checked"] += 1
            if res["channels"]:
                with progress_lock:
                    scan_progress["valid"] += 1
                results.append(res)
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        executor.map(_test_one, valid_ips)

    # 流验证
    if verify_stream and HAS_AIOHTTP and results:
        with progress_lock:
            scan_progress["status"] = "verifying"
            scan_progress["phase"] = "正在验证直播流..."

        all_channels_flat = []
        for r in results:
            for name, url in r["channels"]:
                all_channels_flat.append((name, url))

        with progress_lock:
            scan_progress["total"] = len(all_channels_flat)
            scan_progress["checked"] = 0
            scan_progress["valid"] = 0

        batch_size = 200
        verified_map = {}

        for batch_idx in range(0, len(all_channels_flat), batch_size):
            batch = all_channels_flat[batch_idx:batch_idx + batch_size]
            batch_results = verify_streams_sync(batch, max_concurrent=20, timeout=8)

            for name, url, is_valid, speed, speed_ms in batch_results:
                verified_map[url] = (is_valid, speed, speed_ms)
                with progress_lock:
                    scan_progress["checked"] += 1
                    if is_valid:
                        scan_progress["valid"] += 1

        filtered_results = []
        for r in results:
            valid_channels = []
            for name, url in r["channels"]:
                v = verified_map.get(url)
                if v and v[0]:
                    speed = v[1]
                    speed_ms = v[2]
                    valid_channels.append({
                        "name": name,
                        "url": url,
                        "speed": speed,
                        "speed_ms": speed_ms
                    })
            if valid_channels:
                valid_channels.sort(key=lambda x: x["speed_ms"])
                r["channels"] = valid_channels
                r["channel_count"] = len(valid_channels)
                r["avg_speed"] = round(sum(c["speed"] for c in valid_channels) / len(valid_channels), 1)
                r["max_speed"] = max(c["speed"] for c in valid_channels)
                r["avg_speed_ms"] = round(sum(c["speed_ms"] for c in valid_channels) / len(valid_channels), 1)
                r["min_speed_ms"] = min(c["speed_ms"] for c in valid_channels)
                filtered_results.append(r)

        filtered_results.sort(key=lambda x: x["avg_speed_ms"])
        results = filtered_results

    with progress_lock:
        scan_progress["status"] = "idle"

    return results


# ==================== 组播 IP 筛选模块 ====================

async def check_connectivity_single(session, ip_port, timeout=3):
    """初次连通性检测：仅检测 ip:port 是否可达"""
    url = f"http://{ip_port}/"
    result = {
        'ip_port': ip_port,
        'reachable': False,
        'latency_ms': 0,
        'error': ''
    }
    try:
        t_start = time.perf_counter()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            await resp.content.read(128)
            elapsed = (time.perf_counter() - t_start) * 1000
            result['reachable'] = True
            result['latency_ms'] = round(elapsed, 1)
    except asyncio.TimeoutError:
        result['error'] = '超时'
    except aiohttp.ClientConnectorError:
        result['error'] = '连接失败'
    except Exception as e:
        result['error'] = str(e)[:80]
    return result


async def batch_connectivity_check(ip_ports, timeout=3, concurrency=30):
    """批量并发连通性检测"""
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_connectivity_single(session, ip, timeout) for ip in ip_ports]
        results = await asyncio.gather(*tasks)
    return list(results)


def run_connectivity_check(ip_ports, timeout=3, concurrency=30):
    """在同步环境中运行连通性检测"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, batch_connectivity_check(ip_ports, timeout, concurrency))
            return future.result()
    else:
        return asyncio.run(batch_connectivity_check(ip_ports, timeout, concurrency))


async def check_single(session, ip_port, multicast_addr, protocol, timeout=5):
    """验证单个 ip:port 对指定组播地址的可用性，同时测速"""
    url = f"http://{ip_port}/{protocol}/{multicast_addr}"
    result = {
        'ip_port': ip_port,
        'status': 'fail',
        'speed_ms': 0,
        'url': url,
        'error': ''
    }
    try:
        t_start = time.perf_counter()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            chunk = await resp.content.read(8192)
            elapsed = (time.perf_counter() - t_start) * 1000
            if resp.status == 200 and len(chunk) > 0:
                result['status'] = 'ok'
                result['speed_ms'] = round(elapsed, 1)
            else:
                result['error'] = f'HTTP {resp.status}'
    except asyncio.TimeoutError:
        result['error'] = '超时'
    except aiohttp.ClientConnectorError:
        result['error'] = '连接失败'
    except Exception as e:
        result['error'] = str(e)[:80]
    return result


async def batch_check(ip_ports, multicast_addr, protocol, timeout=5, concurrency=20):
    """批量并发验证"""
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_single(session, ip, multicast_addr, protocol, timeout) for ip in ip_ports]
        results = await asyncio.gather(*tasks)
    return list(results)


def run_async_check(ip_ports, multicast_addr, protocol, timeout=5, concurrency=20):
    """在同步环境中运行异步批量验证"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, batch_check(ip_ports, multicast_addr, protocol, timeout, concurrency))
            return future.result()
    else:
        return asyncio.run(batch_check(ip_ports, multicast_addr, protocol, timeout, concurrency))


# ==================== 播放器自动查找（跨平台） ====================

def find_player():
    """跨平台自动查找系统中已安装的播放器"""
    if sys.platform == 'win32':
        return _find_player_windows()
    else:
        return _find_player_linux()


_WIN_PLAYER_REGISTRY = {
    'PotPlayer': (
        [
            os.path.expandvars('%ProgramFiles%\\DAUM\\PotPlayer'),
            os.path.expandvars('%ProgramFiles(x86)%\\DAUM\\PotPlayer'),
            os.path.expandvars('%LOCALAPPDATA%\\PotPlayer'),
            'C:\\PotPlayer', 'D:\\PotPlayer', 'E:\\PotPlayer',
        ],
        ['PotPlayerMini64.exe', 'PotPlayerMini.exe', 'PotPlayer.exe']
    ),
    'VLC': (
        [
            os.path.expandvars('%ProgramFiles%\\VideoLAN\\VLC'),
            os.path.expandvars('%ProgramFiles(x86)%\\VideoLAN\\VLC'),
        ],
        ['vlc.exe']
    ),
    'MPC-HC': (
        [
            os.path.expandvars('%ProgramFiles%\\MPC-HC'),
            os.path.expandvars('%ProgramFiles(x86)%\\MPC-HC'),
        ],
        ['mpc-hc64.exe', 'mpc-hc.exe']
    ),
    'MPC-BE': (
        [
            os.path.expandvars('%ProgramFiles%\\MPC-BE'),
            os.path.expandvars('%ProgramFiles(x86)%\\MPC-BE'),
        ],
        ['mpc-be64.exe', 'mpc-be.exe']
    ),
    'mpv': (
        [
            os.path.expandvars('%ProgramFiles%\\mpv'),
            os.path.expandvars('%ProgramFiles(x86)%\\mpv'),
            os.path.expandvars('%LOCALAPPDATA%\\mpv'),
        ],
        ['mpv.exe']
    ),
    'KMPlayer': (
        [
            os.path.expandvars('%ProgramFiles%\\KMPlayer'),
            os.path.expandvars('%ProgramFiles(x86)%\\KMPlayer'),
            os.path.expandvars('%ProgramFiles%\\The KMPlayer'),
            os.path.expandvars('%ProgramFiles(x86)%\\The KMPlayer'),
        ],
        ['KMPlayer.exe', 'KMPlayer64.exe']
    ),
    'SMPlayer': (
        [
            os.path.expandvars('%ProgramFiles%\\SMPlayer'),
            os.path.expandvars('%ProgramFiles(x86)%\\SMPlayer'),
        ],
        ['smplayer.exe']
    ),
}


def _find_exe_in_dirs(dirs, exe_names):
    for d in dirs:
        for name in exe_names:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def _global_search_exe(exe_names, search_roots=('C:\\', 'D:\\'), timeout=8):
    import glob as _glob

    priority_roots = [
        os.path.expandvars('%ProgramFiles%'),
        os.path.expandvars('%ProgramFiles(x86)%'),
        os.path.expandvars('%LOCALAPPDATA%'),
        os.path.expandvars('%APPDATA%'),
    ]
    seen = set()
    roots = []
    for r in priority_roots + list(search_roots):
        r = os.path.normpath(r)
        if r not in seen and os.path.isdir(r):
            seen.add(r)
            roots.append(r)

    start_time = time.time()
    for root in roots:
        if time.time() - start_time > timeout:
            break
        try:
            for name in exe_names:
                pattern = os.path.join(root, '**', name)
                iterator = _glob.iglob(pattern, recursive=True)
                for found in iterator:
                    if os.path.isfile(found):
                        normalized = found.lower()
                        skip_keywords = ['\\$recycle.bin', '\\temp\\', '\\windows.old\\',
                                         '\\installer\\', '\\downloaded installations\\']
                        if any(k in normalized for k in skip_keywords):
                            continue
                        return found
                    if time.time() - start_time > timeout:
                        break
        except (PermissionError, OSError, Exception):
            continue
    return None


def _find_player_windows():
    for player_name, (dirs, exe_names) in _WIN_PLAYER_REGISTRY.items():
        exe_path = _find_exe_in_dirs(dirs, exe_names)
        if exe_path:
            return exe_path, player_name

    common_exes = ['PotPlayerMini64.exe', 'PotPlayerMini.exe', 'vlc.exe',
                   'mpc-hc64.exe', 'mpc-hc.exe', 'mpc-be64.exe', 'mpc-be.exe',
                   'mpv.exe', 'KMPlayer.exe', 'smplayer.exe']
    exe_path = _global_search_exe(common_exes, timeout=8)
    if exe_path:
        basename = os.path.basename(exe_path).lower()
        name_map = {
            'potplayer': 'PotPlayer', 'vlc': 'VLC',
            'mpc-hc': 'MPC-HC', 'mpc-be': 'MPC-BE',
            'mpv': 'mpv', 'kmplayer': 'KMPlayer', 'smplayer': 'SMPlayer',
        }
        for key, pname in name_map.items():
            if key in basename:
                return exe_path, pname
        return exe_path, '未知播放器'
    return None, None


_LINUX_PLAYERS = [
    ('vlc', 'VLC'),
    ('mpv', 'mpv'),
    ('ffplay', 'FFplay'),
    ('mplayer', 'MPlayer'),
    ('smplayer', 'SMPlayer'),
    ('cvlc', 'VLC (cvlc)'),
]


def _which(cmd):
    paths = os.environ.get('PATH', '/usr/bin:/usr/local/bin').split(os.pathsep)
    for p in paths:
        full = os.path.join(p, cmd)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def _find_player_linux():
    for cmd, name in _LINUX_PLAYERS:
        path = _which(cmd)
        if path:
            return path, name
    return None, None


# ==================== 代理IP检测模块 ====================

# ISP 英文名 → 中文名映射
ISP_CN_MAP = {
    'China Telecom': '电信',
    'China Unicom': '联通',
    'China Mobile': '移动',
    'China Tietong': '铁通',
    'China Education and Research Network': '教育网',
    'Tencent cloud computing': '腾讯云',
    'Tencent Computer Systems Company Limited': '腾讯',
    'Shenzhen Tencent Computer Systems Company Limited': '腾讯',
    'Alibaba': '阿里云',
    'Alibaba Cloud': '阿里云',
    'China Internet Network Information Center': 'CNNIC',
    'Hangzhou Alibaba Advertising Co.,Ltd.': '阿里云',
    'Aliyun': '阿里云',
    'Baidu': '百度',
    'Huawei Cloud': '华为云',
    'Beijing Baidu Netcom Science and Technology Co., Ltd.': '百度',
    'Dr. Peng Telecom & Media Group': '鹏博士',
    'CHINANET-BACKBONE': '中国电信骨干网',
    'CHINANET': '电信',
    'UNICOM': '联通',
    'CMNET': '移动',
    'Amazon': '亚马逊云',
    'Amazon.com': '亚马逊云',
    'Microsoft Corporation': '微软云',
    'Microsoft Azure': '微软云',
    'Google Cloud': '谷歌云',
    'Google LLC': '谷歌',
    'Cloudflare': 'Cloudflare',
    'DigitalOcean': 'DigitalOcean',
    'Vultr': 'Vultr',
    'Linode': 'Linode',
    'OVH': 'OVH',
    'Hetzner': 'Hetzner',
}


def _format_isp(isp: str) -> str:
    """将 ISP 英文名转为中文显示"""
    if not isp:
        return ''
    if isp in ISP_CN_MAP:
        return ISP_CN_MAP[isp]
    for en, cn in ISP_CN_MAP.items():
        if en.lower() in isp.lower():
            return cn
    return isp


def _format_location(data: dict) -> str:
    """格式化 ip-api 返回的地理位置信息，ISP 转中文"""
    country = data.get('country', '')
    region = data.get('regionName', '')
    city = data.get('city', '')
    isp = _format_isp(data.get('isp', ''))
    parts = [country, region, city, isp]
    return ' '.join(p for p in parts if p).strip() or '未知'


def _proxy_tcp_check(host: str, port: int, timeout: float = 5.0):
    """TCP 端口连通性检测"""
    start = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        elapsed = (time.time() - start) * 1000
        return True, elapsed
    except Exception:
        elapsed = (time.time() - start) * 1000
        return False, elapsed


def _check_http_proxy(host: str, port: int, timeout: float = 12.0):
    """HTTP/HTTPS 代理检测"""
    start = time.time()
    proxies = {'http': f'http://{host}:{port}', 'https': f'http://{host}:{port}'}
    try:
        resp = http_requests.get(
            'http://ip-api.com/json/?lang=zh-CN',
            proxies=proxies,
            timeout=timeout,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        elapsed = (time.time() - start) * 1000
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == 'success':
                exit_ip = data.get('query', '未知')
                location = _format_location(data)
                return True, elapsed, exit_ip, location, ''
            return False, elapsed, '', '', 'IP 查询失败'
        return False, elapsed, '', '', f'HTTP {resp.status_code}'
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        err = str(e)[:60]
        if 'ConnectTimeout' in type(e).__name__ or 'timed out' in err.lower():
            err = '连接超时'
        elif 'ReadTimeout' in type(e).__name__:
            err = '读取超时'
        elif 'ProxyError' in type(e).__name__:
            err = '代理错误'
        elif 'ConnectionError' in type(e).__name__:
            err = '连接失败'
        return False, elapsed, '', '', err


def _check_socks_proxy(host: str, port: int, socks_ver, timeout: float = 12.0):
    """SOCKS5/SOCKS4 代理检测"""
    if not HAS_SOCKS:
        return False, 0, '', '', 'PySocks 未安装'
    start = time.time()
    try:
        s = socks.socksocket()
        s.settimeout(timeout)
        s.set_proxy(socks_ver, host, port)

        target_host = 'ip-api.com'
        target_port = 80
        ip = socket.gethostbyname(target_host)
        s.connect((ip, target_port))

        request_str = (
            f'GET /json/?lang=zh-CN HTTP/1.1\r\n'
            f'Host: {target_host}\r\n'
            f'User-Agent: Mozilla/5.0\r\n'
            f'Accept: */*\r\n'
            f'Connection: close\r\n\r\n'
        )
        s.sendall(request_str.encode())

        response = b''
        while True:
            try:
                data = s.recv(4096)
                if not data:
                    break
                response += data
            except socket.timeout:
                break

        s.close()
        elapsed = (time.time() - start) * 1000

        body = response.split(b'\r\n\r\n', 1)
        if len(body) > 1:
            try:
                data = json.loads(body[1])
                if data.get('status') == 'success':
                    exit_ip = data.get('query', '未知')
                    location = _format_location(data)
                    return True, elapsed, exit_ip, location, ''
                return True, elapsed, '未知', '未知', 'IP 查询失败'
            except Exception:
                return True, elapsed, '未知', '未知', '解析失败'
        return True, elapsed, '未知', '未知', '响应为空'
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        err = str(e)[:60]
        return False, elapsed, '', '', err


def _detect_proxy_type(port: int, user_type: str) -> str:
    """推测代理类型"""
    if user_type and user_type != 'all':
        return user_type
    if port in (1080, 10808):
        return 'socks5'
    if port in (80, 8080, 3128, 8888):
        return 'http'
    if port in (443, 8443):
        return 'https'
    return 'http'


def _check_single_proxy(proxy_info: dict) -> dict:
    """检测单个代理"""
    host = proxy_info['host']
    port = proxy_info['port']
    ptype = proxy_info['type']

    result = {
        'host': host, 'port': port, 'type': ptype,
        'raw': f'{host}:{port}',
        'valid': False, 'latency': 0,
        'exit_ip': '', 'location': '', 'error': '',
    }

    tcp_ok, tcp_latency = _proxy_tcp_check(host, port, timeout=5.0)
    if not tcp_ok:
        result['latency'] = round(tcp_latency)
        result['error'] = 'TCP 端口不可达'
        return result

    if ptype == 'socks5':
        ok, lat, exit_ip, location, err = _check_socks_proxy(host, port, socks.SOCKS5, timeout=15.0)
    elif ptype == 'socks4':
        ok, lat, exit_ip, location, err = _check_socks_proxy(host, port, socks.SOCKS4, timeout=15.0)
    else:
        ok, lat, exit_ip, location, err = _check_http_proxy(host, port, timeout=15.0)

    result['valid'] = ok
    result['latency'] = round(lat)
    result['exit_ip'] = exit_ip
    result['location'] = location
    result['error'] = err
    return result


def _parse_proxies(text: str, type_filter: str = 'all') -> list:
    """解析代理列表文本"""
    lines = text.strip().split('\n')
    proxies = []
    seen = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.rsplit(':', 1)
        if len(parts) != 2:
            continue
        host, port_str = parts
        host = host.strip()
        port_str = port_str.strip()
        try:
            port = int(port_str)
        except ValueError:
            continue
        if port < 1 or port > 65535:
            continue
        key = f'{host}:{port}'
        if key in seen:
            continue
        seen.add(key)
        ptype = _detect_proxy_type(port, type_filter)
        if type_filter and type_filter != 'all' and ptype != type_filter:
            continue
        proxies.append({'host': host, 'port': port, 'type': ptype})
    return proxies


def _query_ip_location(ip: str) -> str:
    """对指定 IP 查询地理位置"""
    try:
        resp = http_requests.get(
            f'http://ip-api.com/json/{ip}?lang=zh-CN',
            timeout=5,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == 'success':
                return _format_location(data)
    except Exception:
        pass
    return '未知'


# ==================== 路由 ====================

@app.route('/')
def index():
    return render_template('index.html', has_aiohttp=HAS_AIOHTTP)


# ===== 酒店 IP 筛选 API =====

@app.route('/api/hotel/extract', methods=['POST'])
def api_hotel_extract():
    """提取 IP"""
    data = request.get_json()
    text = data.get('text', '')
    ip_list = extract_ips(text)
    if not ip_list:
        return jsonify({"error": "未检测到有效的 IP:Port 格式", "ips": [], "count": 0})
    return jsonify({
        "ips": [f"{ip}:{port}" for ip, port in ip_list],
        "count": len(ip_list)
    })


@app.route('/api/hotel/verify', methods=['POST'])
def api_hotel_verify():
    """并发验证 IP"""
    data = request.get_json()
    text = data.get('text', '')
    ip_list = extract_ips(text)
    if not ip_list:
        return jsonify({"error": "未检测到 IP:Port", "valid": [], "count": 0})

    valid = verify_ips_concurrent(ip_list, max_workers=80, timeout=3)
    return jsonify({
        "valid": [f"{ip}:{port}" for ip, port in valid],
        "count": len(valid),
        "total": len(ip_list)
    })


@app.route('/api/hotel/scan', methods=['POST'])
def api_hotel_scan():
    """完整扫描：验证+数据源测试+流验证"""
    data = request.get_json()
    text = data.get('text', '')
    source_type = data.get('source_type', 'json')
    verify_stream = data.get('verify_stream', False)

    ip_list = extract_ips(text)
    if not ip_list:
        return jsonify({"error": "未检测到 IP:Port", "results": [], "count": 0})

    valid_ips = verify_ips_concurrent(ip_list, max_workers=80, timeout=3)
    if not valid_ips:
        return jsonify({
            "error": "没有可连通的 IP:Port",
            "results": [],
            "valid_ips": [],
            "count": 0
        })

    results = run_full_scan(valid_ips, source_type, verify_stream=verify_stream)

    all_channels = []
    for r in results:
        for ch in r["channels"]:
            all_channels.append({
                "name": ch["name"],
                "url": ch["url"],
                "speed": ch.get("speed", 0),
                "speed_ms": ch.get("speed_ms", 0),
                "ip": r["ip"],
                "port": r["port"]
            })

    return jsonify({
        "results": [{
            "ip": r["ip"],
            "port": r["port"],
            "source_type": r["source_type"],
            "channels": [{"name": ch["name"], "url": ch["url"], "speed": ch.get("speed", 0), "speed_ms": ch.get("speed_ms", 0)} for ch in r["channels"]],
            "channel_count": r.get("channel_count", len(r["channels"])),
            "avg_speed": r.get("avg_speed", 0),
            "max_speed": r.get("max_speed", 0),
            "avg_speed_ms": r.get("avg_speed_ms", 0),
            "min_speed_ms": r.get("min_speed_ms", 0)
        } for r in results],
        "all_channels": all_channels,
        "valid_ips": [f"{ip}:{port}" for ip, port in valid_ips],
        "count": len(results),
        "total_channels": len(all_channels),
        "has_aiohttp": HAS_AIOHTTP
    })


@app.route('/api/progress')
def api_progress():
    """获取扫描进度"""
    with progress_lock:
        return jsonify(dict(scan_progress))


# ===== IPTV 频道查询 API =====

@app.route('/api/iptv/query')
def api_iptv_query():
    """
    代理查询 IPTV 频道列表（酒店频道查询）
    自动尝试两个数据源接口，哪个能连通就用哪个返回频道列表：
      1) /iptv/live/1000.json?key=txiptv        (JSON 格式)
      2) /ZHGXTV/Public/json/live_interface.txt  (TXT 格式)
    GET /api/iptv/query?server=183.2.73.7:9901
    """
    server = request.args.get('server', '').strip()

    if not server:
        return jsonify({'code': -1, 'msg': '缺少 server 参数'}), 400

    # 安全校验
    if not re.match(r'^[\w.\-\[\]:]+$', server):
        return jsonify({'code': -1, 'msg': '非法的 server 参数'}), 400

    if not server.startswith('http://') and not server.startswith('https://'):
        server = 'http://' + server

    base = server.rstrip('/')
    # 从地址中解析出 ip / port，供 TXT 接口重写频道地址时使用
    _m = re.match(r'https?://([^/:]+)(?::(\d+))?', base)
    ip = _m.group(1) if _m else ''
    port = _m.group(2) if (_m and _m.group(2)) else '80'

    def _to_relative(url):
        """把完整地址转成相对路径，方便前端统一拼接 http://server 前缀"""
        u = str(url).strip()
        if u.startswith(base):
            return u[len(base):]
        _hm = re.match(r'https?://[^/]+', u)
        if _hm:
            return u[_hm.end():]
        return u

    errors = []

    # ---------- 接口 1：JSON ----------
    json_url = base + '/iptv/live/1000.json?key=txiptv'
    try:
        resp = http_requests.get(
            json_url,
            headers={'Accept': 'application/json'},
            timeout=(5, 15),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 0:
            errors.append(f'JSON 接口返回异常(code={data.get("code")})')
        else:
            data['source'] = 'json'
            return jsonify(data)
    except http_requests.exceptions.ConnectTimeout:
        errors.append('JSON 接口连接超时')
    except http_requests.exceptions.ReadTimeout:
        errors.append('JSON 接口读取超时')
    except http_requests.exceptions.ConnectionError:
        errors.append('JSON 接口无法连接')
    except http_requests.exceptions.HTTPError as e:
        errors.append(f'JSON 接口返回错误({e.response.status_code})')
    except ValueError:
        errors.append('JSON 接口返回数据非有效 JSON')
    except Exception as e:
        errors.append(f'JSON 接口未知错误: {str(e)}')

    # ---------- 接口 2：TXT（自动兜底，使用 http_get 自动检测编码）----------
    txt_url = base + '/ZHGXTV/Public/json/live_interface.txt'
    try:
        code, text = http_get(txt_url, timeout=10)
        if code != 200 or text is None:
            errors.append(f'TXT 接口请求失败({code})')
        elif len(text) < 100:
            errors.append(f'TXT 接口返回数据过短({len(text)}字节)')
        else:
            channels = parse_txt_source(ip, port, text)
            if not channels:
                errors.append('TXT 接口未解析到任何频道')
            else:
                data = [{'num': i + 1, 'name': n, 'typename': '', 'url': _to_relative(u)} for i, (n, u) in enumerate(channels)]
                return jsonify({
                    'code': 0,
                    'count': len(data),
                    'data': data,
                    'source': 'txt',
                })
    except Exception as e:
        errors.append(f'TXT 接口未知错误: {str(e)}')

    # 两个接口都失败
    return jsonify({
        'code': -1,
        'msg': '两个数据源接口均未能获取频道列表：' + '；'.join(errors),
        'count': 0,
        'data': [],
    }), 502


@app.route('/api/iptv/verify', methods=['POST'])
def api_iptv_verify():
    """
    对 IPTV 频道列表中的每个直播源进行并发拉流验证
    POST body: { server: "ip:port", channels: [{name, url}, ...] }
    返回: { code: 0, data: [{name, url, speed_ms}, ...], total, valid_count }
    """
    data = request.get_json()
    server = (data.get('server') or '').strip()
    raw_channels = data.get('channels', [])
    concurrency = min(data.get('concurrency', 30), 50)
    timeout = min(data.get('timeout', 8), 15)

    if not raw_channels:
        return jsonify({'code': 1, 'msg': '频道列表为空'})

    # 构建完整的频道 URL 列表
    base = server
    if not base.startswith('http://') and not base.startswith('https://'):
        base = 'http://' + base
    base = base.rstrip('/')

    channels_to_check = []
    for ch in raw_channels:
        name = ch.get('name', '')
        url_raw = ch.get('url', '')
        if not url_raw:
            continue
        # 拼接完整 URL
        if url_raw.startswith('http://') or url_raw.startswith('https://'):
            full_url = url_raw
        elif url_raw.startswith('/'):
            full_url = base + url_raw
        else:
            full_url = base + '/' + url_raw
        channels_to_check.append((name, full_url))

    if not channels_to_check:
        return jsonify({'code': 1, 'msg': '无有效频道地址'})

    total = len(channels_to_check)

    # 无 aiohttp 时直接返回全部（标记未验证）
    if not HAS_AIOHTTP:
        return jsonify({
            'code': 0,
            'data': [{'name': n, 'url': u, 'speed_ms': 0} for n, u in channels_to_check],
            'total': total,
            'valid_count': total,
            'verified': False,
            'msg': 'aiohttp 未安装，跳过拉流验证'
        })

    # 并发拉流验证（复用已有的 verify_streams_sync）
    results = verify_streams_sync(channels_to_check, max_concurrent=concurrency, timeout=timeout)

    valid_channels = []
    for name, url, is_valid, speed_kbps, speed_ms in results:
        if is_valid and name:
            valid_channels.append({
                'name': name,
                'url': url,
                'speed_ms': round(speed_ms, 1),
                'speed_kbps': round(speed_kbps, 1)
            })

    # 按速度排序（快的在前）
    valid_channels.sort(key=lambda x: x['speed_ms'])

    return jsonify({
        'code': 0,
        'data': valid_channels,
        'total': total,
        'valid_count': len(valid_channels),
        'verified': True
    })


# ===== 组播 IP 筛选 API =====

@app.route('/api/multicast/config', methods=['GET'])
def api_multicast_config():
    """获取配置文件中的组播列表"""
    items = load_multicast_config()
    return jsonify({'code': 0, 'data': items})


@app.route('/api/multicast/parse', methods=['POST'])
def api_multicast_parse():
    """解析 IP:端口，筛选有效格式，并进行初次连通性检测"""
    data = request.get_json()
    text = data.get('text', '')
    if not text.strip():
        return jsonify({'code': 1, 'msg': '输入为空'})

    all_ips = parse_ip_ports(text)
    valid = [ip for ip in all_ips if is_valid_ip_port(ip)]
    invalid_count = len(all_ips) - len(valid)

    unreachable = []
    reachable = []
    if valid:
        results = run_connectivity_check(valid, timeout=3, concurrency=30)
        for r in results:
            if r['reachable']:
                reachable.append(r['ip_port'])
            else:
                unreachable.append(r['ip_port'])

    return jsonify({
        'code': 0,
        'data': {
            'total': len(all_ips),
            'valid_count': len(valid),
            'invalid_count': invalid_count,
            'reachable_count': len(reachable),
            'unreachable_count': len(unreachable),
            'valid': reachable
        }
    })


@app.route('/api/multicast/check', methods=['POST'])
def api_multicast_check():
    """验证有效 IP 对指定组播地址的可用性，同时测速"""
    data = request.get_json()
    ip_ports = data.get('ip_ports', [])
    multicast_addr = data.get('multicast_addr', '')
    protocol = data.get('protocol', 'udp')
    timeout = data.get('timeout', 5)
    concurrency = data.get('concurrency', 20)

    if not ip_ports:
        return jsonify({'code': 1, 'msg': '无待验证IP'})
    if not multicast_addr:
        return jsonify({'code': 1, 'msg': '未选择组播地址'})

    results = run_async_check(ip_ports, multicast_addr, protocol, timeout, concurrency)
    results.sort(key=lambda x: (x['status'] != 'ok', x['speed_ms']))

    ok_list = [r for r in results if r['status'] == 'ok']
    fail_list = [r for r in results if r['status'] == 'fail']

    return jsonify({
        'code': 0,
        'data': {
            'total': len(results),
            'ok_count': len(ok_list),
            'fail_count': len(fail_list),
            'results': results
        }
    })


@app.route('/api/multicast/play', methods=['POST'])
def api_multicast_play():
    """调用外部播放器播放"""
    data = request.get_json()
    url = data.get('url', '')

    if not url:
        return jsonify({'code': 1, 'msg': '播放地址为空'})

    try:
        player_path, player_name = find_player()

        if not player_path:
            platform = sys.platform
            if platform == 'win32':
                hint = '请安装以下任一播放器：PotPlayer、VLC、MPC-HC、mpv'
            else:
                hint = '服务器未安装播放器。请安装 vlc/mpv/ffplay，或手动复制地址到本地播放器打开'
            return jsonify({
                'code': 1,
                'msg': f'未找到可用播放器。{hint}',
                'data': {'url': url}
            })

        subprocess.Popen([player_path, url], shell=False)
        return jsonify({'code': 0, 'msg': f'已使用 {player_name} 打开播放'})

    except Exception as e:
        return jsonify({'code': 1, 'msg': f'播放失败: {str(e)}'})


# ===== 配置文件编辑 API（需密码） =====

@app.route('/api/config/verify_password', methods=['POST'])
def api_config_verify_password():
    """验证编辑密码"""
    data = request.get_json()
    pwd = data.get('password', '')
    correct_pwd = get_config_password()
    if pwd == correct_pwd:
        return jsonify({'code': 0, 'msg': '验证通过'})
    return jsonify({'code': 1, 'msg': '密码错误'}), 401


@app.route('/api/config/read', methods=['POST'])
def api_config_read():
    """读取 config.ini 全部内容（需密码）"""
    data = request.get_json()
    pwd = data.get('password', '')
    correct_pwd = get_config_password()
    if pwd != correct_pwd:
        return jsonify({'code': 1, 'msg': '密码错误'}), 401
    content = read_full_config()
    return jsonify({'code': 0, 'data': {'content': content}})


@app.route('/api/config/save', methods=['POST'])
def api_config_save():
    """保存 config.ini 内容（需密码）"""
    data = request.get_json()
    pwd = data.get('password', '')
    content = data.get('content', '')
    correct_pwd = get_config_password()
    if pwd != correct_pwd:
        return jsonify({'code': 1, 'msg': '密码错误'}), 401
    if not content.strip():
        return jsonify({'code': 1, 'msg': '内容不能为空'})
    try:
        save_full_config(content)
        # 重新加载组播配置到内存
        return jsonify({'code': 0, 'msg': '保存成功，组播列表已更新'})
    except Exception as e:
        return jsonify({'code': 1, 'msg': f'保存失败: {str(e)}'}), 500


# ===== 代理IP检测 API =====

@app.route('/api/proxy/check', methods=['POST'])
def api_proxy_check():
    """批量检测代理 API（SSE 流式返回）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请求数据为空'}), 400

    text = data.get('proxies', '')
    type_filter = data.get('type', 'all')
    concurrency = min(int(data.get('concurrency', 10)), 50)

    proxies = _parse_proxies(text, type_filter)
    if not proxies:
        return jsonify({'error': '未识别到有效的代理IP（格式：IP:端口）'}), 400

    def generate():
        total = len(proxies)
        results = []
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pexec:
            futures = [pexec.submit(_check_single_proxy, p) for p in proxies]
            for future in futures:
                try:
                    result = future.result(timeout=30)
                except Exception as e:
                    result = {
                        'host': '', 'port': 0, 'type': '',
                        'raw': '', 'valid': False, 'latency': 0,
                        'exit_ip': '', 'location': '', 'error': str(e),
                    }
                results.append(result)
                completed += 1

                event_data = json.dumps({
                    'result': result,
                    'progress': {'completed': completed, 'total': total},
                }, ensure_ascii=False)
                yield f'data: {event_data}\n\n'

        valid_list = [r for r in results if r['valid']]
        invalid_list = [r for r in results if not r['valid']]
        valid_list.sort(key=lambda x: x['latency'])

        location_cache = {}
        for r in valid_list:
            ip = r.get('exit_ip', '')
            loc = r.get('location', '')
            if ip and not loc:
                if ip in location_cache:
                    r['location'] = location_cache[ip]
                else:
                    loc = _query_ip_location(ip)
                    r['location'] = loc
                    location_cache[ip] = loc

        summary = json.dumps({
            'done': True,
            'valid': valid_list,
            'invalid': invalid_list,
            'stats': {
                'total': total,
                'valid_count': len(valid_list),
                'invalid_count': len(invalid_list),
                'avg_latency': round(sum(r['latency'] for r in valid_list) / len(valid_list)) if valid_list else 0,
            }
        }, ensure_ascii=False)
        yield f'data: {summary}\n\n'

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@app.route('/api/proxy/myip', methods=['GET'])
def api_proxy_myip():
    """获取本机出口 IP"""
    try:
        resp = http_requests.get('https://api.ipify.org?format=json', timeout=5)
        data = resp.json()
        return jsonify({'ip': data.get('ip', '未知')})
    except Exception:
        try:
            resp = http_requests.get('https://httpbin.org/ip', timeout=5)
            data = resp.json()
            return jsonify({'ip': data.get('origin', '未知')})
        except Exception:
            return jsonify({'ip': '无法获取'})


# ==================== 启动 ====================

if __name__ == '__main__':
    HOST = os.environ.get('HOST', '0.0.0.0')
    PORT = int(os.environ.get('PORT', '6604'))
    print("=" * 55)
    print("  测绘空间 IP 筛选工具合集")
    print("  ├─ 酒店 IP 筛选（IPTV IP:Port + 频道 + 拉流验证）")
    print("  ├─ 组播 IP 筛选（连通性 + 组播验证 + 测速）")
    print("  ├─ IPTV 频道查询（频道列表 + M3U导出 + 播放器调用）")
    print("  ├─ 代理IP检测（SOCKS5/SOCKS4/HTTP/HTTPS + 地理位置）")
    print(f"  └─ 访问地址: http://127.0.0.1:{PORT}")
    print("=" * 55)
    app.run(host=HOST, port=PORT, debug=False)
