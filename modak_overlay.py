"""MODAK boss timer 3.0 — map-triggered auto sync and sound alarms.
Run with Python or package using the existing PyInstaller workflow.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import winsound
from collections import Counter
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog

import mss
from PIL import Image, ImageOps, ImageEnhance, ImageTk

NAMES = ['그늘숲', '초원', '황야', '협곡 진입로', '망자의 묘지',
         '망국의 기록보관소', '잊혀진 고성 입구', '망국의 병기창']
BASE = Path(os.environ.get('APPDATA') or Path.home()) / 'ModakBossOverlay'
CONFIG = BASE / 'config.json'
DIAG = BASE / 'ocr_diagnostics'
BG = '#17212b'
FG = '#e7f1fa'


def locate_tesseract():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [root / 'Tesseract-OCR' / 'tesseract.exe',
                  exe_dir / '_internal' / 'Tesseract-OCR' / 'tesseract.exe',
                  exe_dir / 'Tesseract-OCR' / 'tesseract.exe',
                  Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe'),
                  Path(r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe')]
    system = shutil.which('tesseract')
    if system:
        candidates.append(Path(system))
    for candidate in candidates:
        if candidate.is_file():
            data = candidate.parent / 'tessdata'
            return candidate, data if (data / 'eng.traineddata').is_file() else None
    return None, None


def parse_time(text):
    text = text.strip().replace('：', ':')
    if not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', text):
        raise ValueError('MM:SS 또는 HH:MM:SS로 입력하세요.')
    parts = list(map(int, text.split(':')))
    if any(x >= 60 for x in parts[1:]):
        raise ValueError('분과 초는 00~59여야 합니다.')
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]


def format_time(value):
    value = max(0, int(value))
    hours, rem = divmod(value, 3600)
    minutes, seconds = divmod(rem, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def extract_times(raw):
    normalized = re.sub(r'\s+', '', raw).replace('：', ':')
    found = []
    for match in re.finditer(r'(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)', normalized):
        try:
            found.append((match.group(), parse_time(match.group())))
        except ValueError:
            pass
    return found


def ocr_image(image, executable, data_dir, psm=7):
    # Use ASCII-only temp paths, avoiding Windows console encoding problems.
    with tempfile.TemporaryDirectory(prefix='modak_') as temp:
        src = Path(temp) / 'image.png'
        dst = Path(temp) / 'output'
        image.save(src)
        cmd = [str(executable), str(src), str(dst), '-l', 'eng',
               '--psm', str(psm), '-c', 'tessedit_char_whitelist=0123456789:']
        if data_dir:
            cmd += ['--tessdata-dir', str(data_dir)]
        env = os.environ.copy()
        env.pop('TESSDATA_PREFIX', None)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=6, env=env,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode('utf-8', errors='replace')[-400:])
        output = dst.with_suffix('.txt')
        return output.read_text(encoding='utf-8', errors='replace') if output.exists() else ''


def variants(original):
    """Search several vertical bands and color thresholds; no guessed colons."""
    w, h = original.size
    bands = [('top', 0, .52), ('middle', .15, .73),
             ('lower', .27, .88), ('whole', 0, 1)]
    for label, start, end in bands:
        top = min(h-1, int(h*start))
        bottom = max(top+1, min(h, int(h*end)))
        gray = ImageOps.grayscale(original.crop((0, top, w, bottom)))
        zoom = gray.resize((max(120, w*6), max(60, (bottom-top)*6)), Image.Resampling.LANCZOS)
        for threshold in (200, 175, 225):
            # Tesseract prefers dark letters on a white background.
            image = zoom.point(lambda v, t=threshold: 0 if v >= t else 255)
            padded = Image.new('L', (image.width+40, image.height+40), 255)
            padded.paste(image, (20, 20))
            yield f'{label}_white{threshold}', padded
        if label in ('middle', 'whole'):
            image = ImageOps.autocontrast(zoom)
            padded = Image.new('L', (image.width+40, image.height+40), 255)
            padded.paste(image, (20, 20))
            yield f'{label}_gray', padded


def recognize(original, executable, data_dir):
    """Return seconds or None, plus diagnostics. Prefer independently repeated readings."""
    votes = Counter()
    traces = []
    best_preview = None
    # First pass: prioritize the proven high-contrast white-digit approach.
    for index, (name, image) in enumerate(variants(original)):
        if best_preview is None:
            best_preview = image.copy()
        try:
            raw = ocr_image(image, executable, data_dir)
            candidates = extract_times(raw)
            traces.append(f'{name} psm7: {raw.strip()!r}')
            # One vote per image variant, even if OCR duplicates a time.
            for _, seconds in set(candidates):
                votes[seconds] += 1
        except Exception as exc:
            traces.append(f'{name}: 오류 {exc}')
        # Stop early if multiple distinct preprocessing views agree.
        if votes and votes.most_common(1)[0][1] >= 2:
            leader, count = votes.most_common(1)[0]
            runner_up = votes.most_common(2)[1][1] if len(votes) > 1 else 0
            if count > runner_up:
                return leader, traces, best_preview
    # A single valid reading is useful when other variants cannot see tiny text,
    # but mark it as unconfirmed in diagnostics and require no competing reading.
    if len(votes) == 1:
        result = next(iter(votes))
        traces.append('주의: 단일 시간 후보 (다른 전처리에서 교차 검증되지 않음)')
        return result, traces, best_preview
    if votes:
        traces.append(f'충돌하는 후보: {dict(votes)} — 기존 시간 유지')
    return None, traces, best_preview


class App:
    def __init__(self, root):
        self.root = root
        root.title('MODAK BOSS TIMER 3')
        root.configure(bg=BG)
        root.attributes('-topmost', True)
        self.data = self.load()
        self.alpha = float(self.data.get('alpha', .94))
        root.attributes('-alpha', self.alpha)
        root.geometry(self.data.get('geometry', '390x540+80+80'))
        self.rows = {}
        self.events = queue.Queue()
        self.busy = False
        self.auto_enabled = bool(self.data.get('auto_enabled', True))
        self.sound_enabled = bool(self.data.get('sound_enabled', True))
        self.map_visible = False
        self.map_hits = 0
        self.next_probe = time.monotonic() + 4
        self.next_auto_sync = 0
        self.probing = False
        self.alarmed = {}  # boss index -> {end timestamp, fired thresholds}
        self.build_ui()
        self.draw_rows()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(200, self.tick)

    def load(self):
        try:
            data = json.loads(CONFIG.read_text(encoding='utf-8'))
            if isinstance(data.get('bosses'), list):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {'bosses': [{'name': name, 'end': None, 'rect': None} for name in NAMES],
                'alpha': .94}

    def save(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self.data['alpha'] = self.alpha
        self.data['geometry'] = self.root.geometry()
        self.data['auto_enabled'] = self.auto_enabled
        self.data['sound_enabled'] = self.sound_enabled
        CONFIG.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill='x', padx=10, pady=(10, 4))
        tk.Label(header, text='⚔ MODAK BOSS TIMER 3', bg=BG, fg=FG,
                 font=('Malgun Gothic', 12, 'bold')).pack(side='left')
        tk.Button(header, text='−', command=self.root.iconify, width=3).pack(side='right')
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill='x', padx=8, pady=4)
        for label, fn in [('영역 지정', self.choose_region), ('화면 동기화', self.sync),
                          ('직접 입력', self.manual), ('지역 추가', self.add)]:
            tk.Button(bar, text=label, command=fn,
                      font=('Malgun Gothic', 8)).pack(side='left', padx=2)
        self.status = tk.Label(self.root, bg=BG, fg='#a7c5d9', wraplength=365,
                               text='자동 감지 대기 중: 월드맵을 열면 자동 동기화합니다.',
                               font=('Malgun Gothic', 9), justify='left')
        self.status.pack(fill='x', padx=10, pady=(2, 4))
        options = tk.Frame(self.root, bg=BG)
        options.pack(fill='x', padx=10, pady=(0, 4))
        self.auto_var = tk.BooleanVar(value=self.auto_enabled)
        self.sound_var = tk.BooleanVar(value=self.sound_enabled)
        tk.Checkbutton(options, text='월드맵 자동 감지', variable=self.auto_var,
                       command=self.toggle_auto, bg=BG, fg=FG,
                       selectcolor='#233140', activebackground=BG,
                       activeforeground=FG).pack(side='left')
        tk.Checkbutton(options, text='알람음', variable=self.sound_var,
                       command=self.toggle_sound, bg=BG, fg=FG,
                       selectcolor='#233140', activebackground=BG,
                       activeforeground=FG).pack(side='left', padx=9)
        panel = tk.Frame(self.root, bg=BG)
        panel.pack(fill='both', expand=True, padx=8)
        self.canvas = tk.Canvas(panel, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(panel, orient='vertical', command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.list_frame.bind('<Configure>', lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.create_window((0, 0), window=self.list_frame, anchor='nw')
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill='x', padx=10, pady=8)
        tk.Label(footer, text='투명도', bg=BG, fg=FG).pack(side='left')
        scale = tk.Scale(footer, from_=45, to=100, orient='horizontal',
                         showvalue=False, bg=BG, highlightthickness=0,
                         length=125, command=self.opacity)
        scale.set(round(self.alpha*100))
        scale.pack(side='left')
        tk.Button(footer, text='설정 저장', command=self.save).pack(side='right')

    def toggle_auto(self):
        self.auto_enabled = self.auto_var.get()
        self.map_visible = False
        self.map_hits = 0
        self.next_probe = time.monotonic() + 1
        self.status.config(text='자동 감지 켜짐' if self.auto_enabled else '자동 감지 꺼짐')
        self.save()

    def toggle_sound(self):
        self.sound_enabled = self.sound_var.get()
        self.save()

    def opacity(self, value):
        self.alpha = int(value)/100
        self.root.attributes('-alpha', self.alpha)

    def draw_rows(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.rows.clear()
        for index, boss in enumerate(self.data['bosses']):
            frame = tk.Frame(self.list_frame, bg='#233140', padx=8, pady=7)
            frame.pack(fill='x', pady=2)
            label = tk.Label(frame, text=boss['name'], fg=FG, bg='#233140',
                             font=('Malgun Gothic', 10), anchor='w')
            label.pack(side='left', fill='x', expand=True)
            countdown = tk.Label(frame, text='미확인', fg='#b0c2d0', bg='#233140',
                                  font=('Consolas', 12, 'bold'))
            countdown.pack(side='right')
            self.rows[index] = countdown
            for widget in (frame, label, countdown):
                widget.bind('<Button-3>', lambda event, i=index: self.menu(event, i))

    def menu(self, event, index):
        popup = tk.Menu(self.root, tearoff=0)
        popup.add_command(label='시간 직접 입력', command=lambda: self.manual(index))
        popup.add_command(label='인식 영역 재지정', command=lambda: self.choose_region(index))
        popup.add_command(label='지역 이름 변경', command=lambda: self.rename(index))
        popup.add_command(label='지역 삭제', command=lambda: self.delete(index))
        try:
            popup.tk_popup(event.x_root, event.y_root)
        finally:
            popup.grab_release()

    def choose_index(self, prompt):
        names = [boss['name'] for boss in self.data['bosses']]
        name = simpledialog.askstring('지역 선택', prompt + '\n' + ', '.join(names), parent=self.root)
        if not name:
            return None
        if name.strip() not in names:
            messagebox.showerror('오류', '지역 이름을 정확히 입력해 주세요.')
            return None
        return names.index(name.strip())

    def manual(self, index=None):
        if index is None:
            index = self.choose_index('시간을 입력할 지역 이름:')
        if index is None:
            return
        boss = self.data['bosses'][index]
        value = simpledialog.askstring('남은 시간', boss['name']+' (MM:SS 또는 HH:MM:SS)', parent=self.root)
        if value is None:
            return
        try:
            boss['end'] = time.time() + parse_time(value)
        except ValueError as exc:
            messagebox.showerror('입력 오류', str(exc))
            return
        self.alarmed.pop(index, None)
        self.status.config(text=boss['name']+' 수동 입력 완료')
        self.save()

    def add(self):
        name = simpledialog.askstring('지역 추가', '지역 이름:', parent=self.root)
        if name and name.strip():
            self.data['bosses'].append({'name': name.strip(), 'end': None, 'rect': None})
            self.draw_rows()
            self.save()

    def rename(self, index):
        boss = self.data['bosses'][index]
        name = simpledialog.askstring('이름 변경', '새 지역 이름:',
                                       initialvalue=boss['name'], parent=self.root)
        if name and name.strip():
            boss['name'] = name.strip()
            self.draw_rows()
            self.save()

    def delete(self, index):
        if messagebox.askyesno('삭제', self.data['bosses'][index]['name']+' 지역을 삭제할까요?'):
            self.data['bosses'].pop(index)
            self.draw_rows()
            self.save()

    def choose_region(self, index=None):
        if self.busy:
            self.status.config(text='동기화 중에는 영역을 변경할 수 없습니다.')
            return
        if index is None:
            index = self.choose_index('영역을 지정할 지역 이름:')
        if index is None:
            return
        self.root.withdraw()
        self.root.after(350, lambda: self.region_window(index))

    def region_window(self, index):
        try:
            with mss.mss() as sct:
                screen = sct.monitors[0]
                shot = sct.grab(screen)
            image = Image.frombytes('RGB', shot.size, shot.rgb)
            window = tk.Toplevel(self.root)
            window.overrideredirect(True)
            window.attributes('-topmost', True)
            window.geometry(f'{image.width}x{image.height}+{screen["left"]}+{screen["top"]}')
            photo = ImageTk.PhotoImage(image)
            canvas = tk.Canvas(window, width=image.width, height=image.height,
                               highlightthickness=0, cursor='crosshair')
            canvas.pack()
            canvas.create_image(0, 0, image=photo, anchor='nw')
            canvas.image = photo
            canvas.create_text(18, 18, anchor='nw', fill='yellow',
                               font=('Malgun Gothic', 14, 'bold'),
                               text='숫자 전체를 드래그하세요 (ESC 취소)')
            drag = {'start': None, 'shape': None}

            def close():
                window.destroy()
                self.root.deiconify()

            def down(event):
                drag['start'] = (event.x, event.y)
                drag['shape'] = canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                         outline='yellow', width=2)

            def move(event):
                if drag['start']:
                    canvas.coords(drag['shape'], *drag['start'], event.x, event.y)

            def up(event):
                if drag['start']:
                    x, y = drag['start']
                    left, top = min(x, event.x), min(y, event.y)
                    width, height = abs(x-event.x), abs(y-event.y)
                    if width >= 10 and height >= 10:
                        self.data['bosses'][index]['rect'] = [left+screen['left'],
                                                             top+screen['top'], width, height]
                        self.save()
                        self.status.config(text=self.data['bosses'][index]['name']+' 영역 저장 완료')
                close()

            canvas.bind('<ButtonPress-1>', down)
            canvas.bind('<B1-Motion>', move)
            canvas.bind('<ButtonRelease-1>', up)
            window.bind('<Escape>', lambda _event: close())
        except Exception as exc:
            self.root.deiconify()
            messagebox.showerror('화면 캡처 오류', str(exc))

    def sync(self):
        if self.busy:
            self.status.config(text='이미 동기화 중입니다. 잠시 기다려 주세요.')
            return
        executable, data_dir = locate_tesseract()
        if executable is None:
            self.status.config(text='Tesseract OCR 실행 파일을 찾지 못했습니다.')
            messagebox.showerror('OCR 오류', 'Tesseract 실행 파일을 찾지 못했습니다.')
            return
        targets = [(i, tuple(map(int, boss['rect'])), boss['name'])
                   for i, boss in enumerate(self.data['bosses']) if boss.get('rect')]
        if not targets:
            self.status.config(text='먼저 영역 지정을 해 주세요.')
            return
        self.busy = True
        self.status.config(text='지도 캡처 중...')
        # Hide overlay while capturing, so it cannot obscure a boss timer.
        self.root.withdraw()
        self.root.after(300, lambda: self.capture_and_scan(targets, executable, data_dir))

    def capture_and_scan(self, targets, executable, data_dir):
        try:
            captures = []
            with mss.mss() as sct:
                for index, rect, name in targets:
                    x, y, w, h = rect
                    if w <= 0 or h <= 0:
                        captures.append((index, name, rect, None, '잘못된 영역 크기'))
                        continue
                    shot = sct.grab({'left': x, 'top': y, 'width': w, 'height': h})
                    captures.append((index, name, rect,
                                     Image.frombytes('RGB', shot.size, shot.rgb), None))
            captured_at = time.time()
            self.root.deiconify()
            self.status.config(text=f'시간 인식 중... 0/{len(captures)}개 처리')
            threading.Thread(target=self.worker,
                             args=(captures, executable, data_dir, captured_at),
                             daemon=True).start()
        except Exception as exc:
            self.root.deiconify()
            self.busy = False
            self.status.config(text='캡처 오류: '+str(exc))

    def probe_map(self):
        # Map-open detection requires TWO independently recognized timer fields.
        # This is an OCR-based detector, not a game API; it may miss some openings.
        if self.busy or self.probing or not self.auto_enabled:
            return
        targets = [(i, tuple(map(int, b['rect'])))
                   for i, b in enumerate(self.data['bosses']) if b.get('rect')]
        if len(targets) < 2:
            return
        executable, data_dir = locate_tesseract()
        if executable is None:
            self.status.config(text='자동 감지: Tesseract를 찾지 못했습니다.')
            self.next_probe = time.monotonic() + 30
            return
        # Capture only two compact, previously reliable areas. Never OCR overlay pixels.
        # Keep detection lightweight and do not modify countdowns during probes.
        preferred = [0, 1, 3, 5, 6, 7]
        selected = sorted(targets, key=lambda pair: preferred.index(pair[0])
                          if pair[0] in preferred else 99)[:2]
        self.probing = True
        # Hiding for ~200ms prevents overlay from contaminating screen capture.
        self.root.withdraw()
        self.root.after(220, lambda: self.capture_probe(selected, executable, data_dir))

    def capture_probe(self, selected, executable, data_dir):
        try:
            samples = []
            with mss.mss() as sct:
                for _, (x, y, w, h) in selected:
                    shot = sct.grab({'left': x, 'top': y, 'width': w, 'height': h})
                    samples.append(Image.frombytes('RGB', shot.size, shot.rgb))
            self.root.deiconify()
            threading.Thread(target=self.probe_worker,
                             args=(samples, executable, data_dir), daemon=True).start()
        except Exception as exc:
            self.root.deiconify()
            self.probing = False
            self.next_probe = time.monotonic() + 10
            self.status.config(text='지도 감지 오류: ' + str(exc))

    def probe_worker(self, samples, executable, data_dir):
        recognized = 0
        try:
            for image in samples:
                # Only a few known-good preprocessings are needed for map detection.
                for idx, (_, variant) in enumerate(variants(image)):
                    if idx >= 3:
                        break
                    if extract_times(ocr_image(variant, executable, data_dir)):
                        recognized += 1
                        break
            self.events.put(('probe', recognized == len(samples)))
        except Exception as exc:
            self.events.put(('probe_error', str(exc)))

    def handle_probe(self, detected):
        self.probing = False
        now = time.monotonic()
        if not self.auto_enabled:
            return
        if detected:
            self.map_hits += 1
            # A single full pair of OCR readings is evidence of a visible map.
            if not self.map_visible:
                self.map_visible = True
                self.next_auto_sync = now
            if not self.busy and now >= self.next_auto_sync:
                self.next_auto_sync = now + 30
                self.sync()
        else:
            self.map_hits = 0
            self.map_visible = False
        self.next_probe = now + (30 if detected else 5)

    def alarm_worker(self, level, name):
        # Native Windows audio: no sound file or new dependency needed.
        try:
            pattern = {300: [(740, 160), (0, 100), (740, 160)],
                       60: [(900, 220), (0, 100), (900, 220), (0, 100), (900, 220)],
                       0: [(1050, 330), (0, 130), (1050, 330), (0, 130), (1050, 500)]}[level]
            for frequency, duration in pattern:
                if frequency:
                    winsound.Beep(frequency, duration)
                else:
                    time.sleep(duration / 1000)
        except Exception:
            try:
                winsound.MessageBeep()
            except Exception:
                pass

    def check_alarms(self, now):
        for index, boss in enumerate(self.data['bosses']):
            end = boss.get('end')
            if end is None:
                continue
            state = self.alarmed.get(index)
            if state is None or abs(state[0] - end) > 3:
                state = [end, set()]
                self.alarmed[index] = state
            remaining = end - now
            # Avoid firing alerts for stale countdowns loaded after their thresholds.
            previous = getattr(self, '_previous_remaining', {}).get(index)
            if previous is None:
                continue
            for threshold in (300, 60, 0):
                if self.sound_enabled and threshold not in state[1] and previous > threshold >= remaining:
                    state[1].add(threshold)
                    threading.Thread(target=self.alarm_worker,
                                     args=(threshold, boss['name']), daemon=True).start()
        self._previous_remaining = {i: b['end'] - now for i, b in enumerate(self.data['bosses'])
                                    if b.get('end') is not None}

    def worker(self, captures, executable, data_dir, captured_at):
        results = []
        reports = ['모닥 월드보스 OCR 2.0 진단', '']
        try:
            DIAG.mkdir(parents=True, exist_ok=True)
            for position, (index, name, rect, image, error) in enumerate(captures, 1):
                if error:
                    results.append((index, None))
                    reports.append(f'{name} | 영역 {rect} | 오류 {error}\n')
                else:
                    image.save(DIAG / f'{index+1:02d}_original.png')
                    seconds, lines, preview = recognize(image, executable, data_dir)
                    if preview is not None:
                        preview.save(DIAG / f'{index+1:02d}_processed.png')
                    results.append((index, seconds))
                    reports.append(f'{name} | 영역 {rect} | 인식: {seconds is not None}')
                    reports.extend(lines)
                    reports.append('')
                self.events.put(('progress', position, len(captures)))
            (DIAG / 'ocr_report.txt').write_text('\n'.join(reports), encoding='utf-8')
            self.events.put(('complete', results, captured_at))
        except Exception:
            error = traceback.format_exc()
            try:
                DIAG.mkdir(parents=True, exist_ok=True)
                (DIAG / 'error.txt').write_text(error, encoding='utf-8')
            except OSError:
                pass
            self.events.put(('error', error.splitlines()[-1]))

    def tick(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'progress':
                    self.status.config(text=f'시간 인식 중... {event[1]}/{event[2]}개 처리')
                elif event[0] == 'probe':
                    self.handle_probe(event[1])
                elif event[0] == 'probe_error':
                    self.probing = False
                    self.map_visible = False
                    self.next_probe = time.monotonic() + 10
                elif event[0] == 'error':
                    self.busy = False
                    self.status.config(text='동기화 오류: '+event[1])
                elif event[0] == 'complete':
                    self.busy = False
                    results, captured_at = event[1], event[2]
                    success = 0
                    for index, seconds in results:
                        if seconds is not None:
                            new_end = captured_at + seconds
                            old_end = self.data['bosses'][index].get('end')
                            self.data['bosses'][index]['end'] = new_end
                            if old_end is None or abs(old_end - new_end) > 3:
                                self.alarmed.pop(index, None)
                            success += 1
                    self.status.config(text=f'동기화 완료: {success}/{len(results)}개 인식\n진단: {DIAG}')
                    self.next_auto_sync = time.monotonic() + 30
                    self.save()
        except queue.Empty:
            pass
        now = time.time()
        for index, boss in enumerate(self.data['bosses']):
            label = self.rows.get(index)
            if label is None:
                continue
            end = boss.get('end')
            if end is None:
                label.config(text='미확인', fg='#a8bdcd')
            elif end <= now:
                label.config(text='출현 예상!', fg='#ff9186')
            else:
                remain = end-now
                label.config(text=format_time(remain),
                             fg='#ffd37b' if remain <= 300 else '#c7e7fc')
        # Schedule automatic detection without blocking the Tk UI.
        if self.auto_enabled and not self.busy and not self.probing and time.monotonic() >= self.next_probe:
            self.next_probe = time.monotonic() + 10
            self.probe_map()
        self.check_alarms(now)
        self.root.after(400, self.tick)

    def close(self):
        self.save()
        self.root.destroy()


if __name__ == '__main__':
    BASE.mkdir(parents=True, exist_ok=True)
    window = tk.Tk()
    App(window)
    window.mainloop()
