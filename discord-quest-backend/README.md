# 🚀 Discord Quest Auto Farmer — Backend Edition

## Architecture
- **Frontend**: Beautiful Web UI (HTML/CSS/JS) — giống giao diện cũ
- **Backend**: Python Flask + SocketIO — chạy tool Python thật
- **Communication**: WebSocket real-time updates

## ✅ Quest Support
| Type | Status | Notes |
|------|--------|-------|
| WATCH_VIDEO | ✅ Working | Video progress |
| PLAY_ON_DESKTOP | ✅ Working | Heartbeat + Python backend |
| STREAM_ON_DESKTOP | ✅ Working | Heartbeat + Python backend |
| PLAY_ACTIVITY | ✅ Working | Heartbeat + Python backend |
| WATCH_VIDEO_ON_MOBILE | ✅ Working | Video progress |

## 🚀 Deploy

### Local Development
```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

### Deploy to VPS/Server (24/7)
```bash
# 1. Upload files to server
# 2. Install dependencies
pip install -r requirements.txt

# 3. Run with screen/tmux (keep alive)
screen -S quest
python app.py
# Ctrl+A, D to detach

# 4. Or use systemd for auto-start
```

### Deploy to Render/Railway (Free)
1. Push to GitHub
2. Connect to Render/Railway
3. Set start command: `python app.py`
4. Environment: Python 3.10+

## 🔧 Cách dùng
1. Mở web UI
2. Nhập Discord Token
3. Bấm "Kết nối & Chạy"
4. Backend Python chạy thật, WebSocket cập nhật real-time

## 🔒 Bảo mật
- Token gửi đến backend server (của bạn)
- Không lưu token permanent, chỉ dùng trong session
- Chạy trên server riêng = an toàn hơn browser
