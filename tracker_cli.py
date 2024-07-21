import asyncio
import aiohttp
import struct
import random
import os
from urllib.parse import urlparse, urlencode
from tqdm import tqdm

def generate_random_info_hash():
    return os.urandom(20)

class UDPTrackerProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.response = asyncio.Future()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not self.response.done():
            self.response.set_result(data)

    def error_received(self, exc):
        if not self.response.done():
            self.response.set_exception(exc)

    def connection_lost(self, exc):
        if not self.response.done():
            self.response.set_exception(exc or ConnectionError("Connection closed"))

async def check_udp_tracker(tracker, info_hash, timeout=5):
    parsed = urlparse(tracker)
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            UDPTrackerProtocol,
            remote_addr=(parsed.hostname, parsed.port or 80)
        )

        try:
            # Connection request
            transaction_id = random.randint(0, 0xFFFFFFFF)
            packet = struct.pack('>QII', 0x41727101980, 0, transaction_id)
            transport.sendto(packet)

            data = await asyncio.wait_for(protocol.response, timeout=timeout)
            action, received_transaction_id, connection_id = struct.unpack('>IIQ', data)

            if action != 0 or received_transaction_id != transaction_id:
                return False

            # Reset the response future
            protocol.response = asyncio.Future()

            # Scrape request
            transaction_id = random.randint(0, 0xFFFFFFFF)
            packet = struct.pack('>QII', connection_id, 2, transaction_id) + info_hash
            transport.sendto(packet)

            data = await asyncio.wait_for(protocol.response, timeout=timeout)
            action, received_transaction_id = struct.unpack('>II', data[:8])

            return action == 2 and received_transaction_id == transaction_id
        finally:
            transport.close()

    except Exception:
        return False

async def check_http_tracker(session, tracker, info_hash, timeout=5):
    try:
        params = {'info_hash': info_hash.decode('latin1')}
        scrape_url = tracker.replace('announce', 'scrape')
        async with session.get(scrape_url, params=urlencode(params, doseq=True), timeout=timeout) as response:
            return response.status < 400
    except:
        return False

async def check_tracker(session, tracker, info_hash):
    if tracker.startswith('udp:'):
        return await check_udp_tracker(tracker, info_hash)
    else:
        return await check_http_tracker(session, tracker, info_hash)

async def fetch_tracker_list(session, url):
    async with session.get(url) as response:
        if response.status == 200:
            content = await response.text()
            return [line.strip() for line in content.split('\n') if line.strip()]
        raise Exception(f"无法获取 tracker 列表，HTTP 状态码: {response.status}")

async def main(tracker_list_url, max_concurrent=50):
    info_hash = generate_random_info_hash()
    output_file = f"available_trackers_{info_hash.hex()[:16]}.txt"

    async with aiohttp.ClientSession() as session:
        trackers = await fetch_tracker_list(session, tracker_list_url)
        print(f"成功获取 {len(trackers)} 个 trackers")

        semaphore = asyncio.Semaphore(max_concurrent)
        async def bounded_check(tracker):
            async with semaphore:
                return tracker, await check_tracker(session, tracker, info_hash)

        tasks = [bounded_check(tracker) for tracker in trackers]
        
        success_count = udp_success = http_success = 0
        with tqdm(total=len(tasks), desc="检查 Trackers", ncols=100) as pbar, open(output_file, "w") as f:
            for future in asyncio.as_completed(tasks):
                tracker, result = await future
                if result:
                    success_count += 1
                    if tracker.startswith('udp:'):
                        udp_success += 1
                    else:
                        http_success += 1
                    f.write(f"{tracker}\n")
                pbar.update(1)
                pbar.set_postfix({
                    "成功": success_count, 
                    "UDP": udp_success, 
                    "HTTP": http_success
                }, refresh=True)

        print(f"\n检查完成 - 成功: {success_count}, 失败: {len(trackers) - success_count}, 总数: {len(trackers)}")
        print(f"UDP 成功: {udp_success}, HTTP 成功: {http_success}")
        print(f"可用的 trackers 已保存到 {output_file}")


if __name__ == "__main__":
    tracker_list_url = "https://raw.githubusercontent.com/EricLeeaaaaa/TrackersList/main/all.txt"
    asyncio.run(main(tracker_list_url, max_concurrent=100))