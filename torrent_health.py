import aiohttp
import asyncio
import bencodepy
import random
import sys
from urllib.parse import urlparse, urlencode
import socket
import struct
import websockets
import json
import string
from tqdm.asyncio import tqdm
import time
import re
import logging

# Configuration
MAX_RETRIES = 3
TIMEOUT = 10
TCP_CONNECTOR_LIMIT_PER_HOST = 10
TCP_CONNECTOR_LIMIT = 100
DEFAULT_THRESHOLD = 1

# Initialize logging
logging.basicConfig(level=logging.ERROR)  # Set to ERROR to reduce log output
logger = logging.getLogger(__name__)

async def generate_qbittorrent_peer_id() -> str:
    """Generate a peer ID for qBittorrent."""
    version = "4650"  # qBittorrent version 4.6.5
    random_chars = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    return f"-qB{version}-{random_chars}"

def parse_tracker_url(url: str) -> tuple:
    """Parse tracker URL to extract scheme, hostname, and port."""
    parsed = urlparse(url)
    scheme = parsed.scheme
    hostname = parsed.hostname
    port = parsed.port
    
    if port is None:
        if scheme == 'http':
            port = 80
        elif scheme == 'https':
            port = 443
        elif scheme == 'udp':
            port = 6969
        elif scheme in ('ws', 'wss'):
            port = 80 if scheme == 'ws' else 443

    return scheme, hostname, port

async def udp_scrape(tracker: str, info_hash: str) -> tuple:
    """Scrape a UDP tracker for seeders and leechers."""
    parsed = urlparse(tracker)
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UDPTrackerProtocol(loop),
            remote_addr=(parsed.hostname, parsed.port or 6969)
        )
        try:
            # Connect
            connection_id = await udp_connect(protocol)
            if connection_id is None:
                return 0, 0

            # Scrape
            transaction_id = random.randint(0, 0xFFFFFFFF)
            packet = struct.pack('>QII', connection_id, 2, transaction_id) + bytes.fromhex(info_hash)
            response = await protocol.communicate(packet)
            if response is None:
                return 0, 0

            action, received_transaction_id = struct.unpack('>II', response[:8])
            if action != 2 or received_transaction_id != transaction_id:
                return 0, 0

            seeders, completed, leechers = struct.unpack('>III', response[8:20])
            return seeders, leechers
        finally:
            transport.close()
    except Exception as e:
        logger.error(f"UDP scrape error for {tracker}: {str(e)}")
        return 0, 0

async def udp_connect(protocol: 'UDPTrackerProtocol') -> int:
    """Establish a connection to a UDP tracker."""
    transaction_id = random.randint(0, 0xFFFFFFFF)
    packet = struct.pack('>QII', 0x41727101980, 0, transaction_id)
    response = await protocol.communicate(packet)
    if response is None:
        return None
    action, received_transaction_id, connection_id = struct.unpack('>IIQ', response)
    return connection_id if (action == 0 and received_transaction_id == transaction_id) else None

async def ws_scrape(tracker: str, info_hash: str) -> tuple:
    """Scrape a WebSocket tracker for seeders and leechers."""
    try:
        async with websockets.connect(tracker, timeout=TIMEOUT) as websocket:
            await websocket.send(json.dumps({
                'action': 'scrape',
                'info_hash': info_hash
            }))
            response = await asyncio.wait_for(websocket.recv(), timeout=TIMEOUT)
            data = json.loads(response)
            return data.get('complete', 0), data.get('incomplete', 0)
    except websockets.exceptions.InvalidURI:
        logger.error(f"Invalid WebSocket URI: {tracker}")
    except websockets.exceptions.ConnectionClosed:
        logger.error(f"WebSocket connection closed unexpectedly: {tracker}")
    except asyncio.TimeoutError:
        logger.error(f"WebSocket connection timed out: {tracker}")
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON response from WebSocket: {tracker}")
    except Exception as e:
        logger.error(f"WebSocket scrape error for {tracker}: {str(e)}")
    return 0, 0

def is_html_response(content: bytes) -> bool:
    """Check if the response content is HTML."""
    return re.search(b'<html|<!DOCTYPE', content, re.IGNORECASE) is not None

async def check_tracker(session: aiohttp.ClientSession, tracker: str, info_hash: str) -> tuple:
    """Check a tracker for seeders and leechers."""
    for _ in range(MAX_RETRIES):
        try:
            scheme, hostname, port = parse_tracker_url(tracker)

            if scheme in ('http', 'https'):
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
    """Custom UDP protocol for communicating with UDP trackers."""
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
        """Send data and wait for a response."""
        self.future = self.loop.create_future()
        self.transport.sendto(data)
        try:
            return await asyncio.wait_for(self.future, timeout)
        except asyncio.TimeoutError:
            return None

async def check_torrent_health(info_hash: str, tracker_file: str, threshold: int):
    """Check the health of a torrent by querying trackers."""
    with open(tracker_file, 'r') as f:
        trackers = [line.strip() for line in f if line.strip()]

    total_seeders = 0
    total_leechers = 0
    active_trackers = []

    conn = aiohttp.TCPConnector(limit_per_host=TCP_CONNECTOR_LIMIT_PER_HOST, limit=TCP_CONNECTOR_LIMIT)
    async with aiohttp.ClientSession(connector=conn) as session:
        tasks = [check_tracker(session, tracker, info_hash) for tracker in trackers]
        for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Checking trackers"):
            tracker, seeders, leechers = await task
            if seeders + leechers >= threshold:
                active_trackers.append((tracker, seeders, leechers))
                total_seeders += seeders
                total_leechers += leechers

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
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("使用方法: python script.py <info_hash> <tracker_file> [threshold]")
        sys.exit(1)

    info_hash = sys.argv[1]
    tracker_file = sys.argv[2]
    threshold = int(sys.argv[3]) if len(sys.argv) == 4 else DEFAULT_THRESHOLD
    
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    start_time = time.time()
    asyncio.run(check_torrent_health(info_hash, tracker_file, threshold))
    end_time = time.time()
    
    print(f"\n总运行时间: {end_time - start_time:.2f} 秒")