import requests
import bencodepy
import random
import sys
import asyncio
from urllib.parse import urlparse, urlencode
import socket
import struct
import websockets
import json
import string

def generate_qbittorrent_peer_id():
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

def udp_scrape(tracker, info_hash):
    scheme, hostname, port = parse_tracker_url(tracker)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)

    try:
        connection_id = 0x41727101980
        transaction_id = random.randint(0, 2**32 - 1)
        
        # Connection request
        packet = struct.pack(">QLL", connection_id, 0, transaction_id)
        sock.sendto(packet, (hostname, port))
        response = sock.recv(16)
        
        _, action, response_transaction_id = struct.unpack(">LLL", response[:12])
        if action != 0 or response_transaction_id != transaction_id:
            return 0

        connection_id, = struct.unpack(">Q", response[8:])

        # Scrape request
        action = 2
        transaction_id = random.randint(0, 2**32 - 1)
        packet = struct.pack(">QLL20s", connection_id, action, transaction_id, bytes.fromhex(info_hash))
        sock.sendto(packet, (hostname, port))
        response = sock.recv(8 + 12)

        _, action, response_transaction_id = struct.unpack(">LLL", response[:12])
        if action != 2 or response_transaction_id != transaction_id:
            return 0

        seeders, _, _ = struct.unpack(">LLL", response[12:])
        return seeders
    except Exception as e:
        print(f"UDP Scrape error: {str(e)}")
        return 0
    finally:
        sock.close()

async def ws_scrape(tracker, info_hash):
    try:
        async with websockets.connect(tracker) as websocket:
            await websocket.send(json.dumps({
                'action': 'scrape',
                'info_hash': info_hash
            }))
            response = await websocket.recv()
            data = json.loads(response)
            return data.get('complete', 0)
    except Exception as e:
        print(f"WebSocket error: {str(e)}")
        return 0

async def check_torrent_health(info_hash, tracker_file):
    with open(tracker_file, 'r') as f:
        trackers = [line.strip() for line in f if line.strip()]

    total_seeders = 0
    active_trackers = 0

    print(f"检查种子健康状况 (Info Hash: {info_hash})")
    print("-" * 50)

    for tracker in trackers:
        try:
            seeders = 0
            scheme, hostname, port = parse_tracker_url(tracker)

            if scheme in ('http', 'https'):
                params = {
                    'info_hash': bytes.fromhex(info_hash),
                    'peer_id': generate_qbittorrent_peer_id(),
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
                response = requests.get(url, headers=headers, timeout=5)
                response.raise_for_status()
                try:
                    decoded = bencodepy.decode(response.content)
                    if isinstance(decoded, dict):
                        seeders = decoded.get(b'complete', 0)
                    else:
                        print(f"Unexpected response format from {tracker}")
                except bencodepy.exceptions.BencodeDecodeError:
                    print(f"Failed to decode response from {tracker}")
            elif scheme == 'udp':
                seeders = udp_scrape(tracker, info_hash)
            elif scheme in ('ws', 'wss'):
                seeders = await ws_scrape(tracker, info_hash)
            else:
                print(f"Unsupported scheme {scheme} for tracker {tracker}")
                continue

            print(f"Tracker: {tracker}")
            print(f"做种用户数: {seeders}")
            print("-" * 50)

            total_seeders += seeders
            active_trackers += 1

        except requests.exceptions.RequestException as e:
            print(f"Tracker: {tracker}")
            print(f"HTTP Request Error: {str(e)}")
            print("-" * 50)
        except Exception as e:
            print(f"Tracker: {tracker}")
            print(f"错误: {str(e)}")
            print("-" * 50)

        await asyncio.sleep(1)

    print(f"活跃Tracker数: {active_trackers}")
    print(f"总做种用户数: {total_seeders}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("使用方法: python script.py <info_hash> <tracker_file>")
        sys.exit(1)

    info_hash = sys.argv[1]
    tracker_file = sys.argv[2]
    asyncio.run(check_torrent_health(info_hash, tracker_file))