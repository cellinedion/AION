import sys
import os
import json
import pymem
import pymem.process
import threading
import time
import psutil
import ctypes
import subprocess
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import QTextCursor

# [1. 윈도우 보안 및 권한 설정]
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

def set_debug_privilege():
    h_token = ctypes.c_void_p()
    if advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(h_token)):
        luid = ctypes.create_string_buffer(8)
        if advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", luid):
            tp = ctypes.create_string_buffer(16)
            ctypes.memmove(tp, b'\x01\x00\x00\x00', 4)
            ctypes.memmove(ctypes.addressof(tp)+4, luid, 8)
            ctypes.memmove(ctypes.addressof(tp)+12, b'\x02\x00\x00\x00', 4)
            advapi32.AdjustTokenPrivileges(h_token, False, tp, 0, None, None)
        kernel32.CloseHandle(h_token)

def is_admin():
    try: return ctypes.windll.shell32.IsUserAnAdmin()
    except: return False

if not is_admin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit()

set_debug_privilege()

# [2. 메모리 및 프로세스 설정 상수]
PAGE_EXECUTE_READWRITE = 0x40 
PROC_NAME = "aion.bin"
MOD_NAME = "Game.dll"
POINTER_BASE = 0x010AF5C8 

if getattr(sys, 'frozen', False): BASE_DIR = os.path.dirname(sys.executable)
else: BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config_settings.json")
BASE_CALC_1 = 0x15D9BB4 - 0x613104 - 0xB6400 - 0x1       
ADDR_TRIGGER = 0x15D9BB4 + 0x14EE4C - 0x10

ATTACK_MOTION_PATH = [0x58, 0x10, 0x28, 0x388, 0x5AA]    
MOVE_SPEED_PATH = [0x58, 0x10, 0x28, 0x388, 0x784]       
STEALTH_PATH = [0x58, 0x10, 0x28, 0x388, 0x3A0]          
RADAR_OFF, SELECT_100M_OFF, CHAR_SPEED_OFF = 0xF5, 0xE5, 0x39

# Z축 주소 설정
ADDR_Z_ORIGIN = 0x15C00E8
Z_CURRENT_PATH = [0x58, 0x10, 0x28, 0x1A0, 0xA8]

class AionTriggerHelper(QMainWindow):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    update_ui_signal = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.pm, self.base_addr = None, 0
        self.target_pid = None
        self.is_connected = False
        
        self.init_ui()
        self.load_settings()
        
        self.log_signal.connect(self.append_log)
        self.status_signal.connect(self.update_status_ui)
        self.update_ui_signal.connect(self.sync_ui)
        
        threading.Thread(target=self.control_loop, daemon=True).start()

    def init_ui(self):
        self.setWindowTitle("Aion Helper - Standalone Pro")
        self.resize(550, 850)
        self.setMinimumSize(450, 700)
        
        central_widget = QWidget(); self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        top_bar = QHBoxLayout()
        self.btn_select = QPushButton("🎮 대상 프로세스 수동 선택")
        self.btn_select.setMinimumHeight(40)
        self.btn_select.setStyleSheet("font-weight: bold; background-color: #EBF5FB;")
        self.btn_select.clicked.connect(self.select_process)
        
        self.chk_ontop = QCheckBox("항상 위")
        self.chk_ontop.setChecked(True)
        self.chk_ontop.toggled.connect(self.toggle_always_on_top)
        self.toggle_always_on_top(True)
        
        top_bar.addWidget(self.btn_select, 7)
        top_bar.addWidget(self.chk_ontop, 3)
        layout.addLayout(top_bar)

        mon_box = QGroupBox("📊 실시간 데이터 (Z축 4바이트 정수)")
        mon_layout = QGridLayout(); self.controls = {}

        items = [("공격 모션", "int"), ("이동 속도", "float")]
        for row, (name, dtype) in enumerate(items, 1):
            mon_layout.addWidget(QLabel(name), row, 0)
            cur_view = QLineEdit()
            cur_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_view.setStyleSheet("font-weight: bold; color: #E74C3C; background-color: white;")
            cur_view.returnPressed.connect(lambda n=name: self.direct_manual_inject(n))
            mon_layout.addWidget(cur_view, row, 1)
            
            inp = QSpinBox() if dtype == "int" else QDoubleSpinBox()
            if dtype == "int": inp.setRange(0, 65535)
            else: inp.setRange(0.0, 100.0); inp.setSingleStep(0.1)
            inp.setFixedWidth(80); mon_layout.addWidget(inp, row, 2)
            
            chk = QCheckBox("강제 주입")
            chk.setStyleSheet("color: #C0392B; font-weight: bold;")
            mon_layout.addWidget(chk, row, 3)
            self.controls[name] = {"view": cur_view, "input": inp, "freeze": chk, "type": dtype}

        # 모니터링 리스트
        mon_items = [
            ("트리거 값", "#2E86C1"), 
            ("은신 활성화", "#2E86C1"), 
            ("레이더", "#2E86C1"), 
            ("케선 속도", "#2E86C1"), 
            ("100미터 선택", "#2E86C1"),
            ("(z축 기존값)", "#0000FF"), # 파란색
            ("(z축 현재값)", "#FF0000")  # 빨간색
        ]
        
        for idx, (name, color) in enumerate(mon_items):
            row_m = idx + 3
            mon_layout.addWidget(QLabel(name), row_m, 0)
            cur_view = QLineEdit(); cur_view.setReadOnly(True); cur_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_view.setStyleSheet(f"background-color: #F4F6F7; font-weight: bold; color: {color};")
            mon_layout.addWidget(cur_view, row_m, 1)
            
            if name == "100미터 선택":
                self.check_100m = QCheckBox("110m 고정"); mon_layout.addWidget(self.check_100m, row_m, 2)
            else:
                mon_layout.addWidget(QLabel("모니터링"), row_m, 2)
            self.controls[name] = {"view": cur_view}

        mon_box.setLayout(mon_layout); layout.addWidget(mon_box)
        
        self.btn_save = QPushButton("💾 현재 설정 저장"); self.btn_save.clicked.connect(self.save_settings)
        layout.addWidget(self.btn_save)
        
        self.status_info = QLabel("상태: 연결 대기 중..."); self.status_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_info)
        
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True); self.log_box.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.log_box)

    def toggle_always_on_top(self, state):
        if state: self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        else: self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def select_process(self):
        try:
            procs = [f"aion.bin (PID: {p.info['pid']})" for p in psutil.process_iter(['pid', 'name']) if p.info['name'] and p.info['name'].lower() == PROC_NAME]
            if not procs: return
            item, ok = QInputDialog.getItem(self, "선택", "PID 선택:", procs, 0, False)
            if ok and item:
                self.target_pid = int(item.split("PID: ")[1].replace(")", ""))
                self.is_connected = False
        except: pass

    def direct_manual_inject(self, name):
        if not self.is_connected: return
        try:
            val = float(self.controls[name]["view"].text())
            path = ATTACK_MOTION_PATH if name == "공격 모션" else MOVE_SPEED_PATH
            vtype = 'short' if name == "공격 모션" else 'float'
            addr = self.get_direct_addr(path)
            if addr:
                self.force_write_rwx(addr, val, vtype)
                self.append_log(f"⚡ [수동 주입] {name} -> {val} 완료")
        except Exception as e: self.append_log(f"⚠️ 입력 오류: {e}")

    def control_loop(self):
        while True:
            if not self.is_connected:
                found_pid = self.target_pid if self.target_pid and psutil.pid_exists(self.target_pid) else None
                if not found_pid:
                    for p in psutil.process_iter(['pid', 'name']):
                        if p.info['name'] and p.info['name'].lower() == PROC_NAME: found_pid = p.info['pid']; break
                if found_pid:
                    try:
                        self.pm = pymem.Pymem(); self.pm.open_process_from_id(found_pid)
                        mod = pymem.process.module_from_name(self.pm.process_handle, MOD_NAME)
                        if mod:
                            self.base_addr = mod.lpBaseOfDll; self.is_connected = True
                            self.status_signal.emit(f"● 연결됨 (PID: {found_pid})", "#27AE60")
                    except: self.is_connected = False
                time.sleep(1.0)
            else:
                try:
                    if not self.execute_logic(): self.is_connected = False
                except: self.is_connected = False
                time.sleep(0.05)

    def execute_logic(self):
        try:
            trigger_val = self.pm.read_int(self.base_addr + ADDR_TRIGGER)
            addr_motion = self.get_direct_addr(ATTACK_MOTION_PATH)
            addr_speed = self.get_direct_addr(MOVE_SPEED_PATH)
            if not addr_motion or not addr_speed: return False 

            if trigger_val == 0 or self.controls["공격 모션"]["freeze"].isChecked():
                self.force_write_rwx(addr_motion, self.controls["공격 모션"]["input"].value(), 'short')
            
            if trigger_val == 0 or self.controls["이동 속도"]["freeze"].isChecked():
                self.force_write_rwx(addr_speed, self.controls["이동 속도"]["input"].value(), 'float')

            if trigger_val == 0:
                s_ptr = self.get_direct_addr(STEALTH_PATH)
                if s_ptr: self.force_write_rwx(s_ptr, 2560.0, 'float')
                self.force_write_rwx(self.base_addr + BASE_CALC_1 + RADAR_OFF, 400211.0, 'float')
                self.force_write_rwx(self.base_addr + BASE_CALC_1 + CHAR_SPEED_OFF, 8.0, 'float')
                self.force_write_rwx(self.base_addr + BASE_CALC_1 + SELECT_100M_OFF, 110.0 if self.check_100m.isChecked() else 50.0, 'float')

            # 데이터 수집
            data = {
                "트리거 값": trigger_val,
                "공격 모션": self.pm.read_short(addr_motion),
                "이동 속도": self.pm.read_float(addr_speed),
                "레이더": self.pm.read_float(self.base_addr + BASE_CALC_1 + RADAR_OFF),
                "케선 속도": self.pm.read_float(self.base_addr + BASE_CALC_1 + CHAR_SPEED_OFF),
                "100미터 선택": self.pm.read_float(self.base_addr + BASE_CALC_1 + SELECT_100M_OFF),
                # Z축 기존값 - 4바이트 정수(Int)로 읽기
                "(z축 기존값)": self.pm.read_int(self.base_addr + ADDR_Z_ORIGIN) 
            }
            
            # Z축 현재값 - 4바이트 정수(Int)로 읽기
            z_curr_addr = self.get_direct_addr(Z_CURRENT_PATH)
            data["(z축 현재값)"] = self.pm.read_int(z_curr_addr) if z_curr_addr else 0
            
            s_ptr = self.get_direct_addr(STEALTH_PATH)
            data["은신 활성화"] = self.pm.read_float(s_ptr) if s_ptr else "N/A"
            
            self.update_ui_signal.emit(data)
            return True
        except: return False

    def get_direct_addr(self, path_offsets):
        try:
            addr = self.pm.read_longlong(self.base_addr + POINTER_BASE)
            if addr == 0: return None
            for i in range(len(path_offsets) - 1):
                addr = self.pm.read_longlong(addr + path_offsets[i])
                if addr == 0: return None
            return addr + path_offsets[-1]
        except: return None

    def force_write_rwx(self, addr, value, vtype='float'):
        if not self.pm or not addr: return
        try:
            target_addr = int(addr)
            size = 4 if vtype in ['float', 'int'] else 2
            old_p = ctypes.c_ulong()
            if kernel32.VirtualProtectEx(self.pm.process_handle, target_addr, size, PAGE_EXECUTE_READWRITE, ctypes.byref(old_p)):
                if vtype == 'float': self.pm.write_float(target_addr, float(value))
                elif vtype == 'short': self.pm.write_short(target_addr, int(value))
                elif vtype == 'int': self.pm.write_int(target_addr, int(value))
                kernel32.VirtualProtectEx(self.pm.process_handle, target_addr, size, old_p, ctypes.byref(old_p))
        except: pass

    def save_settings(self):
        try:
            config = {
                "attack_motion": self.controls["공격 모션"]["input"].value(),
                "move_speed": self.controls["이동 속도"]["input"].value(),
                "motion_freeze": self.controls["공격 모션"]["freeze"].isChecked(),
                "speed_freeze": self.controls["이동 속도"]["freeze"].isChecked(),
                "check_100m": self.check_100m.isChecked(),
                "ontop": self.chk_ontop.isChecked()
            }
            with open(CONFIG_PATH, "w") as f: json.dump(config, f)
            self.append_log("💾 설정 저장됨")
        except: pass

    def load_settings(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    d = json.load(f)
                    self.controls["공격 모션"]["input"].setValue(d.get("attack_motion", 0))
                    self.controls["이동 속도"]["input"].setValue(d.get("move_speed", 0.0))
                    self.controls["공격 모션"]["freeze"].setChecked(d.get("motion_freeze", False))
                    self.controls["이동 속도"]["freeze"].setChecked(d.get("speed_freeze", False))
                    self.check_100m.setChecked(d.get("check_100m", False))
                    self.chk_ontop.setChecked(d.get("ontop", True))
                    self.toggle_always_on_top(self.chk_ontop.isChecked())
            except: pass

    @pyqtSlot(dict)
    def sync_ui(self, data):
        for name, val in data.items():
            if name in self.controls:
                if not self.controls[name]["view"].hasFocus():
                    # 4바이트 정수형은 소수점 없이 정수로 표시
                    if name in ["(z축 기존값)", "(z축 현재값)"]:
                        txt = str(val)
                    else:
                        txt = str(val) if isinstance(val, (int, str)) else f"{val:.2f}"
                    self.controls[name]["view"].setText(txt)

    @pyqtSlot(str, str)
    def update_status_ui(self, t, c): self.status_info.setText(t); self.status_info.setStyleSheet(f"color: {c}; font-weight: bold;")
    @pyqtSlot(str)
    def append_log(self, m): self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {m}"); self.log_box.moveCursor(QTextCursor.MoveOperation.End)

if __name__ == "__main__":
    app = QApplication(sys.argv); win = AionTriggerHelper(); win.show(); sys.exit(app.exec())