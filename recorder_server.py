# -*- coding: utf-8 -*-
"""后端服务 — 抖音直播后台录制，直连 FLV 流"""
import sys, os, time, json, threading, re, requests, subprocess, queue
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")

# 从配置文件加载用户设置
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
def _load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}
def _save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# 应用保存的输出目录
_config = _load_config()
if _config.get("output_dir"):
    OUTPUT_DIR = _config["output_dir"]

os.makedirs(OUTPUT_DIR, exist_ok=True)

DOUYIN_SESSIONID = "23bac88c94e983a8003fd934072cdca4"
QUALITY_ORDER = ['or4', 'uhd', 'hiquhd5', 'hiqhd5', 'hd', 'sd', 'ld']
QUALITY_LABELS = {
    'or4': '原画', 'uhd': '超清', 'hiquhd5': '高码超清', 'hiqhd5': '高码高清',
    'hd': '高清', 'sd': '标清', 'ld': '流畅'
}

state = {
    "running": False,
    "name": "",
    "url": "",
    "current_file": "",
    "current_file_size": 0,
    "segment_minutes": 30,
    "quality": "or4",
    "buffer_mb": 20,
    "status_text": "就绪",
    "segment_started_at": 0,
    "log": [],
    # 定时开始
    "scheduled_at": 0,       # 时间戳，0=未设置
    "scheduled_countdown": 0, # 剩余秒数
}

seg_seconds = 1800
stop_event = threading.Event()
split_event = threading.Event()
pause_event = threading.Event()


def add_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].append(f"[{ts}] {msg}")
    if len(state["log"]) > 100:
        state["log"] = state["log"][-100:]
    print(f"[{ts}] {msg}")


def make_session():
    s = requests.Session()
    s.cookies.set('sessionid', DOUYIN_SESSIONID)
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Referer': 'https://live.douyin.com/',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    return s


def _find_ffmpeg():
    """找到 ffmpeg 可执行文件路径"""
    # 先试系统 PATH
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return "ffmpeg"
    except Exception:
        pass
    # 再试 imageio_ffmpeg 自带的
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return exe
    except Exception:
        pass
    return None

ffmpeg_exe = _find_ffmpeg()
ffmpeg_ok = ffmpeg_exe is not None


def convert_to_mp4(flv_path):
    """用 ffmpeg 无损换容器 FLV→MP4，不重新编码，秒级完成"""
    mp4_path = flv_path.rsplit(".", 1)[0] + ".mp4"
    try:
        sz_flv = os.path.getsize(flv_path) / (1024 * 1024)
        add_log(f"🔄 转换 MP4: {os.path.basename(flv_path)} ({sz_flv:.1f} MB) → ...")
        r = subprocess.run(
            [ffmpeg_exe, "-fflags", "+genpts+igndts+discardcorrupt", "-i", flv_path,
             "-c", "copy",
             "-reset_timestamps", "1",
             "-map_metadata", "-1",
             "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart",
             "-y", mp4_path],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode == 0 and os.path.exists(mp4_path):
            sz_mp4 = os.path.getsize(mp4_path) / (1024 * 1024)
            add_log(f"✅ MP4 完成: {os.path.basename(mp4_path)} ({sz_mp4:.1f} MB)")
            # 转成功就删原 FLV，省空间
            try:
                os.remove(flv_path)
            except Exception:
                pass
            return mp4_path
        else:
            err = r.stderr.strip().split("\n")[-1] if r.stderr else "未知错误"
            add_log(f"⚠ MP4 转换失败: {err}")
            return None
    except Exception as e:
        add_log(f"⚠ MP4 转换异常: {e}")
        return None


def extract_flv_urls(page_text, target_quality=None):
    """提取 FLV 地址，按画质+CDN排序。target_quality 过滤指定画质"""
    all_urls = re.findall(r'https?://[^\"\s,]+\.flv[^\"\s,]*', page_text)

    decoded = []
    for u in all_urls:
        u = u.replace('\\u0026', '&')
        if 'only_audio=1' in u:
            continue
        if target_quality:
            if f'_{target_quality}.flv' in u:
                decoded.append(u)
        else:
            decoded.append(u)

    # 去重
    seen = set()
    unique = []
    for u in decoded:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    def sort_key(url):
        score = 0
        for i, q in enumerate(QUALITY_ORDER):
            if f'_{q}.flv' in url:
                score = i * 100
                break
        if url.startswith('https'):
            score -= 10
        if 't11-admin' in url:
            score -= 5
        elif 't5-admin' in url:
            score += 50
        return score

    unique.sort(key=sort_key)
    return unique


def check_cookie_valid(douyin_url):
    """点击开始后先验证 cookie 是否有效，无效直接报错不给等"""
    session = make_session()
    try:
        resp = session.get(douyin_url, timeout=15, allow_redirects=False)
        # 被重定向了 — 看目的地
        if resp.status_code in (301, 302):
            loc = resp.headers.get("Location", "")
            # 重定向到另一个直播间 → cookie 有效，只是房间号变了
            if "live.douyin.com" in loc:
                add_log(f"🔀 Cookie 有效，重定向到新房间: {loc[:80]}")
                return True
            # 重定向到登录页 → cookie 过期
            if "login" in loc.lower() or "passport" in loc.lower():
                add_log(f"❌ Cookie 检测: 重定向到登录页 → {loc[:80]}")
                return False
            # 其他重定向放行
            add_log(f"⚠ Cookie 检测: 未知重定向 → {loc[:80]}，放行")
            return True

        text = resp.text[:100000]
        # 检测是否跳到登录页
        if "login" in text.lower() and "passport" in text.lower():
            add_log(f"❌ Cookie 检测: 页面为登录页，sessionid 已过期")
            return False

        # 有效直播页特征
        if 'window.__INITIAL_STATE__' in text:
            return True
        if '.flv' in text or 'stream_url' in text or 'play_addr' in text:
            return True
        if 'room_id' in text or 'web_rid' in text:
            return True
        if '"status"' in text:
            return True  # 2=直播中 4=已结束

        # 200 且页面内容不少 → 大概率有效
        if len(text) > 5000:
            add_log(f"⚠ Cookie 检测: 未匹配特征但页面正常({len(text)}字符)，放行")
            return True

        add_log(f"❌ Cookie 检测: 页面异常(仅{len(text)}字符)，可能已过期")
        return False
    except Exception as e:
        add_log(f"⚠ Cookie 检测网络异常: {e}")
        return True  # 网络问题不是 cookie 问题，放行


def fetch_flv_url(douyin_url, target_quality=None):
    """获取最佳可用 FLV 地址，返回 (url, quality_name)"""
    session = make_session()
    resp = session.get(douyin_url, timeout=15)
    resp.raise_for_status()
    urls = extract_flv_urls(resp.text, target_quality)

    if not urls:
        # 指定画质没有 → 回退到所有画质
        if target_quality:
            add_log(f"⚠ 未找到 {target_quality} 画质，回退到最佳可用")
            urls = extract_flv_urls(resp.text)
        if not urls:
            return None, None

    # 试到第一个 200
    for flv in urls[:20]:
        try:
            r = session.head(flv, timeout=(5, 10), allow_redirects=True)
            if r.status_code == 200:
                q = None
                for qn in QUALITY_ORDER:
                    if f'_{qn}.flv' in flv:
                        q = qn
                        break
                label = QUALITY_LABELS.get(q, '?')
                add_log(f"✅ 获取到流 ({label}, {q}, 共{len(urls)}档)")
                return flv, q
        except Exception:
            continue

    add_log(f"⚠ 使用备用流")
    return urls[0], None


def download_to_file(flv_url, filepath, max_seconds):
    """带缓冲的流式下载 FLV → 文件。
    生产者线程下载 → 内存缓冲队列 → 消费者线程写盘。
    网络抖动时缓冲吸收，短卡顿不断连不切文件。
    返回 (bytes, reason)"""
    buf_mb = state.get("buffer_mb", 20)
    CHUNK = 256 * 1024
    Q_SIZE = max(10, (buf_mb * 1024 * 1024) // CHUNK)  # 20MB ≈ 80 个块

    q = queue.Queue(maxsize=Q_SIZE)
    written = 0
    start = time.time()
    last_update = 0
    last_data = time.time()
    producer_running = [True]  # 用列表引用，线程间共享
    SENTINEL = object()

    def producer():
        """下载线程：从网络拉数据 → 放队列。断开自动重试"""
        retries = 0
        max_retries = 10  # 缓冲模式多给机会
        while retries <= max_retries:
            if stop_event.is_set() or split_event.is_set() or pause_event.is_set():
                break
            try:
                session = make_session()
                resp = session.get(flv_url, stream=True, timeout=(10, 30))
                resp.raise_for_status()
                retries = 0  # 连上就复位
                for chunk in resp.iter_content(chunk_size=CHUNK):
                    if stop_event.is_set() or split_event.is_set() or pause_event.is_set():
                        return
                    if chunk:
                        try:
                            q.put(chunk, timeout=1)  # 1秒超时，让切段信号能插队
                        except queue.Full:
                            if stop_event.is_set() or split_event.is_set() or pause_event.is_set():
                                return
                            q.put(chunk)  # 不是切段就继续等
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ReadTimeout):
                retries += 1
                if retries <= max_retries:
                    time.sleep(1)
                    continue
                break
            except Exception:
                retries += 1
                if retries <= max_retries:
                    time.sleep(1)
                    continue
                break
        q.put(SENTINEL)
        producer_running[0] = False

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    add_log(f"📦 缓冲 {buf_mb}MB 已启用")

    with open(filepath, 'wb') as f:
        while True:
            try:
                item = q.get(timeout=1)
                if item is SENTINEL:
                    break
                if isinstance(item, bytes):
                    f.write(item)
                    written += len(item)
                    last_data = time.time()
                    now = time.time()
                    if now - last_update > 1:
                        if os.path.exists(filepath):
                            state["current_file_size"] = os.path.getsize(filepath)
                        last_update = now
                # 停止/切段/暂停信号 → 立刻响应
                if stop_event.is_set():
                    return written, 'stopped'
                if split_event.is_set():
                    return written, 'split_manual'
                if pause_event.is_set():
                    return written, 'paused'
                if max_seconds > 0 and (time.time() - start) >= max_seconds:
                    return written, 'timeout'
            except queue.Empty:
                if stop_event.is_set():
                    return written, 'stopped'
                if split_event.is_set():
                    return written, 'split_manual'
                if pause_event.is_set():
                    return written, 'paused'
                # 生产者已死 + 队列空 → 真结束了
                if not producer_running[0] and q.empty():
                    break
                # 缓冲空了超过 45 秒 → 网络真断了，返回让上层重建
                if time.time() - last_data > 45:
                    return written, 'retry_newfile' if written > 0 else 'eof'

    # 如果是因为手动切割/暂停被生产者停止的
    if split_event.is_set():
        return written, 'split_manual'
    if pause_event.is_set():
        return written, 'paused'
    return written, 'eof'


def scheduler_loop():
    """后台调度线程：每秒检查是否到定时开始时间"""
    add_log("⏰ 定时调度器已启动")
    while True:
        time.sleep(1)
        if state["scheduled_at"] <= 0:
            continue
        remaining = max(0, int(state["scheduled_at"] - time.time()))
        state["scheduled_countdown"] = remaining
        if remaining <= 0 and state["running"]:
            # 时间到，检查running标志确认未被取消
            state["scheduled_at"] = 0
            state["scheduled_countdown"] = 0
            global seg_seconds
            seg_seconds = state["segment_minutes"] * 60
            add_log("⏰ 定时时间到，自动开始录制...")
            stop_event.clear()
            split_event.clear()
            t = threading.Thread(target=recording_loop, daemon=True)
            t.start()


def recording_loop():
    """后台录制主循环"""
    stop_event.clear()
    quality = state["quality"]

    add_log(f"🔍 获取直播流地址 (画质: {QUALITY_LABELS.get(quality, quality)})...")
    state["status_text"] = "获取流地址..."

    try:
        flv_url, actual_q = fetch_flv_url(state["url"], quality)
    except Exception as e:
        add_log(f"❌ 获取页面失败: {e}")
        state["running"] = False
        return

    if not flv_url:
        # 等开播
        add_log("❌ 未找到视频流，等待开播...")
        while state["running"]:
            state["status_text"] = "等待开播..."
            for _ in range(30):
                if not state["running"]:
                    break
                time.sleep(1)
            if not state["running"]:
                break
            try:
                flv_url, actual_q = fetch_flv_url(state["url"], quality)
                if flv_url:
                    break
            except Exception:
                pass
        if not state["running"]:
            state["status_text"] = "已停止"
            add_log("录制停止")
            return

    segment_idx = 1
    while state["running"]:
        state["segment_started_at"] = time.time()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(OUTPUT_DIR, f"{state['name']}_{ts}.flv")
        state["current_file"] = os.path.basename(filename)

        add_log(f"▶ 第{segment_idx}段: {os.path.basename(filename)}")
        state["status_text"] = "录制中"

        limit = seg_seconds if state["segment_minutes"] > 0 else 0
        written, reason = download_to_file(flv_url, filename, limit)

        if os.path.exists(filename):
            sz_mb = os.path.getsize(filename) / (1024 * 1024)
            add_log(f"  已保存 {sz_mb:.1f} MB ({reason})")
            # 自动转 MP4（后台线程，不阻塞录制）
            if ffmpeg_ok:
                threading.Thread(target=convert_to_mp4, args=(filename,), daemon=True).start()

        if not state["running"]:
            break

        if reason == 'stopped':
            break

        if reason == 'paused':
            # 暂停：保存当前段，等待恢复
            add_log("⏸ 录制已暂停")
            state["status_text"] = "已暂停"
            state["paused"] = True
            # 等待恢复信号
            while state["running"]:
                if not pause_event.is_set():
                    # pause_event 被清除 — 可能是恢复，也可能是停止
                    # 先检查 running 状态，防止把停止当成恢复
                    pause_event.clear()
                    if not state["running"]:
                        break
                    # 确认是恢复
                    state["paused"] = False
                    add_log("▶ 录制已恢复")
                    state["status_text"] = "录制中"
                    # 重新获取 FLV URL（旧的可能已过期）
                    try:
                        flv_url, actual_q = fetch_flv_url(state["url"], quality)
                        if flv_url:
                            segment_idx += 1
                            break
                    except Exception:
                        pass
                    # 获取失败，等一会再试
                    time.sleep(3)
                    continue
                time.sleep(0.5)
            if not state["running"]:
                break
            continue

        if reason in ('timeout', 'split_manual'):
            split_event.clear()
            segment_idx += 1
            continue

        if reason == 'retry_newfile':
            # 重连成功，开新文件避免音画不同步
            add_log("🔌 重连成功，开新分段")
            segment_idx += 1
            continue

        if reason in ('eof', 'error'):
            # 不管是主播卡了还是自己网卡了，先刷新 FLV URL 快速重试
            add_log("🔌 连接中断，尝试恢复...")
            state["status_text"] = "重连中..."

            # 快速重试 3 次，每次刷新 URL
            recovered = False
            for attempt in range(3):
                if not state["running"]:
                    break
                time.sleep(3)
                try:
                    new_url, _ = fetch_flv_url(state["url"], quality)
                    if new_url:
                        flv_url = new_url
                        add_log(f"✅ 已恢复，继续录制")
                        state["status_text"] = "录制中"
                        recovered = True
                        break
                except Exception:
                    pass

            if recovered:
                segment_idx += 1
                continue

            # 快速重试失败 → 进入等待循环
            add_log("⏳ 等待开播...")
            state["status_text"] = "等待开播..."
            while state["running"]:
                for _ in range(30):
                    if not state["running"]:
                        break
                    time.sleep(1)
                if not state["running"]:
                    break
                try:
                    flv_url, actual_q = fetch_flv_url(state["url"], quality)
                    if flv_url:
                        add_log("✅ 检测到开播，恢复录制")
                        segment_idx += 1
                        break
                except Exception:
                    pass

    state["status_text"] = "已停止"
    add_log("录制停止")


# ============================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path == "/api/state":
            seg_elapsed = int(time.time() - state["segment_started_at"]) if state["running"] and state["segment_started_at"] else 0
            self._json({
                "running": state["running"],
                "name": state["name"],
                "url": state["url"],
                "current_file": state["current_file"],
                "current_file_size": state["current_file_size"],
                "segment_minutes": state["segment_minutes"],
                "quality": state["quality"],
                "buffer_mb": state["buffer_mb"],
                "status_text": state["status_text"],
                "elapsed": seg_elapsed,
                "log": state["log"][-20:],
                "scheduled_at": state["scheduled_at"],
                "scheduled_countdown": state["scheduled_countdown"],
                "paused": state.get("paused", False),
                "output_dir": OUTPUT_DIR,
            })
        elif self.path == "/api/get_cookie":
            self._json({"sessionid": DOUYIN_SESSIONID})
        elif self.path == "/api/get_output_dir":
            self._json({"output_dir": OUTPUT_DIR})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            if getattr(sys, "frozen", False):
                panel = os.path.join(sys._MEIPASS, "recorder_panel.html")
            else:
                panel = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recorder_panel.html")
            with open(panel, "rb") as f:
                self.wfile.write(f.read())

    def do_POST(self):
        global seg_seconds, DOUYIN_SESSIONID

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/api/start":
            url = body.get("url", "").strip()
            name = body.get("name", "").strip()
            mins = body.get("segment_minutes", 30)
            qual = body.get("quality", "or4")
            buf_mb = body.get("buffer_mb", 20)

            if not url:
                self._json({"ok": False, "error": "URL 不能为空"})
                return
            if not name:
                name = url.rstrip('/').split('/')[-1]

            # 先检测 cookie 是否有效
            add_log("🔍 检测 Cookie 有效性...")
            if not check_cookie_valid(url):
                self._json({"ok": False, "error": "Cookie 已过期，请重新获取 sessionid（Chrome F12 → Application → Cookies → live.douyin.com → sessionid）"})
                return
            add_log("✅ Cookie 有效")

            if state["running"]:
                state["running"] = False
                stop_event.set()
                time.sleep(1)

            seg_seconds = mins * 60
            stop_event.clear()
            split_event.clear()
            pause_event.clear()
            state["url"] = url
            state["name"] = name
            state["segment_minutes"] = mins
            state["quality"] = qual
            state["buffer_mb"] = buf_mb
            state["running"] = True
            state["log"] = []
            state["status_text"] = "等待开播..."
            state["current_file"] = ""
            state["current_file_size"] = 0
            state["paused"] = False

            add_log(f"🎬 开始监控 {name} | 画质:{QUALITY_LABELS.get(qual, qual)} | 切割:{f'{mins}分钟' if mins > 0 else '不切割'}")
            threading.Thread(target=recording_loop, daemon=True).start()
            self._json({"ok": True})

        elif self.path == "/api/split":
            if state["running"]:
                split_event.set()
                add_log("✂ 手动切割 → 开始新分段")
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "未在录制"})

        elif self.path == "/api/pause":
            if state["running"] and not state.get("paused"):
                pause_event.set()
                add_log("⏸ 暂停录制...")
                self._json({"ok": True})
            elif state.get("paused"):
                self._json({"ok": False, "error": "已在暂停中"})
            else:
                self._json({"ok": False, "error": "未在录制"})

        elif self.path == "/api/resume":
            if state.get("paused"):
                pause_event.clear()
                # recording_loop 检测到 pause_event 清除后会继续
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "当前未暂停"})

        elif self.path == "/api/stop":
            state["running"] = False
            state["paused"] = False
            state["scheduled_at"] = 0
            state["scheduled_countdown"] = 0
            stop_event.set()
            pause_event.clear()  # 如果正在暂停等待中，唤醒它以便退出
            add_log("⏹ 手动停止")
            self._json({"ok": True})

        elif self.path == "/api/schedule_start":
            url = body.get("url", "").strip()
            name = body.get("name", "").strip()
            delay_minutes = body.get("delay_minutes", 0)
            mins = body.get("segment_minutes", 30)
            qual = body.get("quality", "or4")
            buf_mb = body.get("buffer_mb", 20)

            if not url:
                self._json({"ok": False, "error": "URL 不能为空"})
                return
            if delay_minutes <= 0:
                self._json({"ok": False, "error": "定时分钟数必须大于0"})
                return
            if not name:
                name = url.rstrip('/').split('/')[-1]

            # 先验证 cookie
            add_log("🔍 检测 Cookie 有效性...")
            if not check_cookie_valid(url):
                self._json({"ok": False, "error": "Cookie 已过期，请重新获取 sessionid"})
                return
            add_log("✅ Cookie 有效")

            # 如果正在录制中，先停掉
            if state["running"]:
                state["running"] = False
                stop_event.set()
                time.sleep(1)

            # 设置定时
            state["scheduled_at"] = time.time() + delay_minutes * 60
            state["scheduled_countdown"] = delay_minutes * 60
            state["url"] = url
            state["name"] = name
            state["segment_minutes"] = mins
            state["quality"] = qual
            state["buffer_mb"] = buf_mb
            state["running"] = True
            state["log"] = []
            state["status_text"] = "等待定时开始..."
            state["current_file"] = ""
            state["current_file_size"] = 0

            seg_seconds = mins * 60
            stop_event.clear()
            split_event.clear()

            target_time = datetime.fromtimestamp(state["scheduled_at"]).strftime("%H:%M:%S")
            add_log(f"⏰ 已设置定时录制: {target_time} 开始 ({delay_minutes}分钟后)")
            add_log(f"🎬 主播: {name} | 画质:{QUALITY_LABELS.get(qual, qual)} | 切割:{f'{mins}分钟' if mins > 0 else '不切割'}")
            self._json({"ok": True, "scheduled_at": state["scheduled_at"], "target_time": target_time})

        elif self.path == "/api/schedule_cancel":
            if state["scheduled_at"] > 0:
                state["scheduled_at"] = 0
                state["scheduled_countdown"] = 0
                state["running"] = False
                stop_event.set()
                state["status_text"] = "就绪"
                add_log("⏰ 定时录制已取消")
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "没有定时任务"})

        elif self.path == "/api/set_cookie":
            sid = body.get("sessionid", "").strip()
            if not sid:
                self._json({"ok": False, "error": "sessionid 不能为空"})
                return
            DOUYIN_SESSIONID = sid
            add_log("🍪 Cookie 已更新")
            self._json({"ok": True})

        elif self.path == "/api/browse_folder":
            # 弹出原生 Windows 文件夹选择器
            try:
                import subprocess
                ps_cmd = (
                    'Add-Type -AssemblyName System.Windows.Forms; '
                    '$fb = New-Object System.Windows.Forms.FolderBrowserDialog; '
                    '$fb.Description = "选择录制文件保存目录"; '
                    '$fb.ShowNewFolderButton = $true; '
                    'if ($fb.ShowDialog() -eq "OK") { $fb.SelectedPath } else { "" }'
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=60,
                    creationflags=0x08000000 if sys.platform == 'win32' else 0  # CREATE_NO_WINDOW
                )
                selected = r.stdout.strip()
                if selected:
                    self._json({"ok": True, "path": selected})
                else:
                    self._json({"ok": False, "error": "未选择目录"})
            except Exception as e:
                self._json({"ok": False, "error": f"打开对话框失败: {e}"})

        elif self.path == "/api/set_output_dir":
            global OUTPUT_DIR
            new_dir = body.get("output_dir", "").strip()
            if not new_dir:
                self._json({"ok": False, "error": "目录路径不能为空"})
                return
            # 创建目录（如果不存在）
            try:
                os.makedirs(new_dir, exist_ok=True)
            except Exception as e:
                self._json({"ok": False, "error": f"无法创建目录: {e}"})
                return
            OUTPUT_DIR = new_dir
            _config["output_dir"] = new_dir
            _save_config(_config)
            add_log(f"📁 输出目录已更改为: {OUTPUT_DIR}")
            self._json({"ok": True, "output_dir": OUTPUT_DIR})

        else:
            self._json({"ok": False, "error": "unknown"})


if __name__ == "__main__":
    import subprocess
    subprocess.run(["taskkill", "/F", "/IM", "streamlink.exe"], capture_output=True)

    if ffmpeg_ok:
        print(f"✅ ffmpeg 可用 → FLV 自动转 MP4 ({ffmpeg_exe})")
    else:
        print("⚠ ffmpeg 未安装 → 仅保存 FLV，不转换 MP4")

    # 启动定时调度线程
    threading.Thread(target=scheduler_loop, daemon=True).start()

    port = 8766
    print(f"录制服务: http://localhost:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
