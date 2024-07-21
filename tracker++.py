import asyncio
import aiohttp
import struct
import random
import os
from urllib.parse import urlparse, urlencode
from typing import List, Tuple, Dict
from abc import ABC, abstractmethod
from dataclasses import dataclass
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QWidget, QLineEdit, QPushButton, QTextEdit, QProgressBar, QLabel,
                             QFrame, QSplitter)
from PyQt6.QtCore import QObject, pyqtSignal, QThread, Qt
from PyQt6.QtGui import QFont, QColor, QPalette

@dataclass
class TrackerResult:
    url: str
    is_working: bool
    protocol: str

class TrackerChecker(ABC):
    @abstractmethod
    async def check(self, tracker: str, info_hash: bytes) -> bool:
        pass

class UDPTrackerChecker(TrackerChecker):
    async def check(self, tracker: str, info_hash: bytes) -> bool:
        parsed = urlparse(tracker)
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: UDPTrackerProtocol(loop),
                remote_addr=(parsed.hostname, parsed.port or 80)
            )
            try:
                return await self._perform_udp_check(protocol, info_hash)
            finally:
                transport.close()
        except Exception:
            return False

    async def _perform_udp_check(self, protocol: 'UDPTrackerProtocol', info_hash: bytes) -> bool:
        connection_id = await self._udp_connect(protocol)
        if connection_id is None:
            return False
        return await self._udp_scrape(protocol, connection_id, info_hash)

    async def _udp_connect(self, protocol: 'UDPTrackerProtocol') -> int:
        transaction_id = random.randint(0, 0xFFFFFFFF)
        packet = struct.pack('>QII', 0x41727101980, 0, transaction_id)
        response = await protocol.communicate(packet)
        if response is None:
            return None
        action, received_transaction_id, connection_id = struct.unpack('>IIQ', response)
        return connection_id if (action == 0 and received_transaction_id == transaction_id) else None

    async def _udp_scrape(self, protocol: 'UDPTrackerProtocol', connection_id: int, info_hash: bytes) -> bool:
        transaction_id = random.randint(0, 0xFFFFFFFF)
        packet = struct.pack('>QII', connection_id, 2, transaction_id) + info_hash
        response = await protocol.communicate(packet)
        if response is None:
            return False
        action, received_transaction_id = struct.unpack('>II', response[:8])
        return action == 2 and received_transaction_id == transaction_id

class HTTPTrackerChecker(TrackerChecker):
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def check(self, tracker: str, info_hash: bytes) -> bool:
        try:
            params = {'info_hash': info_hash.decode('latin1')}
            scrape_url = tracker.replace('announce', 'scrape')
            async with self.session.get(scrape_url, params=urlencode(params, doseq=True), timeout=5) as response:
                return response.status < 400
        except Exception:
            return False

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

class TrackerManager:
    def __init__(self, session: aiohttp.ClientSession, max_concurrent: int = 100):
        self.session = session
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.udp_checker = UDPTrackerChecker()
        self.http_checker = HTTPTrackerChecker(session)

    async def check_tracker(self, tracker: str, info_hash: bytes) -> TrackerResult:
        async with self.semaphore:
            protocol = urlparse(tracker).scheme
            checker = self.udp_checker if protocol == 'udp' else self.http_checker
            is_working = await checker.check(tracker, info_hash)
            return TrackerResult(tracker, is_working, protocol)

    async def check_trackers(self, trackers: List[str], info_hash: bytes) -> List[TrackerResult]:
        tasks = [self.check_tracker(tracker, info_hash) for tracker in trackers]
        return await asyncio.gather(*tasks)

async def fetch_tracker_list(session: aiohttp.ClientSession, url: str) -> List[str]:
    async with session.get(url) as response:
        if response.status == 200:
            content = await response.text()
            return [line.strip() for line in content.split('\n') if line.strip()]
        raise Exception(f"无法获取 tracker 列表，HTTP 状态码: {response.status}")

def generate_random_info_hash() -> bytes:
    return os.urandom(20)

class WorkerSignals(QObject):
    progress = pyqtSignal(int)
    result = pyqtSignal(object)
    finished = pyqtSignal()

class Worker(QThread):
    def __init__(self, tracker_list_url):
        super().__init__()
        self.tracker_list_url = tracker_list_url
        self.signals = WorkerSignals()

    async def run_async(self):
        info_hash = generate_random_info_hash()
        output_file = f"available_trackers_{info_hash.hex()[:16]}.txt"

        async with aiohttp.ClientSession() as session:
            trackers = await fetch_tracker_list(session, self.tracker_list_url)
            self.signals.progress.emit(0)

            manager = TrackerManager(session)
            results = []
            for i, result in enumerate(await manager.check_trackers(trackers, info_hash)):
                results.append(result)
                self.signals.progress.emit(int((i + 1) / len(trackers) * 100))

            working_trackers = [r for r in results if r.is_working]
            udp_success = sum(1 for r in working_trackers if r.protocol == 'udp')
            http_success = sum(1 for r in working_trackers if r.protocol in ('http', 'https'))

            with open(output_file, "w") as f:
                for result in working_trackers:
                    f.write(f"{result.url}\n")

            summary = f"检查完成 - 成功: {len(working_trackers)}, 失败: {len(trackers) - len(working_trackers)}, 总数: {len(trackers)}\n"
            summary += f"UDP 成功: {udp_success}, HTTP 成功: {http_success}\n"
            summary += f"可用的 trackers 已保存到 {output_file}"

            self.signals.result.emit((summary, working_trackers))

    def run(self):
        asyncio.run(self.run_async())
        self.signals.finished.emit()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tracker Checker")
        self.setGeometry(100, 100, 800, 600)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f0f0f0;
            }
            QLineEdit, QTextEdit {
                background-color: white;
                border: 1px solid #dcdcdc;
                border-radius: 4px;
                padding: 5px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
            QProgressBar {
                border: 1px solid #dcdcdc;
                border-radius: 4px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
            }
        """)

        main_layout = QVBoxLayout()

        url_layout = QHBoxLayout()
        self.url_input = QLineEdit("https://raw.githubusercontent.com/EricLeeaaaaa/TrackersList/main/all.txt")
        self.url_input.setPlaceholderText("Enter tracker list URL")
        self.check_button = QPushButton("Check Trackers")
        url_layout.addWidget(self.url_input, 7)
        url_layout.addWidget(self.check_button, 3)
        main_layout.addLayout(url_layout)

        self.progress_bar = QProgressBar()
        main_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.status_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        
        self.summary_text = QTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.setFrameStyle(QFrame.Shape.NoFrame)
        splitter.addWidget(self.summary_text)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFrameStyle(QFrame.Shape.NoFrame)
        splitter.addWidget(self.result_text)

        main_layout.addWidget(splitter)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        self.check_button.clicked.connect(self.start_checking)

    def start_checking(self):
        self.check_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Checking...")
        self.summary_text.clear()
        self.result_text.clear()

        self.worker = Worker(self.url_input.text())
        self.worker.signals.progress.connect(self.update_progress)
        self.worker.signals.result.connect(self.show_result)
        self.worker.signals.finished.connect(self.on_finished)
        self.worker.start()

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def show_result(self, result):
        summary, working_trackers = result
        self.summary_text.setText(summary)
        self.result_text.setText("Working Trackers:\n" + "\n".join([t.url for t in working_trackers]))

    def on_finished(self):
        self.check_button.setEnabled(True)
        self.status_label.setText("Check completed")

if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec()