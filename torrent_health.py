import aiohttp
import asyncio
import bencodepy
import random
import sys
from urllib.parse import urlparse, urlencode
import struct
import websockets
import json
import string
from tqdm.asyncio import tqdm
import time
import re
import logging
from yarl import URL
from bs4 import BeautifulSoup

# 配置参数
MAX_RETRIES = 3  # 最大重试次数
TIMEOUT = 10  # 超时时间(秒)
TCP_CONNECTOR_LIMIT_PER_HOST = 10  # 每个主机的TCP连接限制
TCP_CONNECTOR_LIMIT = 100  # 总TCP连接限制
DEFAULT_THRESHOLD = 1  # 默认活跃用户阈值

# 初始化日志
logging.basicConfig(level=logging.ERROR)  # 设置日志级别为ERROR以减少输出
logger = logging.getLogger(__name__)

async def generate_qbittorrent_peer_id() -> str:
    """生成qBittorrent的peer ID"""
    version = "4650"  # qBittorrent版本4.6.5
    random_chars = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    return f"-qB{version}-{random_chars}"

def parse_tracker_url(url: str) -> tuple:
    """解析tracker URL,提取scheme、hostname和port"""
    parsed = urlparse(url)
    scheme = parsed.scheme
    port = parsed.port or (80 if scheme in ('http', 'ws') else 443 if scheme in ('https', 'wss') else 6969)
    return scheme, parsed.hostname, port

async def udp_scrape(tracker: str, info_hash: str) -> tuple:
    """从UDP tracker获取做种数和下载数"""
    parsed = urlparse(tracker)
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UDPTrackerProtocol(loop),
            remote_addr=(parsed.hostname, parsed.port or 6969)
        )
        try:
            # 建立连接
            connection_id = await udp_connect(protocol)
            if connection_id is None:
                return 0, 0

            # 发送scrape请求
            transaction_id = random.randint(0, 0xFFFFFFFF)
            packet = struct.pack('>QII', connection_id, 2, transaction_id) + bytes.fromhex(info_hash)
            response = await protocol.communicate(packet)
            if response is None or len(response) < 20:
                logger.error(f"Invalid UDP response length for {tracker}")
                return 0, 0

            action, received_transaction_id = struct.unpack('>II', response[:8])
            if action != 2 or received_transaction_id != transaction_id:
                return 0, 0

            seeders, _, leechers = struct.unpack('>III', response[8:20])
            return seeders, leechers
        finally:
            transport.close()
    except Exception as e:
        logger.error(f"UDP scrape error for {tracker}: {str(e)}")
        return 0, 0

async def udp_connect(protocol: 'UDPTrackerProtocol') -> int:
    """与UDP tracker建立连接"""
    transaction_id = random.randint(0, 0xFFFFFFFF)
    packet = struct.pack('>QII', 0x41727101980, 0, transaction_id)
    response = await protocol.communicate(packet)
    if response is None or len(response) < 16:
        logger.error("Invalid UDP connect response length")
        return None
    action, received_transaction_id, connection_id = struct.unpack('>IIQ', response)
    return connection_id if (action == 0 and received_transaction_id == transaction_id) else None

async def ws_scrape(tracker: str, info_hash: str) -> tuple:
    """从WebSocket tracker获取做种数和下载数"""
    # 构造announce URL
    parsed_url = urlparse(tracker)
    ws_url = f"{tracker}/announce" if parsed_url.path != '/announce' else tracker

    # 准备请求数据
    request_data = {
        'action': 'announce',
        'info_hash': info_hash,
        'peer_id': await generate_qbittorrent_peer_id(),
        'port': 6881,
        'uploaded': 0,
        'downloaded': 0,
        'left': 1000000,
        'event': 'started'
    }

    # 首先尝试使用websockets库
    try:
        async with websockets.connect(ws_url, timeout=TIMEOUT) as websocket:
            await websocket.send(json.dumps(request_data))
            response = await asyncio.wait_for(websocket.recv(), timeout=TIMEOUT)
            data = json.loads(response)
            complete = data.get('complete', data.get('seeders', 0))
            incomplete = data.get('incomplete', data.get('leechers', 0))
            return complete, incomplete
    except Exception as e:
        logger.warning(f"WebSocket connection failed, trying HTTP fallback: {str(e)}")

    # 如果websockets失败，尝试使用aiohttp
    return await http_fallback(ws_url, request_data)

async def http_fallback(ws_url: str, request_data: dict) -> tuple:
    """WebSocket失败时的HTTP回退"""
    http_url = URL(ws_url.replace('wss://', 'https://').replace('ws://', 'http://'))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(http_url, params=request_data, timeout=TIMEOUT) as response:
                if response.status == 200:
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'application/json' in content_type:
                        data = await response.json()
                        complete = data.get('complete', data.get('seeders', 0))
                        incomplete = data.get('incomplete', data.get('leechers', 0))
                        return complete, incomplete
                    elif 'text/html' in content_type:
                        return parse_html_response(await response.text())
                    elif 'application/octet-stream' in content_type:
                        return parse_binary_response(await response.read())
                    else:
                        return parse_unknown_response(await response.read(), content_type)
                logger.error(f"HTTP request failed with status {response.status} for {ws_url}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"HTTP request error for {ws_url}: {str(e)}")
    except Exception as e:
        logger.error(f"Error during HTTP fallback for {ws_url}: {str(e)}")
    return 0, 0

def parse_html_response(html_content: str) -> tuple:
    """尝试从HTML响应中提取做种数和下载数"""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 方法1：查找包含"Seeders:"和"Leechers:"的文本
        seeders = soup.find(string=re.compile(r'Seeders:'))
        leechers = soup.find(string=re.compile(r'Leechers:'))
        
        if seeders and leechers:
            seeders_count = int(re.search(r'\d+', seeders).group())
            leechers_count = int(re.search(r'\d+', leechers).group())
            return seeders_count, leechers_count
        
        # 方法2：查找可能包含做种数和下载数的元素
        seeders_elem = soup.find(['span', 'div', 'p'], class_=re.compile(r'seeder|seed', re.I))
        leechers_elem = soup.find(['span', 'div', 'p'], class_=re.compile(r'leecher|leech', re.I))
        
        if seeders_elem and leechers_elem:
            seeders_count = int(re.search(r'\d+', seeders_elem.text).group())
            leechers_count = int(re.search(r'\d+', leechers_elem.text).group())
            return seeders_count, leechers_count

    except Exception as e:
        logger.error(f"Failed to parse HTML response: {str(e)}")
    
    return 0, 0

def parse_binary_response(binary_content: bytes) -> tuple:
    """尝试从二进制响应中提取做种数和下载数"""
    try:
        # 尝试解码为bencode格式
        decoded = bencodepy.decode(binary_content)
        if isinstance(decoded, dict):
            complete = decoded.get(b'complete', 0)
            incomplete = decoded.get(b'incomplete', 0)
            return complete, incomplete
    except bencodepy.exceptions.BencodeDecodeError:
        # 如果不是bencode格式，可以尝试其他二进制格式的解析
        pass
    except Exception as e:
        logger.error(f"Failed to parse binary response: {str(e)}")
    
    return 0, 0

def parse_unknown_response(content: bytes, content_type: str) -> tuple:
    """尝试解析未知类型的响应"""
    try:
        # 尝试解析为文本
        text_content = content.decode('utf-8', errors='ignore')
        
        # 尝试解析为JSON
        try:
            data = json.loads(text_content)
            complete = data.get('complete', data.get('seeders', 0))
            incomplete = data.get('incomplete', data.get('leechers', 0))
            return complete, incomplete
        except json.JSONDecodeError:
            pass
        
        # 尝试从文本中提取数字
        numbers = re.findall(r'\d+', text_content)
        if len(numbers) >= 2:
            return int(numbers[0]), int(numbers[1])
        
    except Exception as e:
        logger.error(f"Failed to parse unknown response type ({content_type}): {str(e)}")
    
    return 0, 0

def is_html_response(content: bytes) -> bool:
    """检查响应内容是否为HTML"""
    return re.search(b'<html|<!DOCTYPE', content, re.IGNORECASE) is not None

async def check_tracker(session: aiohttp.ClientSession, tracker: str, info_hash: str) -> tuple:
    """检查tracker的做种数和下载数"""
    for _ in range(MAX_RETRIES):
        try:
            scheme, hostname, port = parse_tracker_url(tracker)

            if scheme in ('http', 'https'):
                # 构造HTTP/HTTPS请求参数
                params = {
                    'info_hash': bytes.fromhex(info_hash),
                    'peer_id': await generate_qbittorrent_peer_id(),
                    'port': 6881,
                    'uploaded': 0,
                    'downloaded': 0,
                    'left': 1000000,
                    'compact': 1,
                    'event': 'started',
                    'numwant': 200,
                    'supportcrypto': 1,
                    'no_peer_id': 1
                }
                headers = {
                    'User-Agent': 'qBittorrent/4.6.5',
                    'Accept-Encoding': 'gzip',
                    'Connection': 'close'
                }
                url = f"{tracker}?{urlencode(params)}"
                async with session.get(url, headers=headers, timeout=TIMEOUT) as response:
                    content = await response.read()
                    if is_html_response(content):
                        logger.warning(f"Tracker {tracker} returned HTML instead of bencoded data")
                        return tracker, 0, 0
                    try:
                        decoded = bencodepy.decode(content)
                        if isinstance(decoded, dict):
                            seeders = decoded.get(b'complete', 0)
                            leechers = decoded.get(b'incomplete', 0)
                            return tracker, seeders, leechers
                    except bencodepy.exceptions.BencodeDecodeError:
                        logger.error(f"Failed to decode response from {tracker}")
            elif scheme == 'udp':
                seeders, leechers = await asyncio.wait_for(udp_scrape(tracker, info_hash), timeout=TIMEOUT)
                return tracker, seeders, leechers
            elif scheme in ('ws', 'wss'):
                seeders, leechers = await asyncio.wait_for(ws_scrape(tracker, info_hash), timeout=TIMEOUT)
                return tracker, seeders, leechers
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error for {tracker}: {str(e)}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout for {tracker}, retrying...")
        except Exception as e:
            logger.error(f"Error for {tracker}: {str(e)}")

        await asyncio.sleep(1)
    
    return tracker, 0, 0

class UDPTrackerProtocol(asyncio.DatagramProtocol):
    """用于与UDP tracker通信的自定义UDP协议"""
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.transport = None
        self.future = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self.future and not self.future.done():
            self.future.set_result(data)

    def error_received(self, exc):
        if self.future and not self.future.done():
            self.future.set_exception(exc)

    def connection_lost(self, exc):
        if self.future and not self.future.done():
            self.future.set_exception(exc or ConnectionError("Connection closed"))

    async def communicate(self, data: bytes, timeout: float = 5) -> bytes:
        """发送数据并等待响应"""
        self.future = self.loop.create_future()
        self.transport.sendto(data)
        try:
            return await asyncio.wait_for(self.future, timeout)
        except asyncio.TimeoutError:
            return None

async def check_torrent_health(info_hash: str, tracker_file: str, threshold: int):
    """检查种子的健康状况,查询所有tracker"""
    # 读取tracker列表
    with open(tracker_file, 'r') as f:
        trackers = [line.strip() for line in f if line.strip()]

    total_seeders = 0
    total_leechers = 0
    active_trackers = []

    # 创建TCP连接池
    conn = aiohttp.TCPConnector(limit_per_host=TCP_CONNECTOR_LIMIT_PER_HOST, limit=TCP_CONNECTOR_LIMIT)
    async with aiohttp.ClientSession(connector=conn) as session:
        # 创建并执行所有tracker检查任务
        tasks = [check_tracker(session, tracker, info_hash) for tracker in trackers]
        for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Checking trackers"):
            tracker, seeders, leechers = await task
            if seeders + leechers >= threshold:
                active_trackers.append((tracker, seeders, leechers))
                total_seeders += seeders
                total_leechers += leechers

    # 输出结果
    print(f"总做种数: {total_seeders}")
    print(f"总下载数: {total_leechers}")
    print(f"总用户数: {total_seeders + total_leechers}")
    print(f"有活跃用户的Tracker数: {len(active_trackers)}")

    print("\n有活跃用户的Trackers（详细信息）:")
    for tracker, seeders, leechers in sorted(active_trackers, key=lambda x: x[1], reverse=True):
        print(f"{tracker} | 总用户数: {seeders + leechers} | 做种数: {seeders}")

    print("\n有活跃用户的Trackers（仅URL）:")
    for tracker, _, _ in sorted(active_trackers, key=lambda x: x[1], reverse=True):
        print(tracker)

if __name__ == "__main__":
    # 检查命令行参数
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("使用方法: python script.py <info_hash> <tracker_file> [threshold]")
        sys.exit(1)

    info_hash = sys.argv[1]
    tracker_file = sys.argv[2]
    threshold = int(sys.argv[3]) if len(sys.argv) == 4 else DEFAULT_THRESHOLD
    
    # 为Windows设置事件循环策略
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # 运行主函数并计时
    start_time = time.time()
    asyncio.run(check_torrent_health(info_hash, tracker_file, threshold))
    end_time = time.time()
    
    print(f"\n总运行时间: {end_time - start_time:.2f} 秒")