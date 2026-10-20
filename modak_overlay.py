"""MODAK Boss Timer 5.0 Lite — server-only, dependency-free Windows overlay.
Build with: pyinstaller --onefile --windowed --name ModakBossTimer modak_overlay.py
"""
import json
import os
import queue
import threading
import time
import urllib.request
import winsound
from pathlib import Path
import tkinter as tk
from tkinter import simpledialog, messagebox

API = 'https://api.modakmmo.com/api/v1/world-timers'
HOME = Path(os.environ.get('APPDATA') or Path.home()) / 'ModakBossOverlay'
CONFIG = HOME / 'config.json'
ZONES = [
    ('그늘숲', 'mistwood'), ('초원', 'meadow'), ('황야', 'wasteland'),
    ('협곡 진입로', 'skoll_path'), ('망자의 묘지', 'graveyard'),
    ('망국의 기록보관소', 'forgotten_castle_archive'),
    ('잊혀진 고성 입구', 'forgotten_castle_entrance'),
    ('망국의 병기창', 'forgotten_castle_arsenal'),
]
BG = '#0B1220'
PANEL = '#131F30'
PANEL2 = '#1B2B40'
FG = '#F1F6FC'
MUTED = '#8CA3BA'
CYAN = '#5DE2D1'
GOLD = '#FFC876'
RED = '#FF7C85'


def fmt(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h:02}:{m:02}:{s:02}' if h else f'{m:02}:{s:02}'


def load_config():
    try:
        obj = json.loads(CONFIG.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


class App:
    def __init__(self, root):
        self.root = root
        self.data = load_config()
        self.client_id = str(self.data.get('client_id') or '').strip()
        self.sound = bool(self.data.get('sound_enabled', True))
        self.alpha = float(self.data.get('alpha', .96))
        self.ends = {}
        self.active = {}
        self.alerted = {}
        self.previous = {}
        self.messages = queue.Queue()
        self.fetching = False
        self.next_fetch = 0
        self.last_success = None
        self.status_message = '서버 연결 대기 중'
        self.root.title('MODAK BOSS TIMER · LITE')
        self.root.configure(bg=BG)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', max(.55, min(1., self.alpha)))
        self.root.geometry(self.data.get('geometry', '355x625+80+80'))
        self.root.minsize(320, 530)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.build()
        # Restore previously synced timestamps from 4.0 config without changing its OCR settings.
        for boss in self.data.get('bosses', []):
            zone = dict(ZONES).get(boss.get('name'))
            if zone and isinstance(boss.get('end'), (int, float)):
                self.ends[zone] = boss['end']
        self.root.after(600, self.loop)
        if not self.client_id:
            self.root.after(900, self.set_client_id)

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        self.data['client_id'] = self.client_id
        self.data['sound_enabled'] = self.sound
        self.data['alpha'] = self.alpha
        self.data['geometry'] = self.root.geometry()
        # Keep all old keys, including OCR regions, so 4.0 can still be restored.
        CONFIG.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def make_button(self, parent, text, cmd, bg=PANEL2, fg=FG):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         activebackground='#30445D', activeforeground=FG,
                         relief='flat', bd=0, padx=10, pady=7,
                         cursor='hand2', font=('Malgun Gothic', 9, 'bold'))

    def build(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill='x', padx=17, pady=(17, 4))
        tk.Label(head, text='MODAK', font=('Segoe UI', 18, 'bold'),
                 fg=FG, bg=BG).pack(side='left')
        tk.Label(head, text='BOSS TIMER', font=('Segoe UI', 9, 'bold'),
                 fg=CYAN, bg=BG).pack(side='left', padx=(9, 0), pady=(8, 0))
        self.make_button(head, '−', self.root.iconify).pack(side='right')
        sub = tk.Frame(self.root, bg=BG)
        sub.pack(fill='x', padx=17, pady=(0, 12))
        self.indicator = tk.Label(sub, text='●', fg=GOLD, bg=BG, font=('Segoe UI', 10))
        self.indicator.pack(side='left')
        self.status = tk.Label(sub, text='서버 연결 대기 중', fg=MUTED, bg=BG,
                               font=('Malgun Gothic', 9), anchor='w')
        self.status.pack(side='left', padx=(6, 0), fill='x', expand=True)
        tk.Label(sub, text='v5 LITE', fg=MUTED, bg=BG,
                 font=('Segoe UI', 9)).pack(side='right')
        actions = tk.Frame(self.root, bg=BG)
        actions.pack(fill='x', padx=15, pady=(0, 10))
        self.make_button(actions, '↻  지금 동기화', self.refresh, bg='#126A70').pack(side='left', padx=(0, 6))
        self.make_button(actions, '서버 ID', self.set_client_id).pack(side='left', padx=(0, 6))
        self.sound_button = self.make_button(actions, '', self.toggle_sound)
        self.sound_button.pack(side='left')
        self.update_sound_button()
        self.list_area = tk.Frame(self.root, bg=BG)
        self.list_area.pack(fill='both', expand=True, padx=15)
        self.rows = {}
        for name, zone in ZONES:
            panel = tk.Frame(self.list_area, bg=PANEL, height=48)
            panel.pack(fill='x', pady=3)
            panel.pack_propagate(False)
            left = tk.Frame(panel, bg=PANEL)
            left.pack(side='left', fill='both', expand=True, padx=(13, 0))
            tk.Label(left, text=name, fg=FG, bg=PANEL,
                     font=('Malgun Gothic', 10, 'bold'), anchor='w').pack(anchor='w', pady=(5, 0))
            state = tk.Label(left, text='동기화 대기', fg=MUTED, bg=PANEL,
                             font=('Malgun Gothic', 8), anchor='w')
            state.pack(anchor='w')
            clock = tk.Label(panel, text='--:--', fg=MUTED, bg=PANEL,
                             font=('Consolas', 15, 'bold'))
            clock.pack(side='right', padx=(5, 13))
            self.rows[zone] = (clock, state)
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill='x', padx=17, pady=(9, 13))
        tk.Label(bottom, text='투명도', fg=MUTED, bg=BG,
                 font=('Malgun Gothic', 9)).pack(side='left')
        scale = tk.Scale(bottom, from_=55, to=100, orient='horizontal',
                         command=self.set_alpha, bg=BG, fg=MUTED,
                         highlightthickness=0, troughcolor=PANEL2,
                         showvalue=False, bd=0, length=120)
        scale.set(round(self.alpha * 100))
        scale.pack(side='left', padx=(7, 0))
        tk.Label(bottom, text='30초 자동 갱신', fg=MUTED, bg=BG,
                 font=('Malgun Gothic', 8)).pack(side='right')

    def set_alpha(self, value):
        self.alpha = int(value) / 100
        self.root.attributes('-alpha', self.alpha)

    def update_sound_button(self):
        self.sound_button.config(text='♪ 알람 ON' if self.sound else '♪ 알람 OFF',
                                 fg=CYAN if self.sound else MUTED)

    def toggle_sound(self):
        self.sound = not self.sound
        self.update_sound_button()
        self.save()

    def set_client_id(self):
        value = simpledialog.askstring('서버 클라이언트 ID',
                                       '게임의 X-Modak-Client-Id 값을 입력하세요.\n이 값은 PC에만 저장됩니다.',
                                       initialvalue=self.client_id, parent=self.root)
        if value is None:
            return
        value = value.strip()
        if not value:
            messagebox.showwarning('확인', '클라이언트 ID를 입력해 주세요.', parent=self.root)
            return
        self.client_id = value
        self.save()
        self.next_fetch = 0
        self.refresh()

    def refresh(self):
        if self.fetching or not self.client_id:
            if not self.client_id:
                self.set_client_id()
            return
        self.fetching = True
        self.next_fetch = time.monotonic() + 30
        self.status.config(text='서버 동기화 중…')
        threading.Thread(target=self.worker, args=(self.client_id,), daemon=True).start()

    def worker(self, client_id):
        try:
            req = urllib.request.Request(API, headers={
                'X-Modak-Client-Id': client_id,
                'Accept': 'application/json',
                'Origin': 'https://modakmmo.com',
                'Referer': 'https://modakmmo.com/',
                'User-Agent': 'ModakBossTimer/5.0',
            })
            started = time.time()
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read(2_000_000).decode('utf-8'))
            finished = time.time()
            server_time = payload.get('server_time')
            timers = payload.get('timers')
            if not isinstance(server_time, (int, float)) or not isinstance(timers, list):
                raise ValueError('서버 시간 또는 타이머 목록이 없습니다.')
            offset = server_time / 1000 - (started + finished) / 2
            results = {}
            for timer in timers:
                zone = timer.get('zone_id')
                spawn = timer.get('next_spawn_at')
                if zone in self.rows and isinstance(spawn, (int, float)) and spawn > 0:
                    results[zone] = (spawn / 1000 - offset, bool(timer.get('active', False)))
            if not results:
                raise ValueError('월드보스 시간이 응답에 없습니다.')
            self.messages.put(('success', results))
        except Exception as exc:
            self.messages.put(('failure', str(exc)))

    def play_alarm(self, threshold):
        patterns = {300: [(740, 160), (0, 100), (740, 160)],
                    60: [(900, 220), (0, 100), (900, 220), (0, 100), (900, 220)],
                    0: [(1050, 330), (0, 130), (1050, 330), (0, 130), (1050, 500)]}
        try:
            for freq, ms in patterns[threshold]:
                if freq:
                    winsound.Beep(freq, ms)
                else:
                    time.sleep(ms / 1000)
        except Exception:
            winsound.MessageBeep()

    def check_alarms(self, now):
        for _, zone in ZONES:
            end = self.ends.get(zone)
            if end is None:
                continue
            remain = end - now
            previous = self.previous.get(zone)
            if previous is not None and self.sound:
                fired = self.alerted.setdefault(zone, set())
                for threshold in (300, 60, 0):
                    if threshold not in fired and previous > threshold >= remain:
                        fired.add(threshold)
                        threading.Thread(target=self.play_alarm, args=(threshold,), daemon=True).start()
            self.previous[zone] = remain

    def loop(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                self.fetching = False
                if kind == 'success':
                    now = time.time()
                    for zone, (end, active) in payload.items():
                        old = self.ends.get(zone)
                        if old is None or abs(old - end) > 3:
                            self.alerted[zone] = set()
                            self.previous[zone] = end - now  # avoid spurious alarms on initial sync
                        self.ends[zone] = end
                        self.active[zone] = active
                    self.last_success = now
                    self.status.config(text=f'서버 연결됨 · {len(payload)}/8개 동기화')
                    self.indicator.config(fg=CYAN)
                else:
                    self.status.config(text='연결 실패 · 이전 시간 유지')
                    self.indicator.config(fg=RED)
        except queue.Empty:
            pass
        now = time.time()
        for _, zone in ZONES:
            clock, state = self.rows[zone]
            end = self.ends.get(zone)
            if self.active.get(zone):
                clock.config(text='출현 중', fg=CYAN)
                state.config(text='보스 활성화', fg=CYAN)
            elif end is None:
                clock.config(text='--:--', fg=MUTED)
                state.config(text='동기화 대기', fg=MUTED)
            elif end <= now:
                clock.config(text='출현 예상', fg=RED)
                state.config(text='다음 출현 정보 대기', fg=MUTED)
            else:
                remain = end - now
                clock.config(text=fmt(remain), fg=GOLD if remain <= 300 else FG)
                state.config(text='곧 출현' if remain <= 300 else '다음 출현까지',
                             fg=GOLD if remain <= 300 else MUTED)
        self.check_alarms(now)
        if not self.fetching and self.client_id and time.monotonic() >= self.next_fetch:
            self.refresh()
        self.root.after(350, self.loop)

    def close(self):
        self.save()
        self.root.destroy()


if __name__ == '__main__':
    HOME.mkdir(parents=True, exist_ok=True)
    window = tk.Tk()
    App(window)
    window.mainloop()
