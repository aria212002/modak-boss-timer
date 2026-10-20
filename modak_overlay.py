"""Modak MMO world boss overlay. OCR reads user-selected map countdown rectangles."""
import json, os, re, time, threading, queue, tkinter as tk
import sys
import shutil
from tkinter import ttk, messagebox, simpledialog
from pathlib import Path

try:
    import mss
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ImageTk
    import pytesseract
except ImportError:
    pytesseract = None

OCR_DATA_DIR = None
OCR_CONFIG = '--psm 7 -l eng -c tessedit_char_whitelist=0123456789:'
if pytesseract is not None:
    # PyInstaller onedir: sys._MEIPASS normally points to the _internal folder.
    roots = [Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))]
    if getattr(sys, 'frozen', False):
        roots += [Path(sys.executable).resolve().parent / '_internal',
                  Path(sys.executable).resolve().parent]
    candidates = [root / 'Tesseract-OCR' / 'tesseract.exe' for root in roots]
    which = shutil.which('tesseract')
    if which:
        candidates.append(Path(which))
    candidates += [Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe'),
                   Path(r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe')]
    for executable in candidates:
        if executable.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(executable)
            data_dir = executable.parent / 'tessdata'
            if (data_dir / 'eng.traineddata').is_file():
                OCR_DATA_DIR = data_dir
                # Avoid inherited TESSDATA_PREFIX pointing to the wrong directory.
                os.environ.pop('TESSDATA_PREFIX', None)
                # Explicit --tessdata-dir works reliably with bundled Tesseract.
                OCR_CONFIG += f' --tessdata-dir "{data_dir}"'
            break

BASE = Path(os.getenv('APPDATA', str(Path.home()))) / 'ModakBossOverlay'
BASE.mkdir(parents=True, exist_ok=True)
CONFIG = BASE / 'config.json'
DEFAULT_NAMES = ['그늘숲','초원','황야','협곡 진입로','망자의 묘지','망국의 기록보관소','잊혀진 고성 입구','망국의 병기창']

def seconds(text):
    text = text.strip().replace('：', ':')
    if not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', text):
        raise ValueError('MM:SS 또는 HH:MM:SS 형식으로 입력하세요.')
    vals = [int(x) for x in text.split(':')]
    if any(v >= 60 for v in vals[1:]):
        raise ValueError('분/초는 0~59여야 합니다.')
    return vals[0]*60+vals[1] if len(vals)==2 else vals[0]*3600+vals[1]*60+vals[2]

def fmt(n):
    n = max(0, int(n))
    h, rem = divmod(n,3600); m,s=divmod(rem,60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'

class App:
    def __init__(self, root):
        self.root=root; root.title('MODAK BOSS TIMER'); root.attributes('-topmost', True)
        self.data=self.load(); self.rows={}; self.queue=queue.Queue(); self.scanning=False
        self.alpha=self.data.get('alpha',0.91); root.attributes('-alpha',self.alpha)
        root.geometry(self.data.get('geometry','370x480+70+80'))
        root.configure(bg='#17212b')
        self.make_ui(); self.render(); self.tick()
        root.protocol('WM_DELETE_WINDOW',self.close)

    def load(self):
        try: return json.loads(CONFIG.read_text(encoding='utf-8'))
        except Exception: return {'bosses':[{'name':n,'end':None,'rect':None} for n in DEFAULT_NAMES], 'alpha':0.91}

    def save(self):
        self.data['alpha']=self.alpha; self.data['geometry']=self.root.geometry()
        CONFIG.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf-8')

    def make_ui(self):
        head=tk.Frame(self.root,bg='#17212b'); head.pack(fill='x',padx=10,pady=(10,3))
        tk.Label(head,text='⚔ MODAK BOSS TIMER',bg='#17212b',fg='#e9f2fc',font=('Malgun Gothic',12,'bold')).pack(side='left')
        tk.Button(head,text='−',command=self.minimize,width=3).pack(side='right')
        bar=tk.Frame(self.root,bg='#17212b');bar.pack(fill='x',padx=9,pady=5)
        for label,command in [('영역 지정',self.select_region),('화면 동기화',self.scan),('직접 입력',self.manual),('지역 추가',self.add)]:
            tk.Button(bar,text=label,command=command,font=('Malgun Gothic',8)).pack(side='left',padx=2)
        self.status=tk.Label(self.root,text='지도 화면에서 영역 지정 후 화면 동기화를 눌러주세요.',bg='#17212b',fg='#9cb1c6',font=('Malgun Gothic',9),wraplength=345)
        self.status.pack(fill='x',padx=10)
        frame=tk.Frame(self.root,bg='#17212b');frame.pack(fill='both',expand=True,padx=8,pady=6)
        self.canvas=tk.Canvas(frame,bg='#17212b',highlightthickness=0)
        sb=tk.Scrollbar(frame,orient='vertical',command=self.canvas.yview)
        self.inner=tk.Frame(self.canvas,bg='#17212b')
        self.inner.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.create_window((0,0),window=self.inner,anchor='nw')
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side='left',fill='both',expand=True);sb.pack(side='right',fill='y')
        bottom=tk.Frame(self.root,bg='#17212b');bottom.pack(fill='x',padx=10,pady=7)
        tk.Label(bottom,text='투명도',bg='#17212b',fg='white').pack(side='left')
        tk.Scale(bottom,from_=45,to=100,orient='horizontal',showvalue=False,command=self.opacity,length=110,bg='#17212b',highlightthickness=0).pack(side='left')
        tk.Button(bottom,text='설정 저장',command=self.save).pack(side='right')

    def opacity(self,v):
        self.alpha=int(v)/100; self.root.attributes('-alpha',self.alpha)

    def render(self):
        for w in self.inner.winfo_children(): w.destroy()
        self.rows={}
        for i,b in enumerate(self.data['bosses']):
            line=tk.Frame(self.inner,bg='#233140',padx=8,pady=7);line.pack(fill='x',pady=2)
            tk.Label(line,text=b['name'],fg='#eef4fc',bg='#233140',font=('Malgun Gothic',10),anchor='w').pack(side='left',fill='x',expand=True)
            lbl=tk.Label(line,text='미확인',fg='#9bb2c7',bg='#233140',font=('Consolas',12,'bold'))
            lbl.pack(side='right');self.rows[i]=lbl
            line.bind('<Button-3>',lambda e,j=i:self.context(e,j))
            for w in line.winfo_children():w.bind('<Button-3>',lambda e,j=i:self.context(e,j))

    def context(self,e,i):
        menu=tk.Menu(self.root,tearoff=0)
        menu.add_command(label='시간 직접 입력',command=lambda:self.manual(i))
        menu.add_command(label='인식 영역 재지정',command=lambda:self.select_region(i))
        menu.add_command(label='지역 이름 변경',command=lambda:self.rename(i))
        menu.add_command(label='지역 삭제',command=lambda:self.delete(i))
        menu.tk_popup(e.x_root,e.y_root)

    def rename(self,i):
        b=self.data['bosses'][i];name=simpledialog.askstring('이름 변경','지역 이름:',initialvalue=b['name'],parent=self.root)
        if name: b['name']=name.strip();self.render();self.save()

    def delete(self,i):
        if messagebox.askyesno('삭제',self.data['bosses'][i]['name']+' 지역을 삭제할까요?'):
            self.data['bosses'].pop(i);self.render();self.save()

    def add(self):
        name=simpledialog.askstring('지역 추가','지역 이름:',parent=self.root)
        if name: self.data['bosses'].append({'name':name.strip(),'end':None,'rect':None});self.render();self.save()

    def manual(self,i=None):
        if i is None:
            names=[b['name'] for b in self.data['bosses']]
            name=simpledialog.askstring('지역 선택','지역 이름을 입력하세요:\n'+', '.join(names),parent=self.root)
            if not name:return
            try:i=names.index(name.strip())
            except ValueError:messagebox.showerror('오류','지역 이름을 정확히 입력하세요.');return
        b=self.data['bosses'][i]
        v=simpledialog.askstring('남은 시간',b['name']+' 남은 시간 (MM:SS 또는 HH:MM:SS)',parent=self.root)
        if v is None:return
        try:b['end']=time.time()+seconds(v)
        except ValueError as ex:messagebox.showerror('입력 오류',str(ex));return
        self.status.config(text=b['name']+' 수동 입력 완료');self.save()

    def select_region(self,i=None):
        if i is None:
            names=[b['name'] for b in self.data['bosses']]
            name=simpledialog.askstring('지역 선택','영역을 지정할 지역 이름:\n'+', '.join(names),parent=self.root)
            if not name:return
            try:i=names.index(name.strip())
            except ValueError:messagebox.showerror('오류','지역 이름을 정확히 입력하세요.');return
        self.root.withdraw();self.root.after(450,lambda:self.region_window(i))

    def region_window(self,i):
        try:
            with mss.mss() as sct:
                monitor=sct.monitors[0];shot=sct.grab(monitor)
            im=Image.frombytes('RGB',shot.size,shot.rgb)
            win=tk.Toplevel(self.root);win.overrideredirect(True);win.attributes('-topmost',True)
            win.geometry(f'{im.width}x{im.height}+{monitor["left"]}+{monitor["top"]}')
            photo=ImageTk.PhotoImage(im)
            c=tk.Canvas(win,width=im.width,height=im.height,highlightthickness=0,cursor='crosshair')
            c.pack();c.create_image(0,0,image=photo,anchor='nw');c.image=photo
            c.create_text(20,20,anchor='nw',text='시간 숫자만 드래그하여 선택하세요 (ESC 취소)',fill='yellow',font=('Malgun Gothic',15,'bold'))
            state={'start':None,'rect':None}
            def down(e):
                state['start']=(e.x,e.y)
                state['rect']=c.create_rectangle(e.x,e.y,e.x,e.y,outline='#ffdc50',width=3)
            def move(e):
                if state['start']:c.coords(state['rect'],*state['start'],e.x,e.y)
            def done(e):
                if state['start']:
                    x1,y1=state['start'];x2,y2=e.x,e.y
                    if abs(x2-x1)>8 and abs(y2-y1)>8:
                        self.data['bosses'][i]['rect']=[min(x1,x2)+monitor['left'],min(y1,y2)+monitor['top'],abs(x2-x1),abs(y2-y1)]
                        self.save();self.status.config(text=self.data['bosses'][i]['name']+' 인식 영역 저장됨')
                win.destroy();self.root.deiconify()
            c.bind('<ButtonPress-1>',down);c.bind('<B1-Motion>',move);c.bind('<ButtonRelease-1>',done)
            win.bind('<Escape>',lambda e:(win.destroy(),self.root.deiconify()))
        except Exception as ex:self.root.deiconify();messagebox.showerror('캡처 오류',str(ex))

    def scan(self):
        if self.scanning:return
        targets=[(i,b['rect']) for i,b in enumerate(self.data['bosses']) if b.get('rect')]
        if not targets:self.status.config(text='먼저 지역별 시간 숫자 영역을 지정하세요.');return
        if pytesseract is None:
            self.status.config(text='pytesseract 패키지가 없습니다. install.bat을 실행하세요.')
            return
        self.scanning=True;self.status.config(text='게임 화면에서 시간 인식 중…')
        def work():
            results=[]
            try:
                with mss.mss() as sct:
                    for i,(x,y,w,h) in targets:
                        shot=sct.grab({'left':int(x),'top':int(y),'width':int(w),'height':int(h)})
                        im=Image.frombytes('RGB',shot.size,shot.rgb)
                        im=ImageOps.grayscale(im).resize((max(80,w*5),max(35,h*5)))
                        im=ImageEnhance.Contrast(im).enhance(2.3)
                        candidates=[]
                        for candidate in (im,ImageOps.invert(im)):
                            # Read OCR output as bytes: some Windows Tesseract builds emit
                            # non-UTF-8 bytes even when recognizing only digits.
                            raw_bytes = pytesseract.image_to_string(
                                candidate, lang='eng', config=OCR_CONFIG,
                                output_type=pytesseract.Output.BYTES)
                            raw = raw_bytes.decode('utf-8', errors='replace')
                            found=re.findall(r'\d{1,2}:\d{2}(?::\d{2})?',raw)
                            for v in found:
                                try:candidates.append(seconds(v))
                                except ValueError:pass
                        if candidates:results.append((i,candidates[0]))
                self.queue.put(('result',results,len(targets)))
            except Exception as ex:
                detail = str(ex)
                if 'TesseractError' in type(ex).__name__ or 'traineddata' in detail or 'tessdata' in detail:
                    detail = ('OCR 실행 오류. 포함된 tessdata/eng.traineddata 파일과 OCR 실행 경로를 확인하세요. '
                              + f'실행파일: {pytesseract.pytesseract.tesseract_cmd}; 데이터: {OCR_DATA_DIR}; 원본: {detail}')
                self.queue.put(('error',detail))
        threading.Thread(target=work,daemon=True).start()

    def tick(self):
        try:
            while True:
                result=self.queue.get_nowait();self.scanning=False
                if result[0]=='error':self.status.config(text='인식 실패: '+result[1]);continue
                _,values,total=result
                for i,secs in values:self.data['bosses'][i]['end']=time.time()+secs
                self.status.config(text=f'동기화 완료: {len(values)}/{total}개 인식 · 잘못 읽힌 시간은 직접 수정하세요.')
                self.save()
        except queue.Empty:pass
        now=time.time()
        for i,b in enumerate(self.data['bosses']):
            label=self.rows.get(i)
            if not label:continue
            end=b.get('end')
            if end is None:label.config(text='미확인',fg='#9bb2c7')
            elif end<=now:label.config(text='출현 예상!',fg='#ff8c7e')
            else:
                remaining=end-now
                label.config(text=fmt(remaining),fg='#ffcd79' if remaining<=300 else '#c6e6fc')
        self.root.after(500,self.tick)

    def minimize(self):self.root.iconify()
    def close(self):self.save();self.root.destroy()

if __name__=='__main__':
    root=tk.Tk();app=App(root);root.mainloop()
