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
from PyQt6.QtGui import QTextCursor, QShortcut, QKeySequence, QCursor

# [1. 윈도우 보안 및 권한 설정]
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
user32 = ctypes.windll.user32

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

# [2. 메모리 및 프로세스 설정 상수 - 100% 복구]
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

ADDR_Z_ORIGIN = 0x15C00E8
Z_CURRENT_PATH = [0x58, 0x10, 0x28, 0x1A0, 0xA8]

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
VK_F11 = 0x7A

class AionTriggerHelper(QMainWindow):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    update_ui_signal = pyqtSignal(dict)
    f11_pressed_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.pm, self.base_addr = None, 0
        self.target_pid = None
        self.is_connected = False
        self.is_dragging = False 
        
        self.init_ui()
        self.load_settings()
        
        self.log_signal.connect(self.append_log)
        self.status_signal.connect(self.update_status_ui)
        self.update_ui_signal.connect(self.sync_ui)
        self.f11_pressed_signal.connect(self.reset_transparency)
        
        # 기본 제어 루프
        threading.Thread(target=self.control_loop, daemon=True).start()
        # [추가] 2초 주기 강제 주입 전 전 전 전 루프
        threading.Thread(target=self.freeze_loop, daemon=True).start()
        # F11 백그라운드 감시
        threading.Thread(target=self.background_key_monitor, daemon=True).start()

        # 마우스 위치 감시 타이머 (슬라이더 영역 보호용)
        self.mouse_timer = QTimer(self)
        self.mouse_timer.timeout.connect(self.check_mouse_position)
        self.mouse_timer.start(50)

    def init_ui(self):
        self.setWindowTitle("Aion Helper - 2s Force Sync Pro")
        self.resize(550, 920)
        self.setMinimumSize(320, 500)
        
        central_widget = QWidget(); self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(7, 7, 7, 7)

        # 상단 바
        top_bar = QHBoxLayout()
        self.btn_select = QPushButton("🎮 프로세스 수동 선택"); self.btn_select.setMinimumHeight(45)
        self.btn_select.setStyleSheet("font-weight: bold; background-color: #EBF5FB;")
        self.btn_select.clicked.connect(self.select_process)
        self.chk_ontop = QCheckBox("항상 위"); self.chk_ontop.setChecked(True)
        self.chk_ontop.toggled.connect(self.toggle_always_on_top)
        top_bar.addWidget(self.btn_select, 7); top_bar.addWidget(self.chk_ontop, 3)
        layout.addLayout(top_bar)

        # 모니터링 및 주입 설정
        mon_box = QGroupBox("📊 실시간 데이터 (Z축 4바이트 정수)")
        mon_layout = QGridLayout(); self.controls = {}
        
        items = [("공격 모션", "int"), ("이동 속도", "float")]
        for row, (name, dtype) in enumerate(items, 1):
            mon_layout.addWidget(QLabel(name), row, 0)
            cur_view = QLineEdit(); cur_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_view.setStyleSheet("font-weight: bold; color: #E74C3C; background-color: white;")
            cur_view.returnPressed.connect(lambda n=name: self.direct_manual_inject(n))
            mon_layout.addWidget(cur_view, row, 1)
            inp = QSpinBox() if dtype == "int" else QDoubleSpinBox()
            if dtype == "int": inp.setRange(0, 65535)
            else: inp.setRange(0.0, 100.0); inp.setSingleStep(0.1)
            inp.setFixedWidth(75); mon_layout.addWidget(inp, row, 2)
            chk = QCheckBox("강제"); chk.setStyleSheet("color: #C0392B; font-weight: bold;")
            mon_layout.addWidget(chk, row, 3); self.controls[name] = {"view": cur_view, "input": inp, "freeze": chk, "type": dtype}

        mon_items = [
            ("트리거 값", "#2E86C1"), ("은신 활성화", "#2E86C1"), ("레이더", "#2E86C1"), 
            ("케선 속도", "#2E86C1"), ("100미터 선택", "#2E86C1"),
            ("(z축 기존값)", "#0000FF"), ("(z축 현재값)", "#FF0000")
        ]
        for idx, (name, color) in enumerate(mon_items):
            row_m = idx + 3; mon_layout.addWidget(QLabel(name), row_m, 0)
            cur_view = QLineEdit(); cur_view.setReadOnly(True); cur_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_view.setStyleSheet(f"background-color: #F4F6F7; font-weight: bold; color: {color};")
            mon_layout.addWidget(cur_view, row_m, 1)
            if name == "100미터 선택": 
                self.check_100m = QCheckBox("110m 고정"); mon_layout.addWidget(self.check_100m, row_m, 2)
            else:
                mon_layout.addWidget(QLabel("모니터링"), row_m, 2)
            self.controls[name] = {"view": cur_view}
        
        mon_box.setLayout(mon_layout); layout.addWidget(mon_box)

        # 투명도 조절
        self.trans_box = QGroupBox("🌓 투명도 설정 (슬라이더 항시 조작 가능)")
        trans_layout = QVBoxLayout()
        self.slider_alpha = QSlider(Qt.Orientation.Horizontal)
        self.slider_alpha.setRange(30, 255); self.slider_alpha.setValue(255)
        self.slider_alpha.sliderPressed.connect(self.on_slider_pressed)
        self.slider_alpha.sliderReleased.connect(self.on_slider_released)
        self.slider_alpha.valueChanged.connect(self.update_transparency)
        self.lbl_alpha = QLabel("투명도: 100%"); self.lbl_alpha.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trans_layout.addWidget(self.lbl_alpha); trans_layout.addWidget(self.slider_alpha)
        self.trans_box.setLayout(trans_layout); layout.addWidget(self.trans_box)
        
        self.btn_save = QPushButton("💾 현재 설정 저장"); self.btn_save.clicked.connect(self.save_settings); layout.addWidget(self.btn_save)
        self.status_info = QLabel("상태: 대기 중..."); self.status_info.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.status_info)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True); self.log_box.setStyleSheet("font-size: 10px;"); layout.addWidget(self.log_box)

    # [핵심] 2초마다 강제 체크박스 항목 쓰기/읽기 수행
    def freeze_loop(self):
        while True:
            if self.is_connected and self.pm:
                try:
                    for name in ["공격 모션", "이동 속도"]:
                        if self.controls[name]["freeze"].isChecked():
                            val = self.controls[name]["input"].value()
                            path = ATTACK_MOTION_PATH if name == "공격 모션" else MOVE_SPEED_PATH
                            addr = self.get_direct_addr(path)
                            if addr:
                                vtype = 'short' if name == "공격 모션" else 'float'
                                # 1. 쓰기
                                self.force_write_rwx(addr, val, vtype)
                                # 2. 즉시 다시 읽어서 UI에 확인 (읽기)
                                current_val = self.pm.read_short(addr) if vtype == 'short' else self.pm.read_float(addr)
                                self.log_signal.emit(f"🔄 [2초 강제동기화] {name}: {current_val}")
                except:
                    pass
            time.sleep(2.0)

    def on_slider_pressed(self): self.is_dragging = True; self.set_click_through(False)
    def on_slider_released(self): self.is_dragging = False; self.update_transparency(self.slider_alpha.value())

    def check_mouse_position(self):
        if self.is_dragging: return
        cursor_pos = self.mapFromGlobal(QCursor.pos())
        if self.trans_box.geometry().contains(cursor_pos):
            self.set_click_through(False)
        elif self.slider_alpha.value() < 230:
            self.set_click_through(True)

    def set_click_through(self, enabled):
        hwnd = int(self.winId())
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled: user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        else: user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)

    def update_transparency(self, value):
        alpha = value / 255.0
        self.setWindowOpacity(alpha)
        if not self.is_dragging:
            self.lbl_alpha.setText(f"투명도: {int(alpha*100)}% {'(통과 중)' if value < 230 else ''}")

    def background_key_monitor(self):
        while True:
            if user32.GetAsyncKeyState(VK_F11) & 0x8000: self.f11_pressed_signal.emit(); time.sleep(0.3)
            time.sleep(0.1)

    def reset_transparency(self): self.slider_alpha.setValue(255); self.set_click_through(False)

    def select_process(self):
        procs = [f"aion (PID: {p.info['pid']})" for p in psutil.process_iter(['pid', 'name']) if p.info['name'] and p.info['name'].lower() == PROC_NAME]
        if procs:
            item, ok = QInputDialog.getItem(self, "선택", "대상 PID:", procs, 0, False)
            if ok and item: self.target_pid = int(item.split("PID: ")[1].replace(")", "")); self.is_connected = False

    def direct_manual_inject(self, name):
        if not self.is_connected: return
        try:
            val = float(self.controls[name]["view"].text())
            path = ATTACK_MOTION_PATH if name == "공격 모션" else MOVE_SPEED_PATH
            addr = self.get_direct_addr(path)
            if addr: self.force_write_rwx(addr, val, 'short' if name == "공격 모션" else 'float')
        except: pass

    def control_loop(self):
        while True:
            if not self.is_connected:
                pid = self.target_pid if self.target_pid and psutil.pid_exists(self.target_pid) else next((p.info['pid'] for p in psutil.process_iter(['pid', 'name']) if p.info['name'] and p.info['name'].lower() == PROC_NAME), None)
                if pid:
                    try:
                        self.pm = pymem.Pymem(); self.pm.open_process_from_id(pid)
                        mod = pymem.process.module_from_name(self.pm.process_handle, MOD_NAME)
                        if mod: self.base_addr = mod.lpBaseOfDll; self.is_connected = True; self.status_signal.emit(f"● 연결됨: {pid}", "#27AE60")
                    except: self.is_connected = False
                time.sleep(1.0)
            else:
                try: 
                    if not self.execute_logic(): self.is_connected = False
                except: self.is_connected = False
                time.sleep(0.1) # 기본 루프는 0.1초

    def execute_logic(self):
        try:
            trigger = self.pm.read_int(self.base_addr + ADDR_TRIGGER)
            addr_m = self.get_direct_addr(ATTACK_MOTION_PATH)
            addr_s = self.get_direct_addr(MOVE_SPEED_PATH)
            if not addr_m or not addr_s: return False 

            # 트리거 0일 때 (자동 모드) 주입 로직
            if trigger == 0:
                self.force_write_rwx(addr_m, self.controls["공격 모션"]["input"].value(), 'short')
                self.force_write_rwx(addr_s, self.controls["이동 속도"]["input"].value(), 'float')
                s_ptr = self.get_direct_addr(STEALTH_PATH)
                if s_ptr: self.force_write_rwx(s_ptr, 2560.0, 'float')
                self.force_write_rwx(self.base_addr + BASE_CALC_1 + RADAR_OFF, 400211.0, 'float')
                self.force_write_rwx(self.base_addr + BASE_CALC_1 + CHAR_SPEED_OFF, 8.0, 'float')
                m100_val = 110.0 if self.check_100m.isChecked() else 50.0
                self.force_write_rwx(self.base_addr + BASE_CALC_1 + SELECT_100M_OFF, m100_val, 'float')

            # 모니터링 데이터 갱신
            data = {
                "트리거 값": trigger, 
                "공격 모션": self.pm.read_short(addr_m), 
                "이동 속도": self.pm.read_float(addr_s),
                "레이더": self.pm.read_float(self.base_addr + BASE_CALC_1 + RADAR_OFF),
                "케선 속도": self.pm.read_float(self.base_addr + BASE_CALC_1 + CHAR_SPEED_OFF),
                "100미터 선택": self.pm.read_float(self.base_addr + BASE_CALC_1 + SELECT_100M_OFF),
                "(z축 기존값)": self.pm.read_int(self.base_addr + ADDR_Z_ORIGIN), 
                "(z축 현재값)": self.pm.read_int(self.get_direct_addr(Z_CURRENT_PATH)) if self.get_direct_addr(Z_CURRENT_PATH) else 0
            }
            s_ptr = self.get_direct_addr(STEALTH_PATH)
            data["은신 활성화"] = self.pm.read_float(s_ptr) if s_ptr else "N/A"
            
            self.update_ui_signal.emit(data); return True
        except: return False

    def get_direct_addr(self, p):
        try:
            addr = self.pm.read_longlong(self.base_addr + POINTER_BASE)
            for i in range(len(p)-1):
                addr = self.pm.read_longlong(addr + p[i])
                if addr == 0: return None
            return addr + p[-1]
        except: return None

    def force_write_rwx(self, a, v, t):
        try:
            old = ctypes.c_ulong()
            if kernel32.VirtualProtectEx(self.pm.process_handle, int(a), 4, PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
                if t == 'float': self.pm.write_float(int(a), float(v))
                elif t == 'short': self.pm.write_short(int(a), int(v))
                kernel32.VirtualProtectEx(self.pm.process_handle, int(a), 4, old, ctypes.byref(old))
        except: pass

    def save_settings(self):
        cfg = {"attack": self.controls["공격 모션"]["input"].value(), "speed": self.controls["이동 속도"]["input"].value(), 
               "motion_freeze": self.controls["공격 모션"]["freeze"].isChecked(), "speed_freeze": self.controls["이동 속도"]["freeze"].isChecked(),
               "check_100m": self.check_100m.isChecked(), "alpha": self.slider_alpha.value()}
        with open(CONFIG_PATH, "w") as f: json.dump(cfg, f); self.append_log("💾 설정 및 주입 상태 저장됨")

    def load_settings(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    d = json.load(f)
                    self.controls["공격 모션"]["input"].setValue(d.get("attack", 0))
                    self.controls["이동 속도"]["input"].setValue(d.get("speed", 0.0))
                    self.controls["공격 모션"]["freeze"].setChecked(d.get("motion_freeze", False))
                    self.controls["이동 속도"]["freeze"].setChecked(d.get("speed_freeze", False))
                    self.check_100m.setChecked(d.get("check_100m", False))
                    self.slider_alpha.setValue(d.get("alpha", 255))
            except: pass

    def toggle_always_on_top(self, state):
        if state: self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        else: self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    @pyqtSlot(dict)
    def sync_ui(self, d):
        for k, v in d.items():
            if k in self.controls and not self.controls[k]["view"].hasFocus():
                self.controls[k]["view"].setText(str(v) if isinstance(v, (int, str)) else f"{v:.2f}")

    @pyqtSlot(str, str)
    def update_status_ui(self, t, c): self.status_info.setText(t); self.status_info.setStyleSheet(f"color: {c}; font-weight: bold;")
    @pyqtSlot(str)
    def append_log(self, m): self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {m}"); self.log_box.moveCursor(QTextCursor.MoveOperation.End)

if __name__ == "__main__":
    app = QApplication(sys.argv); win = AionTriggerHelper(); win.show(); sys.exit(app.exec())