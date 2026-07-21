from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import threading
import time
import json
import random
import requests
import re
import base64
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
app.config['SECRET_KEY'] = 'discord-quest-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── Config ──
API_BASE = "https://discord.com/api/v9"
POLL_INTERVAL = 60
HEARTBEAT_INTERVAL = 20
SUPPORTED_TASKS = [
    "WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY", "WATCH_VIDEO_ON_MOBILE",
]

# Global state
active_sessions = {}

def log_emit(sid, level, msg):
    """Send log to frontend via WebSocket"""
    socketio.emit('log', {'level': level, 'msg': msg, 'time': datetime.now().strftime('%H:%M:%S')}, room=sid)

def emit_quests(sid, quests_data):
    """Send quest update to frontend"""
    socketio.emit('quests_update', quests_data, room=sid)

def emit_stats(sid, stats):
    socketio.emit('stats_update', stats, room=sid)

# ── Build number fetcher ──
def fetch_latest_build_number():
    FALLBACK = 504649
    try:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200: return FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        if not scripts:
            scripts_alt = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
            scripts = [s.split('/')[-1].replace('.js', '') for s in scripts_alt]
        for asset_hash in scripts[-5:]:
            try:
                ar = requests.get(f"https://discord.com/assets/{asset_hash}.js", headers={"User-Agent": ua}, timeout=15)
                m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', ar.text)
                if m: return int(m.group(1))
            except: continue
        return FALLBACK
    except: return FALLBACK

def make_super_properties(build_number):
    obj = {
        "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
        "client_version": "1.0.9175", "os_version": "10.0.26100", "os_arch": "x64",
        "app_arch": "x64", "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36",
        "browser_version": "32.2.7", "client_build_number": build_number,
        "native_build_number": 59498, "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj).encode()).decode()

class DiscordAPI:
    def __init__(self, token, build_number):
        self.token = token
        self.session = requests.Session()
        self.username = "Unknown"
        self.user_id = "Unknown"
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36"
        self.session.headers.update({
            "Authorization": token, "Content-Type": "application/json",
            "User-Agent": ua, "X-Super-Properties": make_super_properties(build_number),
            "X-Discord-Locale": "en-US", "X-Discord-Timezone": "Asia/Ho_Chi_Minh",
        })
    def get(self, path, **kwargs): return self.session.get(f"{API_BASE}{path}", **kwargs)
    def post(self, path, payload=None, **kwargs): return self.session.post(f"{API_BASE}{path}", json=payload, **kwargs)
    def validate_token(self):
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                user = r.json()
                self.username = user.get("username", "?")
                self.user_id = user.get("id", "?")
                return True
            return False
        except: return False

# ── Quest helpers ──
def _get(d, *keys):
    if d is None: return None
    for k in keys:
        if k in d: return d[k]
    return None

def get_task_config(quest): return _get(quest.get("config", {}), "taskConfig", "task_config", "taskConfigV2", "task_config_v2")
def get_quest_name(quest):
    cfg = quest.get("config", {})
    msgs = cfg.get("messages", {})
    name = _get(msgs, "questName", "quest_name") or _get(msgs, "gameTitle", "game_title") or cfg.get("application", {}).get("name")
    return name.strip() if name else f"Quest#{quest.get('id', '?')}"

def get_quest_reward(quest):
    cfg = quest.get("config", {})
    msgs = cfg.get("messages", {})
    reward_name = _get(msgs, "rewardName", "reward_name")
    if reward_name: return reward_name.strip()
    rewards_config = _get(cfg, "rewards_config", "rewardsConfig") or {}
    rewards = rewards_config.get("rewards", []) or cfg.get("rewards", [])
    reward_names = []
    for r in rewards:
        r_msgs = r.get("messages", {})
        name = _get(r_msgs, "name", "name_with_article", "nameWithArticle") or _get(r, "name", "title")
        if name: reward_names.append(name.strip())
    return " + ".join(reward_names) if reward_names else "Vật phẩm trong game"

def get_expires_at(quest): return _get(quest.get("config", {}), "expiresAt", "expires_at")
def get_user_status(quest):
    us = _get(quest, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}

def is_completable(quest):
    expires = get_expires_at(quest)
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt <= datetime.now(timezone.utc): return False
        except: pass
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return False
    return any(tc["tasks"].get(t) is not None for t in SUPPORTED_TASKS)

def is_enrolled(quest): return bool(_get(get_user_status(quest), "enrolledAt", "enrolled_at"))
def is_completed(quest): return bool(_get(get_user_status(quest), "completedAt", "completed_at"))

def get_task_type(quest):
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return None
    for t in SUPPORTED_TASKS:
        if tc["tasks"].get(t) is not None: return t
    return None

def get_seconds_needed(quest):
    tc = get_task_config(quest)
    task_type = get_task_type(quest)
    return tc["tasks"][task_type].get("target", 0) if tc and task_type else 0

def get_seconds_done(quest):
    task_type = get_task_type(quest)
    return get_user_status(quest).get("progress", {}).get(task_type, {}).get("value", 0) if task_type else 0

class QuestState:
    def __init__(self, idx, quest_dict):
        self.idx = idx
        self.qid = quest_dict.get("id")
        self.quest_raw = quest_dict
        self.name = get_quest_name(quest_dict)
        self.reward = get_quest_reward(quest_dict)
        self.task_type = get_task_type(quest_dict)
        self.needed = get_seconds_needed(quest_dict)
        self.done = get_seconds_done(quest_dict)
        self.last_tick = time.time()
        self.is_completed = is_completed(quest_dict)
        self.is_enrolled = is_enrolled(quest_dict)
        self.is_completable = is_completable(quest_dict)
        self.is_running = False
        if self.is_completed:
            self.status = "Done"
            self.done = self.needed
        elif not self.is_enrolled:
            self.status = "Unclaimed"
        else:
            self.status = "Pending"
    def update_progress(self, current_done):
        self.done = current_done
        self.last_tick = time.time()

def quest_to_dict(st):
    return {
        "idx": st.idx, "qid": st.qid, "name": st.name, "reward": st.reward,
        "task_type": st.task_type, "needed": st.needed, "done": st.done,
        "is_completed": st.is_completed, "is_enrolled": st.is_enrolled,
        "is_completable": st.is_completable, "is_running": st.is_running,
        "status": st.status, "pct": round(st.done/st.needed*100,1) if st.needed>0 else 0
    }

class QuestRunner(threading.Thread):
    def __init__(self, token, sid, auto_accept=True, auto_complete=True):
        super().__init__(daemon=True)
        self.token = token
        self.sid = sid
        self.auto_accept = auto_accept
        self.auto_complete = auto_complete
        self.running = True
        self.api = None
        self.states = {}
        self.build_number = fetch_latest_build_number()

    def log(self, level, msg):
        log_emit(self.sid, level, msg)

    def emit_quests(self):
        data = [quest_to_dict(st) for _, st in sorted(self.states.items(), key=lambda x: x[1].idx)]
        emit_quests(self.sid, {"quests": data})

    def emit_stats(self):
        all_q = list(self.states.values())
        stats = {
            "total": len(all_q),
            "running": sum(1 for s in all_q if s.is_running),
            "completed": sum(1 for s in all_q if s.is_completed),
            "rewards": sum(1 for s in all_q if s.is_completed)
        }
        emit_stats(self.sid, stats)

    def fetch_quests(self):
        try:
            r = self.api.get("/quests/@me")
            if r.status_code == 200:
                data = r.json()
                return data.get("quests", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            elif r.status_code == 429:
                time.sleep(r.json().get("retry_after", 5))
                return self.fetch_quests()
        except Exception as e: self.log('err', f'Fetch error: {e}')
        return []

    def enroll_quest(self, qid):
        for _ in range(3):
            try:
                r = self.api.post(f"/quests/{qid}/enroll", {"location": 11, "is_targeted": False})
                if r.status_code in (200, 201, 204): return True
                if r.status_code == 429: time.sleep(r.json().get("retry_after", 3) + 1)
            except: pass
        return False

    def complete_video(self, qid):
        st = self.states[qid]
        st.is_running = True
        self.emit_quests()
        while st.done < st.needed and self.running:
            st.done += 7
            timestamp = min(st.needed, st.done)
            try:
                r = self.api.post(f"/quests/{qid}/video-progress", {"timestamp": timestamp + random.random()})
                if r.status_code == 200:
                    if r.json().get("completed_at"): break
                    st.update_progress(min(st.needed, timestamp))
                    self.emit_quests()
                    self.log('ok', f'[VIDEO] {st.name} — {st.done:.0f}s/{st.needed}s')
                elif r.status_code == 429: time.sleep(r.json().get("retry_after", 5))
            except Exception as e: self.log('err', f'[VIDEO] {st.name} error: {e}')
            time.sleep(1)
        try: self.api.post(f"/quests/{qid}/video-progress", {"timestamp": st.needed})
        except: pass
        st.is_completed = True
        st.is_running = False
        st.status = "Done"
        self.log('ok', f'✅ [VIDEO] Completed: {st.name}')
        self.emit_quests()
        self.emit_stats()

    def complete_heartbeat(self, qid):
        st = self.states[qid]
        st.is_running = True
        self.emit_quests()
        pid = random.randint(1000, 30000)
        stream_key = f"call:0:{pid}" if "STREAM" in st.task_type else "call:0:1"
        hb_count = 0

        self.log('info', f'▶ [GAME] Starting: {st.name} | Need: {st.needed}s | SK: {stream_key}')

        while st.done < st.needed and self.running:
            try:
                r = self.api.post(f"/quests/{qid}/heartbeat", {"stream_key": stream_key, "terminal": False})
                if r.status_code == 200:
                    body = r.json()
                    prog = body.get("progress", {}).get(st.task_type, {}).get("value", st.done)
                    st.update_progress(max(st.done, prog))
                    hb_count += 1
                    self.log('ok', f'[GAME] {st.name} — HB #{hb_count} | {st.done:.0f}s/{st.needed}s')
                    self.emit_quests()
                    if body.get("completed_at") or st.done >= st.needed: break
                elif r.status_code == 429:
                    self.log('warn', f'[GAME] {st.name} — Rate limited, waiting...')
                    time.sleep(r.json().get("retry_after", 10))
            except Exception as e: self.log('err', f'[GAME] {st.name} HB error: {e}')

            for _ in range(HEARTBEAT_INTERVAL):
                if st.done >= st.needed or not self.running: break
                time.sleep(1)

        try: self.api.post(f"/quests/{qid}/heartbeat", {"stream_key": stream_key, "terminal": True})
        except: pass

        st.is_completed = True
        st.is_running = False
        st.status = "Done"
        self.log('ok', f'✅ [GAME] Completed: {st.name} | Total HB: {hb_count}')
        self.emit_quests()
        self.emit_stats()

    def process_quest(self, qid):
        st = self.states.get(qid)
        if not st or st.is_completed: return
        try:
            if st.task_type in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
                self.complete_video(qid)
            elif st.task_type in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY"):
                self.complete_heartbeat(qid)
            else:
                st.status = "Error"
                self.log('err', f'Unsupported: {st.task_type}')
        except Exception as e:
            st.status = "Error"
            st.is_running = False
            self.log('err', f'Quest error {st.name}: {e}')
        self.emit_quests()

    def run(self):
        self.api = DiscordAPI(self.token, self.build_number)
        if not self.api.validate_token():
            self.log('err', 'Token không hợp lệ!')
            return
        self.log('ok', f'Welcome {self.api.username} ({self.api.user_id})')
        self.emit_stats()

        while self.running:
            # 1. Fetch quests
            self.log('info', '🔄 Fetching quests...')
            quests = self.fetch_quests()
            self.log('info', f'Found {len(quests)} quests')

            for i, q in enumerate(quests):
                qid = q["id"]
                if qid not in self.states:
                    self.states[qid] = QuestState(i + 1, q)
                else:
                    srv_done = get_seconds_done(q)
                    self.states[qid].done = max(self.states[qid].done, srv_done)
                    self.states[qid].is_completed = is_completed(q)
                    self.states[qid].is_enrolled = is_enrolled(q)
                    if self.states[qid].is_completed:
                        self.states[qid].status = "Done"
            self.emit_quests()
            self.emit_stats()

            # 2. Auto Accept
            if self.auto_accept:
                for qid, st in self.states.items():
                    if not st.is_enrolled and not st.is_completed and st.is_completable:
                        self.log('info', f'🎁 Enrolling: {st.name}')
                        if self.enroll_quest(qid):
                            st.is_enrolled = True
                            st.status = "Pending"
                            self.log('ok', f'Enrolled: {st.name}')
                        self.emit_quests()
                        time.sleep(1)

            # 3. Auto Complete
            if self.auto_complete:
                actionable = [qid for qid, st in self.states.items() 
                                if st.is_enrolled and not st.is_completed and st.is_completable and st.status != "Error"]
                if actionable:
                    self.log('info', f'⚡ Starting {len(actionable)} quest(s)')
                    with ThreadPoolExecutor(max_workers=len(actionable)) as executor:
                        futures = [executor.submit(self.process_quest, qid) for qid in actionable]
                        while any(not f.done() for f in futures):
                            self.emit_quests()
                            time.sleep(3)
                    self.log('ok', '✨ Batch complete')
                else:
                    self.log('info', '📭 No actionable quests')

            # Countdown
            for remaining in range(POLL_INTERVAL, 0, -5):
                if not self.running: break
                self.log('info', f'⏳ Next scan in {remaining}s...')
                time.sleep(5)

# ── Flask Routes ──
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start():
    data = request.json
    token = data.get('token', '').strip()
    sid = data.get('sid', '')
    auto_accept = data.get('auto_accept', True)
    auto_complete = data.get('auto_complete', True)

    if not token or not sid:
        return jsonify({"error": "Token và Session ID required"}), 400

    # Stop existing
    if sid in active_sessions:
        active_sessions[sid].running = False
        time.sleep(1)

    runner = QuestRunner(token, sid, auto_accept, auto_complete)
    active_sessions[sid] = runner
    runner.start()

    return jsonify({"status": "started", "username": "Validating..."})

@app.route('/api/stop', methods=['POST'])
def stop():
    sid = request.json.get('sid', '')
    if sid in active_sessions:
        active_sessions[sid].running = False
        del active_sessions[sid]
    return jsonify({"status": "stopped"})

# ── WebSocket Events ──
@socketio.on('connect')
def handle_connect():
    emit('connected', {'msg': 'Connected to Discord Quest Farmer'})

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in active_sessions:
        active_sessions[sid].running = False

if __name__ == '__main__':
    # Render uses PORT env variable
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
