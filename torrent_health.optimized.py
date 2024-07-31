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
from typing import Tuple, Optional

# 配置
MAX_RETRIES = 3
TIMEOUT = 10
TCP_CONNECTOR_LIMIT_PER_HOST = 10
TCP_CONNECTOR_LIMIT = 100
DEFAULT_THRESHOLD = 1

# 日志配置
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class TrackerChecker:
    def __init__(self, info_hash: str, tracker_file: str, threshold: int):
        self.info_hash = info_hash
        self.tracker_file = tracker_file
        self.threshold = threshold
        self.total_seeders = 0
        self.total_leechers = 0
        self.active_trackers = []

    @staticmethod
    async def generate_peer_id() -> str:
        version = "4650"
        random_chars = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        return f"-qB{version}-{random_chars}"

    @staticmethod
    def parse_tracker_url(url: str) -> Tuple[str, str, int]:
        parsed = urlparse(url)
        scheme = parsed.scheme
        hostname = parsed.hostname
        port = parsed.port or {'http': 80, 'https': 443, 'udp': 6969, 'ws': 80, 'wss': 443}.get(scheme, 80)
        return scheme, hostname, port

    async def check_tracker(self, session: aiohttp.ClientSession, tracker: str) -> Tuple[str, int, int]:
        for _ in range(MAX_RETRIES):
            try:
                scheme, hostname, port = self.parse_tracker_url(tracker)
                if scheme in ('http', 'https'):
                    return await self.http_scrape(session, tracker)
                elif scheme == 'udp':
                    return await self.udp_scrape(tracker)
                elif scheme in ('ws', 'wss'):
                    return await self.ws_scrape(tracker)
            except Exception as e:
                logger.error(f"Error for {tracker}: {str(e)}")
            await asyncio.sleep(1)
        return tracker, 0, 0

    async def http_scrape(self, session: aiohttp.ClientSession, tracker: str) -> Tuple[str, int, int]:
        params = {
            'info_hash': bytes.fromhex(self.info_hash),
            'peer_id': await self.generate_peer_id(),
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
            if self.is_html_response(content):
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
        return tracker, 0, 0

    async def udp_scrape(self, tracker: str) -> Tuple[str, int, int]:
        parsed = urlparse(tracker)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UDPTrackerProtocol(loop),
            remote_addr=(parsed.hostname, parsed.port or 6969)
        )
        try:
            connection_id = await self.udp_connect(protocol)
            if connection_id is None:
                return tracker, 0, 0

            transaction_id = random.randint(0, 0xFFFFFFFF)
            packet = struct.pack('>QII', connection_id, 2, transaction_id) + bytes.fromhex(self.info_hash)
            response = await protocol.communicate(packet)
            if response is None or len(response) < 20:
                logger.error(f"Invalid UDP response length for {tracker}")
                return tracker, 0, 0

            action, received_transaction_id = struct.unpack('>II', response[:8])
            if action != 2 or received_transaction_id != transaction_id:
                return tracker, 0, 0

            seeders, completed, leechers = struct.unpack('>III', response[8:20])
            return tracker, seeders, leechers
        finally:
            transport.close()

    async def udp_connect(self, protocol: 'UDPTrackerProtocol') -> Optional[int]:
        transaction_id = random.randint(0, 0xFFFFFFFF)
        packet = struct.pack('>QII', 0x41727101980, 0, transaction_id)
        response = await protocol.communicate(packet)
        if response is None or len(response) < 16:
            logger.error("Invalid UDP connect response length")
            return None
        action, received_transaction_id, connection_id = struct.unpack('>IIQ', response)
        return connection_id if (action == 0 and received_transaction_id == transaction_id) else None

    async def ws_scrape(self, tracker: str) -> Tuple[str, int, int]:
        parsed_url = urlparse(tracker)
        ws_url = tracker if parsed_url.path == '/announce' else f"{tracker}/announce"

        request_data = {
            'action': 'announce',
            'info_hash': self.info_hash,
            'peer_id': await self.generate_peer_id(),
            'port': 6881,
            'uploaded': 0,
            'downloaded': 0,
            'left': 1000000,
            'event': 'started'
        }

        try:
            async with websockets.connect(ws_url, timeout=TIMEOUT) as websocket:
                await websocket.send(json.dumps(request_data))
                response = await asyncio.wait_for(websocket.recv(), timeout=TIMEOUT)
                data = json.loads(response)
                complete = data.get('complete', data.get('seeders', 0))
                incomplete = data.get('incomplete', data.get('leechers', 0))
                return tracker, complete, incomplete
        except Exception as e:
            logger.warning(f"WebSocket connection failed, trying HTTP fallback: {str(e)}")

        try:
            http_url = URL(ws_url.replace('wss://', 'https://').replace('ws://', 'http://'))
            async with aiohttp.ClientSession() as session:
                async with session.get(http_url, params=request_data, timeout=TIMEOUT) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '').lower()
                        if 'application/json' in content_type:
                            data = await response.json()
                            complete = data.get('complete', data.get('seeders', 0))
                            incomplete = data.get('incomplete', data.get('leechers', 0))
                            return tracker, complete, incomplete
                        elif 'text/html' in content_type:
                            html_content = await response.text()
                            return tracker, *self.parse_html_response(html_content)
                        elif 'application/octet-stream' in content_type:
                            binary_content = await response.read()
                            return tracker, *self.parse_binary_response(binary_content)
                        else:
                            content = await response.read()
                            return tracker, *self.parse_unknown_response(content, content_type)
                    else:
                        logger.error(f"HTTP request failed with status {response.status} for {tracker}")
        except Exception as e:
            logger.error(f"Error during HTTP fallback for {tracker}: {str(e)}")

        return tracker, 0, 0

    @staticmethod
    def parse_html_response(html_content: str) -> Tuple[int, int]:
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            seeders = soup.find(string=re.compile(r'Seeders:'))
            leechers = soup.find(string=re.compile(r'Leechers:'))
            
            if seeders and leechers:
                seeders_count = int(re.search(r'\d+', seeders).group())
                leechers_count = int(re.search(r'\d+', leechers).group())
                return seeders_count, leechers_count
            
            seeders_elem = soup.find(['span', 'div', 'p'], class_=re.compile(r'seeder|seed', re.I))
            leechers_elem = soup.find(['span', 'div', 'p'], class_=re.compile(r'leecher|leech', re.I))
            
            if seeders_elem and leechers_elem:
                seeders_count = int(re.search(r'\d+', seeders_elem.text).group())
                leechers_count = int(re.search(r'\d+', leechers_elem.text).group())
                return seeders_count, leechers_count
        except Exception as e:
            logger.error(f"Failed to parse HTML response: {str(e)}")
        
        return 0, 0

    @staticmethod
    def parse_binary_response(binary_content: bytes) -> Tuple[int, int]:
        try:
            decoded = bencodepy.decode(binary_content)
            if isinstance(decoded, dict):
                complete = decoded.get(b'complete', 0)
                incomplete = decoded.get(b'incomplete', 0)
                return complete, incomplete
        except Exception as e:
            logger.error(f"Failed to parse binary response: {str(e)}")
        
        return 0, 0

    @staticmethod
    def parse_unknown_response(content: bytes, content_type: str) -> Tuple[int, int]:
        try:
            text_content = content.decode('utf-8', errors='ignore')
            
            try:
                data = json.loads(text_content)
                complete = data.get('complete', data.get('seeders', 0))
                incomplete = data.get('incomplete', data.get('leechers', 0))
                return complete, incomplete
            except json.JSONDecodeError:
                pass
            
            numbers = re.findall(r'\d+', text_content)
            if len(numbers) >= 2:
                return int(numbers[0]), int(numbers[1])
            
        except Exception as e:
            logger.error(f"Failed to parse unknown response type ({content_type}): {str(e)}")
        
        return 0, 0

    @staticmethod
    def is_html_response(content: bytes) -> bool:
        return re.search(b'<html|<!DOCTYPE', content, re.IGNORECASE) is not None

    async def check_torrent_health(self):
        with open(self.tracker_file, 'r') as f:
            trackers = [line.strip() for line in f if line.strip()]

        conn = aiohttp.TCPConnector(limit_per_host=TCP_CONNECTOR_LIMIT_PER_HOST, limit=TCP_CONNECTOR_LIMIT)
        async with aiohttp.ClientSession(connector=conn) as session:
            tasks = [self.check_tracker(session, tracker) for tracker in trackers]
            for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Checking trackers"):
                tracker, seeders, leechers = await task
                if seeders + leechers >= self.threshold:
                    self.active_trackers.append((tracker, seeders, leechers))
                    self.total_seeders += seeders
                    self.total_leechers += leechers

        self.print_results()

    def print_results(self):
        print(f"总做种数: {self.total_seeders}")
        print(f"总下载数: {self.total_leechers}")
        print(f"总用户数: {self.total_seeders + self.total_leechers}")
        print(f"有活跃用户的Tracker数: {len(self.active_trackers)}")

        print("\n有活跃用户的Trackers（详细信息）:")
        for tracker, seeders, leechers in sorted(self.active_trackers, key=lambda x: x[1], reverse=True):
            print(f"{tracker} | 总用户数: {seeders + leechers} | 做种数: {seeders}")

        print("\n有活跃用户的Trackers（仅URL）:")
        for tracker, _, _ in sorted(self.active_trackers, key=lambda x: x[1], reverse=True):
            print(tracker)

class UDPTrackerProtocol(asyncio.DatagramProtocol):
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

    async def communicate(self, data: bytes, timeout: float = 5) -> Optional[bytes]:
        self.future = self.loop.create_future()
        self.transport.sendto(data)
        try:
            return await asyncio.wait_for(self.future, timeout)
        except asyncio.TimeoutError:
            return None

def main():
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("使用方法: python script.py <info_hash> <tracker_file> [threshold]")
        sys.exit(1)

    info_hash = sys.argv[1]
    tracker_file = sys.argv[2]
    threshold = int(sys.argv[3]) if len(sys.argv) == 4 else DEFAULT_THRESHOLD
    
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    start_time = time.time()
    checker = TrackerChecker(info_hash, tracker_file, threshold)
    asyncio.run(checker.check_torrent_health())
    end_time = time.time()
    
    print(f"\n总运行时间: {end_time - start_time:.2f} 秒")

if __name__ == "__main__":
    main()