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

MAX_RETRIES = 3
TIMEOUT = 10

async def generate_qbittorrent_peer_id():
    version = "4650"  # qBittorrent 版本 4.6.5
    random_chars = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    return f"-qB{version}-{random_chars}"

def parse_tracker_url(url):
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

async def udp_scrape(tracker, info_hash):
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
        print(f"UDP scrape error for {tracker}: {str(e)}")
        return 0, 0

async def udp_connect(protocol: 'UDPTrackerProtocol') -> int:
    transaction_id = random.randint(0, 0xFFFFFFFF)
    packet = struct.pack('>QII', 0x41727101980, 0, transaction_id)
    response = await protocol.communicate(packet)
    if response is None:
        return None
    action, received_transaction_id, connection_id = struct.unpack('>IIQ', response)
    return connection_id if (action == 0 and received_transaction_id == transaction_id) else None

async def ws_scrape(tracker, info_hash):
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
        print(f"Invalid WebSocket URI: {tracker}")
    except websockets.exceptions.ConnectionClosed:
        print(f"WebSocket connection closed unexpectedly: {tracker}")
    except asyncio.TimeoutError:
        print(f"WebSocket connection timed out: {tracker}")
    except json.JSONDecodeError:
        print(f"Invalid JSON response from WebSocket: {tracker}")
    except Exception as e:
        print(f"WebSocket scrape error for {tracker}: {str(e)}")
    return 0, 0

def is_html_response(content):
    return re.search(b'<html|<!DOCTYPE', content, re.IGNORECASE) is not None

async def check_tracker(session, tracker, info_hash):
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
                        print(f"Tracker {tracker} returned HTML instead of bencoded data")
                        return tracker, 0, 0
                    try:
                        decoded = bencodepy.decode(content)
                        if isinstance(decoded, dict):
                            seeders = decoded.get(b'complete', 0)
                            leechers = decoded.get(b'incomplete', 0)
                            return tracker, seeders, leechers
                    except bencodepy.exceptions.BencodeDecodeError:
                        print(f"Failed to decode response from {tracker}")
            elif scheme == 'udp':
                seeders, leechers = await asyncio.wait_for(udp_scrape(tracker, info_hash), timeout=TIMEOUT)
                if seeders > 0 or leechers > 0:
                    print(f"UDP tracker responded: {tracker} - Seeders: {seeders}, Leechers: {leechers}")
                return tracker, seeders, leechers
            elif scheme in ('ws', 'wss'):
                seeders, leechers = await asyncio.wait_for(ws_scrape(tracker, info_hash), timeout=TIMEOUT)
                return tracker, seeders, leechers
        except aiohttp.ClientError as e:
            print(f"HTTP error for {tracker}: {str(e)}")
        except asyncio.TimeoutError:
            print(f"Timeout for {tracker}, retrying...")
        except Exception as e:
            print(f"Error for {tracker}: {str(e)}")
        
        await asyncio.sleep(1)
    
    return tracker, 0, 0

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

    async def communicate(self, data: bytes, timeout: float = 5) -> bytes:
        self.future = self.loop.create_future()
        self.transport.sendto(data)
        try:
            return await asyncio.wait_for(self.future, timeout)
        except asyncio.TimeoutError:
            return None

async def check_torrent_health(info_hash, tracker_file):
    with open(tracker_file, 'r') as f:
        trackers = [line.strip() for line in f if line.strip()]

    total_seeders = 0
    total_leechers = 0
    active_trackers = []

    conn = aiohttp.TCPConnector(limit_per_host=10, limit=100)
    async with aiohttp.ClientSession(connector=conn) as session:
        tasks = [check_tracker(session, tracker, info_hash) for tracker in trackers]
        for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Checking trackers"):
            tracker, seeders, leechers = await task
            if seeders > 0 or leechers > 0:
                active_trackers.append(f"{tracker} | {seeders + leechers} | {seeders}")
                total_seeders += seeders
                total_leechers += leechers

    print(f"总做种数: {total_seeders}")
    print(f"总下载数: {total_leechers}")
    print(f"总用户数: {total_seeders + total_leechers}")
    print(f"有活跃用户的Tracker数: {len(active_trackers)}")
    print("\n有活跃用户的Trackers:")
    for tracker in sorted(active_trackers, key=lambda x: int(x.split('|')[2].strip()), reverse=True):
        print(tracker)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("使用方法: python script.py <info_hash> <tracker_file>")
        sys.exit(1)

    info_hash = sys.argv[1]
    tracker_file = sys.argv[2]
    
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    start_time = time.time()
    asyncio.run(check_torrent_health(info_hash, tracker_file))
    end_time = time.time()
    
    print(f"\n总运行时间: {end_time - start_time:.2f} 秒")