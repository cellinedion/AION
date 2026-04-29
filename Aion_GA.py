import sys
import os
import json
import pymem
import pymem.process
import threading
import time
import psutil
import ctypes
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import QTextCursor

# [기본 설정]
PROC_NAME = "aion.bin"
MOD_NAME = "Game.dll"
POINTER_BASE = 0x010AF5C8

# 파일 저장 경로
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config_settings.json")

# 주소 계산 상수
BASE_CALC_1 = 0x15D9BB4 - 0x613104 - 0xB6400 - 0x1       
ADDR_TRIGGER = 0x15D9BB4 + 0x14EE4C - 0x10

# 포인터 경로
ATTACK_MOTION_PATH = [0x58, 0x10, 0x28, 0x388, 0x5AA]    
MOVE_SPEED_PATH = [0x58, 0x10, 0x28, 0x388, 0x784]       
STEALTH_PATH = [0x58, 0x10, 0x28, 0x388, 0x3A0]         
RADAR_OFF, SELECT_100M_OFF, CHAR_SPEED_OFF = 0xF5, 0xE5, 0x39

kernel32 = ctypes.windll.kernel32

class AionTriggerHelper(QMainWindow):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    update_ui_signal = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.pm, self.base_addr = None, 0
        self.target_pid = None
        self.is_connected = False
        self.is_64bit = True
        self.last_trigger_val = -1
        
        self.init_ui()
        self.load_settings()
        
        self.log_signal.connect(self.append_log)
        self.status_signal.connect(self.update_status_ui)
        self.update_ui_signal.connect(self.sync_ui)
        
        threading.Thread(target=self.control_loop, daemon=True).start()

    def init_ui(self):
        self.setWindowTitle("Aion Helper Pro")
        self.setFixedSize(500, 680)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)

        # 1. 대상 프로세스 선택 (최상단)
        self.btn_select = QPushButton("🎮 대상 프로세스 선택")
        self.btn_select.setMinimumHeight(45)
        self.btn_select.setStyleSheet("font-weight: bold; font-size: 13px; background-color: #EBF5FB;")
        self.btn_select.clicked.connect(self.select_process)
        main_layout.addWidget(self.btn_select)

        # 2. 현재 수동 설정값 저장 (작게 배치)
        save_layout = QHBoxLayout()
        self.btn_save = QPushButton("💾 설정 저장")
        self.btn_save.setFixedSize(120, 30) # 버튼 크기를 작게 고정
        self.btn_save.setStyleSheet("font-size: 11px;")
        self.btn_save.clicked.connect(self.save_settings)
        save_layout.addStretch() # 왼쪽 여백
        save_layout.addWidget(self.btn_save)
        save_layout.addStretch() # 오른쪽 여백
        main_layout.addLayout(save_layout)

        # 3. 메인 기능 박스
        guide_box = QGroupBox("📊 실시간 모니터링 및 자동 제어")
        guide_layout = QGridLayout()
        self.controls = {}

        # 수동 설정 항목
        items = [("공격 모션", "int"), ("이동 속도", "float")]
        for row, (name, dtype) in enumerate(items, 1):
            guide_layout.addWidget(QLabel(name), row, 0)
            cur_view = QLineEdit(); cur_view.setReadOnly(True); cur_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_view.setStyleSheet("background-color: #EBEDEF; font-weight: bold;")
            guide_layout.addWidget(cur_view, row, 1)
            inp = QSpinBox() if dtype == "int" else QDoubleSpinBox()
            if dtype == "int": inp.setRange(0, 65535)
            else: inp.setRange(0.0, 100.0); inp.setSingleStep(0.1)
            inp.setFixedWidth(100)
            guide_layout.addWidget(inp, row, 2)
            self.controls[name] = {"view": cur_view, "input": inp}

        # 모니터링 항목
        mon_items = ["트리거 값", "은신 활성화", "레이더", "케선 속도", "100미터 선택"]
        for row_m, name in enumerate(mon_items, 3):
            guide_layout.addWidget(QLabel(name), row_m, 0)
            cur_view = QLineEdit(); cur_view.setReadOnly(True); cur_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_view.setStyleSheet("background-color: #F4F6F7; font-weight: bold; color: #2E86C1;")
            guide_layout.addWidget(cur_view, row_m, 1)
            if name == "100미터 선택":
                self.check_100m = QCheckBox("체크:110 / 미체크:50")
                guide_layout.addWidget(self.check_100m, row_m, 2)
            else:
                guide_layout.addWidget(QLabel("자동 관리"), row_m, 2)
            self.controls[name] = {"view": cur_view}

        guide_box.setLayout(guide_layout)
        main_layout.addWidget(guide_box)

        # 4. 상태 표시
        self.status_info = QLabel("상태: 프로세스 대기 중")
        self.status_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_info.setStyleSheet("font-weight: bold; padding: 5px;")
        main_layout.addWidget(self.status_info)

        # 5. 로그 박스
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet("background-color: #FBFCFC; font-size: 11px; color: #566573;")
        main_layout.addWidget(self.log_box)

    def select_process(self):
        try:
            procs = [f"aion.bin (PID: {p.info['pid']})" for p in psutil.process_iter(['pid', 'name']) if p.info['name'] and p.info['name'].lower() == PROC_NAME]
            if not procs: 
                self.append_log("❌ 실행 중인 aion.bin을 찾을 수 없습니다.")
                return
            item, ok = QInputDialog.getItem(self, "프로세스 선택", "연결할 PID 선택:", procs, 0, False)
            if ok and item:
                self.target_pid = int(item.split("PID: ")[1].replace(")", ""))
                self.is_connected = False
                self.append_log(f"🎯 PID {self.target_pid} 선택됨")
        except Exception as e: self.append_log(f"❌ 오류: {str(e)}")

    def safe_write(self, addr, value, vtype='float'):
        if not self.pm or not addr: return
        try:
            old = ctypes.c_ulong()
            if kernel32.VirtualProtectEx(self.pm.process_handle, addr, 4, 0x40, ctypes.byref(old)):
                if vtype == 'float': self.pm.write_float(addr, float(value))
                elif vtype == 'short': self.pm.write_short(addr, int(value))
                kernel32.VirtualProtectEx(self.pm.process_handle, addr, 4, old, ctypes.byref(old))
        except: pass

    @pyqtSlot(dict)
    def sync_ui(self, data):
        for name, val in data.items():
            if name in self.controls:
                text = str(val) if isinstance(val, int) else f"{val:.2f}"
                self.controls[name]["view"].setText(text)

    def control_loop(self):
        while True:
            if self.target_pid:
                if not self.is_connected:
                    try:
                        self.pm = pymem.Pymem(); self.pm.open_process_from_id(self.target_pid)
                        self.is_64bit = pymem.process.is_64_bit(self.pm.process_handle)
                        mod = pymem.process.module_from_name(self.pm.process_handle, MOD_NAME)
                        if mod:
                            self.base_addr = mod.lpBaseOfDll; self.is_connected = True
                            self.status_signal.emit(f"● 연결됨 (PID: {self.target_pid}) [{'64bit' if self.is_64bit else '32bit'}]", "#27AE60")
                    except: 
                        self.is_connected = False; self.target_pid = None
                        self.status_signal.emit("○ 연결 실패", "#C0392B")
                else:
                    try:
                        if not psutil.pid_exists(self.pm.process_id): raise Exception()
                        self.execute_logic()
                    except: 
                        self.is_connected = False; self.pm = None; self.target_pid = None
                        self.status_signal.emit("○ 연결 끊김", "#C0392B")
            time.sleep(0.5)

    def execute_logic(self):
        data = {}
        try:
            trigger_val = self.pm.read_int(self.base_addr + ADDR_TRIGGER)
            data["트리거 값"] = trigger_val

            base_ptr = self.get_addr(ATTACK_MOTION_PATH[:-1])
            addr_radar = self.base_addr + BASE_CALC_1 + RADAR_OFF
            addr_char_speed = self.base_addr + BASE_CALC_1 + CHAR_SPEED_OFF
            addr_100m = self.base_addr + BASE_CALC_1 + SELECT_100M_OFF

            if trigger_val == 0:
                if self.last_trigger_val != 0: self.log_signal.emit("🟢 트리거 0: ACTIVE 모드")
                if base_ptr:
                    self.safe_write(base_ptr + ATTACK_MOTION_PATH[-1], self.controls["공격 모션"]["input"].value(), 'short')
                    self.safe_write(base_ptr + MOVE_SPEED_PATH[-1], self.controls["이동 속도"]["input"].value(), 'float')
                    s_ptr = self.get_addr(STEALTH_PATH[:-1])
                    if s_ptr: self.safe_write(s_ptr + STEALTH_PATH[-1], 2560.0)
                self.safe_write(addr_radar, 400211.0)
                self.safe_write(addr_char_speed, 8.0)
                self.safe_write(addr_100m, 110.0 if self.check_100m.isChecked() else 50.0)

            if base_ptr:
                data["공격 모션"] = self.pm.read_short(base_ptr + ATTACK_MOTION_PATH[-1])
                data["이동 속도"] = self.pm.read_float(base_ptr + MOVE_SPEED_PATH[-1])
                s_ptr = self.get_addr(STEALTH_PATH[:-1])
                if s_ptr: data["은신 활성화"] = self.pm.read_float(s_ptr + STEALTH_PATH[-1])
            data["레이더"] = self.pm.read_float(addr_radar)
            data["케선 속도"] = self.pm.read_float(addr_char_speed)
            data["100미터 선택"] = self.pm.read_float(addr_100m)
            self.update_ui_signal.emit(data)
            self.last_trigger_val = trigger_val
        except: pass

    def get_addr(self, offsets):
        try:
            read_func = self.pm.read_longlong if self.is_64bit else self.pm.read_uint
            addr = read_func(self.base_addr + POINTER_BASE)
            for off in offsets: addr = read_func(addr + off)
            return addr
        except: return None

    def save_settings(self):
        try:
            config = {
                "attack_motion": self.controls["공격 모션"]["input"].value(),
                "move_speed": self.controls["이동 속도"]["input"].value(),
                "check_100m": self.check_100m.isChecked()
            }
            with open(CONFIG_PATH, "w") as f: json.dump(config, f)
            self.append_log("💾 설정값이 안전하게 저장되었습니다.")
        except: self.append_log("❌ 저장 중 오류 발생")

    def load_settings(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    d = json.load(f)
                    self.controls["공격 모션"]["input"].setValue(d.get("attack_motion", 0))
                    self.controls["이동 속도"]["input"].setValue(d.get("move_speed", 0.0))
                    self.check_100m.setChecked(d.get("check_100m", False))
            except: pass

    @pyqtSlot(str, str)
    def update_status_ui(self, t, c):
        self.status_info.setText(t); self.status_info.setStyleSheet(f"color: {c}; font-weight: bold;")

    @pyqtSlot(str)
    def append_log(self, m):
        self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {m}")
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)

if __name__ == "__main__":
    app = QApplication(sys.argv); win = AionTriggerHelper(); win.show(); sys.exit(app.exec())