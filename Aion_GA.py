import sys
import os
import json
import pymem
import pymem.process
import threading
import time
import psutil
import ctypes
import requests
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import QTextCursor, QShortcut, QKeySequence, QCursor

# [0. 설정 및 버전]
CURRENT_VERSION = "4.3.6"
UPDATE_URL = "http://your-server-address.com/version.json" # 실제 서버 주소로 수정 필수

# [1. 윈도우 API 및 권한]
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

def set_debug_privilege():
    advapi32 = ctypes.windll.advapi32
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

# [2. 메모리 및 경로 상수]
PAGE_EXECUTE_READWRITE = 0x40 
PROC_NAME = "aion.bin"
MOD_NAME = "Game.dll"
POINTER_BASE = 0x010AF5C8

if getattr(sys, 'frozen', False): BASE_DIR = os.path.dirname(sys.executable)
else: BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config_settings.json")

BASE_CALC_1 = 0x15D9BB4 - 0x613104 - 0xB6400 - 0x1       
ADDR_TRIGGER1 = 0x15D9BB4 + 0x14EE4C - 0x10
ADDR_TRIGGER2 = 0x15D9BB4 - 0x4AE8

ATTACK_MOTION_PATH = [0x58, 0x10, 0x28, 0x388, 0x5AA]
MOVE_SPEED_PATH = [0x58, 0x10, 0x28, 0x388, 0x784]
STEALTH_PATH = [0x58, 0x10, 0x28, 0x388, 0x3A0]          
RADAR_OFF, SELECT_100M_OFF, CHAR_SPEED_OFF = 0xF5, 0xE5, 0x39
ADDR_Z_ORIGIN = 0x15C00E8
Z_CURRENT_PATH = [0x58, 0x10, 0x28, 0x1A0, 0xA8]

GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
VK_F11 = 0x7A

# [3. 단축키 관리 다이얼로그 - 로직 보존]
class HotkeySetDialog(QDialog):
    def __init__(self, title, current_hotkeys, is_int=True):
        super().__init__()
        self.setWindowTitle(f"{title} 단축키 관리")
        self.setMinimumSize(320, 300)
        self.hotkeys_list = current_hotkeys if isinstance(current_hotkeys, list) else []
        self.is_int = is_int
        self.temp_vk, self.temp_key = None, None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for hk in self.hotkeys_list: self.list_widget.addItem(f"키: {hk['key']} | 값: {hk['val']}")
        layout.addWidget(QLabel("등록된 단축키 목록:"))
        layout.addWidget(self.list_widget)

        input_box = QGroupBox("새 단축키 추가")
        input_layout = QGridLayout()
        self.btn_capture = QPushButton("키 입력 대기...")
        self.btn_capture.clicked.connect(self.start_capture)
        self.val_input = QSpinBox() if self.is_int else QDoubleSpinBox()
        if self.is_int: self.val_input.setRange(0, 65535)
        else: self.val_input.setRange(0.0, 100.0); self.val_input.setDecimals(2); self.val_input.setSingleStep(0.01)
        input_layout.addWidget(QLabel("키:"), 0, 0); input_layout.addWidget(self.btn_capture, 0, 1)
        input_layout.addWidget(QLabel("값:"), 1, 0); input_layout.addWidget(self.val_input, 1, 1)
        input_box.setLayout(input_layout); layout.addWidget(input_box)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("단축키 추가"); self.btn_add.clicked.connect(self.add_hotkey)
        self.btn_del = QPushButton("선택 삭제"); self.btn_del.clicked.connect(self.delete_hotkey)
        btn_row.addWidget(self.btn_add); btn_row.addWidget(self.btn_del); layout.addLayout(btn_row)

        self.btn_close = QPushButton("닫기 및 저장"); self.btn_close.clicked.connect(self.accept); layout.addWidget(self.btn_close)
        self.installEventFilter(self)

    def start_capture(self): self.btn_capture.setText("키를 누르세요..."); self.setFocus()
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and self.btn_capture.text() == "키를 누르세요...":
            vk = event.nativeVirtualKey()
            key_text = QKeySequence(event.key()).toString()
            self.temp_key, self.temp_vk = key_text, vk
            self.btn_capture.setText(key_text)
            return True
        return super().eventFilter(obj, event)

    def add_hotkey(self):
        if self.temp_key and self.temp_vk:
            self.hotkeys_list.append({"key": self.temp_key, "vk": self.temp_vk, "val": self.val_input.value()})
            self.list_widget.addItem(f"키: {self.temp_key} | 값: {self.val_input.value()}")
            self.temp_key = None; self.btn_capture.setText("키 입력 대기...")

    def delete_hotkey(self):
        row = self.list_widget.currentRow()
        if row >= 0: self.list_widget.takeItem(row); self.hotkeys_list.pop(row)

class AionTriggerHelper(QMainWindow):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    update_ui_signal = pyqtSignal(dict)
    f11_pressed_signal = pyqtSignal()
    hotkey_action_signal = pyqtSignal(str, float)

    def __init__(self):
        super().__init__()
        self.pm, self.base_addr = None, 0
        self.is_connected, self.is_dragging = False, False
        self.hotkeys_data = {"공격 모션": [], "이동 속도": []}
        
        self.init_ui()
        self.load_settings()
        
        self.log_signal.connect(self.append_log)
        self.status_signal.connect(self.update_status_ui)
        self.update_ui_signal.connect(self.sync_ui)
        self.f11_pressed_signal.connect(self.reset_transparency)
        self.hotkey_action_signal.connect(self.apply_hotkey_value)
        
        threading.Thread(target=self.control_loop, daemon=True).start()
        threading.Thread(target=self.background_key_monitor, daemon=True).start()
        threading.Thread(target=self.check_for_updates, daemon=True).start() # 업데이트 체크
        self.mouse_timer = QTimer(self); self.mouse_timer.timeout.connect(self.check_mouse_position); self.mouse_timer.start(50)

    def init_ui(self):
        self.setWindowTitle(f"Aion Helper Pro v{CURRENT_VERSION}")
        self.resize(550, 950); central_widget = QWidget(); self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget); layout.setContentsMargins(5, 5, 5, 5)

        top = QHBoxLayout()
        self.btn_select = QPushButton("🎮 프로세스 수동 선택"); self.btn_select.setMinimumHeight(45); self.btn_select.clicked.connect(self.select_process)
        self.chk_ontop = QCheckBox("항상 위"); self.chk_ontop.setChecked(True); self.chk_ontop.toggled.connect(self.toggle_always_on_top)
        top.addWidget(self.btn_select, 7); top.addWidget(self.chk_ontop, 3); layout.addLayout(top)

        mon_box = QGroupBox("📊 실시간 데이터 (Z축 4바이트 정수)"); mon_layout = QGridLayout(); self.controls = {}
        items = [("공격 모션", "int"), ("이동 속도", "float")]
        for row, (name, dtype) in enumerate(items):
            mon_layout.addWidget(QLabel(name), row, 0)
            view = QLineEdit(); view.setAlignment(Qt.AlignmentFlag.AlignCenter); view.setStyleSheet("font-weight: bold; color: #E74C3C; background: white;")
            mon_layout.addWidget(view, row, 1)
            inp = QSpinBox() if dtype == "int" else QDoubleSpinBox()
            if dtype == "int": inp.setRange(0, 65535)
            else: inp.setRange(0.0, 100.0); inp.setDecimals(2); inp.setSingleStep(0.01)
            inp.setFixedWidth(80); mon_layout.addWidget(inp, row, 2)
            btn = QPushButton("키등록"); btn.clicked.connect(lambda ch, n=name: self.open_hotkey_dialog(n))
            mon_layout.addWidget(btn, row, 3); self.controls[name] = {"view": view, "input": inp, "type": dtype}

        mon_items = [("트리거1", "#2E86C1"), ("트리거2", "#2E86C1"), ("은신 활성화", "#2E86C1"), ("레이더", "#2E86C1"), ("케선 속도", "#2E86C1"), ("100미터 선택", "#2E86C1"), ("(z축 기존값)", "#0000FF"), ("(z축 현재값)", "#FF0000")]
        for idx, (name, color) in enumerate(mon_items, 2):
            mon_layout.addWidget(QLabel(name), idx, 0)
            v = QLineEdit(); v.setReadOnly(True); v.setAlignment(Qt.AlignmentFlag.AlignCenter); v.setStyleSheet(f"font-weight: bold; color: {color}; background: #F4F6F7;")
            mon_layout.addWidget(v, idx, 1)
            if name == "100미터 선택": self.check_100m = QCheckBox("110m 고정"); mon_layout.addWidget(self.check_100m, idx, 2)
            else: mon_layout.addWidget(QLabel("모니터링"), idx, 2)
            self.controls[name] = {"view": v}
        mon_box.setLayout(mon_layout); layout.addWidget(mon_box)

        self.trans_box = QGroupBox("🌓 투명도 설정"); t_layout = QVBoxLayout()
        self.slider_alpha = QSlider(Qt.Orientation.Horizontal); self.slider_alpha.setRange(30, 255); self.slider_alpha.setValue(255)
        self.slider_alpha.valueChanged.connect(self.update_transparency)
        self.lbl_alpha = QLabel("투명도: 100%"); self.lbl_alpha.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t_layout.addWidget(self.lbl_alpha); t_layout.addWidget(self.slider_alpha); self.trans_box.setLayout(t_layout); layout.addWidget(self.trans_box)
        
        self.btn_save = QPushButton("💾 현재 설정 저장"); self.btn_save.clicked.connect(self.save_settings); layout.addWidget(self.btn_save)
        self.status_info = QLabel("상태: 대기 중..."); self.status_info.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.status_info)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True); self.log_box.setStyleSheet("font-size: 10px;"); layout.addWidget(self.log_box)

    # [핵심] 자동 업데이트 및 재실행 로직
    def check_for_updates(self):
        try:
            r = requests.get(UPDATE_URL, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get("version") > CURRENT_VERSION:
                    self.log_signal.emit(f"📢 새 버전 v{data.get('version')} 감지. 업데이트 후 재시작합니다.")
                    self.perform_update(data.get("download_url"))
        except: pass

    def perform_update(self, url):
        try:
            r = requests.get(url, timeout=30)
            new_file = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
            temp_file = new_file + ".new"
            with open(temp_file, "wb") as f: f.write(r.content)

            # 재실행 배치 파일 생성
            bat_path = os.path.join(BASE_DIR, "update.bat")
            with open(bat_path, "w") as f:
                f.write(f'@echo off\n')
                f.write(f'taskkill /F /PID {os.getpid()} >nul 2>&1\n') # 현재 프로세스 종료 대기
                f.write(f'timeout /t 1 /nobreak >nul\n')
                f.write(f'move /y "{temp_file}" "{new_file}"\n') # 파일 교체
                f.write(f'start "" "{new_file}"\n') # 새 버전 실행
                f.write(f'del "%~f0"\n') # 자기 자신(bat) 삭제

            subprocess.Popen([bat_path], shell=True)
            QApplication.quit()
        except Exception as e:
            self.log_signal.emit(f"❌ 업데이트 실패: {str(e)}")

    def apply_hotkey_value(self, name, value):
        try:
            if self.controls[name]["type"] == "int": self.controls[name]["input"].setValue(int(value))
            else: self.controls[name]["input"].setValue(float(value))
        except: pass

    def background_key_monitor(self):
        while True:
            if user32.GetAsyncKeyState(VK_F11) & 0x8000: self.f11_pressed_signal.emit(); time.sleep(0.3)
            if self.is_connected:
                for name, hks in self.hotkeys_data.items():
                    for hk in hks:
                        if hk["vk"] and (user32.GetAsyncKeyState(hk["vk"]) & 0x8000):
                            self.hotkey_action_signal.emit(name, float(hk["val"])); time.sleep(0.3)
            time.sleep(0.01)

    def execute_logic(self):
        try:
            t1, t2 = self.pm.read_int(self.base_addr+ADDR_TRIGGER1), self.pm.read_int(self.base_addr+ADDR_TRIGGER2)
            addr_m, addr_s = self.get_direct_addr(ATTACK_MOTION_PATH), self.get_direct_addr(MOVE_SPEED_PATH)
            if t1 == 0 and t2 == 0:
                self.force_write_rwx(addr_m, self.controls["공격 모션"]["input"].value(), 'short')
                self.force_write_rwx(addr_s, self.controls["이동 속도"]["input"].value(), 'float')
                s_ptr = self.get_direct_addr(STEALTH_PATH)
                if s_ptr: self.force_write_rwx(s_ptr, 2560.0, 'float')
                self.force_write_rwx(self.base_addr+BASE_CALC_1+RADAR_OFF, 400211.0, 'float')
                self.force_write_rwx(self.base_addr+BASE_CALC_1+CHAR_SPEED_OFF, 8.0, 'float')
                self.force_write_rwx(self.base_addr+BASE_CALC_1+SELECT_100M_OFF, 110.0 if self.check_100m.isChecked() else 50.0, 'float')
            data = {"트리거1": t1, "트리거2": t2, "공격 모션": self.pm.read_short(addr_m), "이동 속도": self.pm.read_float(addr_s), "레이더": self.pm.read_float(self.base_addr+BASE_CALC_1+RADAR_OFF), "케선 속도": self.pm.read_float(self.base_addr+BASE_CALC_1+CHAR_SPEED_OFF), "100미터 선택": self.pm.read_float(self.base_addr+BASE_CALC_1+SELECT_100M_OFF), "(z축 기존값)": self.pm.read_int(self.base_addr+ADDR_Z_ORIGIN), "(z축 현재값)": self.pm.read_int(self.get_direct_addr(Z_CURRENT_PATH)) if self.get_direct_addr(Z_CURRENT_PATH) else 0}
            s_ptr = self.get_direct_addr(STEALTH_PATH); data["은신 활성화"] = self.pm.read_float(s_ptr) if s_ptr else 0.0
            self.update_ui_signal.emit(data); return True
        except: return False

    def check_mouse_position(self):
        if not self.is_dragging:
            pos = self.mapFromGlobal(QCursor.pos())
            enabled = not self.trans_box.geometry().contains(pos) and self.slider_alpha.value() < 230
            hwnd = int(self.winId()); style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT)

    def update_transparency(self, v): self.setWindowOpacity(v/255.0); self.lbl_alpha.setText(f"투명도: {int(v/255*100)}%")
    def reset_transparency(self): self.slider_alpha.setValue(255)
    def control_loop(self):
        while True:
            if not self.is_connected:
                pid = next((p.info['pid'] for p in psutil.process_iter(['pid', 'name']) if p.info['name'] and p.info['name'].lower() == PROC_NAME), None)
                if pid:
                    try:
                        self.pm = pymem.Pymem(); self.pm.open_process_from_id(pid)
                        mod = pymem.process.module_from_name(self.pm.process_handle, MOD_NAME)
                        if mod: self.base_addr = mod.lpBaseOfDll; self.is_connected = True; self.status_signal.emit(f"● 연결됨: {pid}", "#27AE60")
                    except: pass
                time.sleep(1.0)
            else:
                try: 
                    if not self.execute_logic(): self.is_connected = False
                except: self.is_connected = False
                time.sleep(0.1)

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
        cfg = {"attack": self.controls["공격 모션"]["input"].value(), "speed": self.controls["이동 속도"]["input"].value(), "hotkeys_data": self.hotkeys_data, "check_100m": self.check_100m.isChecked(), "alpha": self.slider_alpha.value()}
        with open(CONFIG_PATH, "w") as f: json.dump(cfg, f); self.append_log("💾 저장됨")

    def load_settings(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    d = json.load(f); self.controls["공격 모션"]["input"].setValue(d.get("attack", 0)); self.controls["이동 속도"]["input"].setValue(d.get("speed", 0.0))
                    self.hotkeys_data = d.get("hotkeys_data", self.hotkeys_data); self.check_100m.setChecked(d.get("check_100m", False)); self.slider_alpha.setValue(d.get("alpha", 255))
            except: pass

    def toggle_always_on_top(self, s): self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint if s else self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint); self.show()

    def select_process(self):
        procs = [f"aion (PID: {p.info['pid']})" for p in psutil.process_iter(['pid', 'name']) if p.info['name'] and p.info['name'].lower() == PROC_NAME]
        if procs:
            i, ok = QInputDialog.getItem(self, "선택", "대상 PID:", procs, 0, False)
            if ok and i: self.target_pid = int(i.split("PID: ")[1].replace(")", "")); self.is_connected = False

    def open_hotkey_dialog(self, name):
        dlg = HotkeySetDialog(name, self.hotkeys_data[name], (self.controls[name]["type"]=="int"))
        if dlg.exec(): self.hotkeys_data[name] = dlg.hotkeys_list; self.append_log(f"✅ {name} 단축키 저장됨")

    @pyqtSlot(dict)
    def sync_ui(self, d):
        for k, v in d.items():
            if k in self.controls and not self.controls[k]["view"].hasFocus(): self.controls[k]["view"].setText(str(v) if isinstance(v, (int, str)) else f"{v:.2f}")

    @pyqtSlot(str, str)
    def update_status_ui(self, t, c): self.status_info.setText(t); self.status_info.setStyleSheet(f"color: {c}; font-weight: bold;")
    @pyqtSlot(str)
    def append_log(self, m): self.log_box.append(f"[{time.strftime('%H:%M:%S')}] {m}"); self.log_box.moveCursor(QTextCursor.MoveOperation.End)

if __name__ == "__main__":
    if not is_admin(): ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    else: set_debug_privilege(); app = QApplication(sys.argv); win = AionTriggerHelper(); win.show(); sys.exit(app.exec())