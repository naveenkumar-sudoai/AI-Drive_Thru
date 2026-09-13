# AI Drive-Thru

A fully local, single-Pi voice-ordering drive-thru: a camera + mic at the
speaker post, a speech pipeline that turns spoken orders into structured JSON
(Whisper → DeepSeek → TTS confirmation), and a live 3-page web dashboard for
kitchen staff. Everything runs on a Raspberry Pi 5 and serves over local WiFi —
no cloud, no auth, no payment.

**Language:** Whisper auto-detects the customer's language, so it works for
**English, Tamil, Hindi, and more** out of the box; the confirmation is spoken
back in the same language (Tamil via `espeak-ng -v ta`). The default menu is
**South Indian** (dosa, idli, vada, pongal, filter coffee, …).

```
mic + camera ──► voice_pipeline/ ──► POST /order ──► backend (FastAPI + SQLite)
                                                        │
                                                        ├─ serves dashboard  (http://<pi-ip>:8000)
                                                        └─ WebSocket /ws ──► live queue updates
```

The **same code runs two ways** — no separate builds:

| | Camera | Presence detection | Photo |
|---|---|---|---|
| **Raspberry Pi** | `picamera2` (auto) | YOLOv8n — fully automatic | yes |
| **PC / no camera** | none | keypress ("press Enter") | placeholder |

`main.py` auto-detects the camera and falls back to keypress mode, so you can
develop/test on a PC and drop the identical code on the Pi.

---

## Project layout

```
ai_drive_thru/
├── voice_pipeline/            # mic -> Whisper -> DeepSeek -> TTS (separate process)
│   ├── main.py                # orchestration loop (camera or keypress mode)
│   ├── test_order.py          # camera-free mic->order test (PC dev / quick check)
│   ├── camera.py              # PresenceDetector (picamera2/cv2) + ManualDetector
│   ├── mic_capture.py         # record while the customer is present
│   ├── stt.py                 # faster-whisper (multilingual, auto-detects Tamil etc.)
│   ├── deepseek_client.py     # DeepSeek parsing + LIVE menu fetch from backend
│   ├── tts_speaker.py         # pyttsx3 / espeak-ng (supports Tamil and others)
│   ├── menu.json              # fallback menu (used only if the backend is down)
│   ├── .env.example           # template for DEEPSEEK_API_KEY (used by systemd)
│   └── requirements.txt
├── backend/
│   ├── app.py                 # FastAPI: endpoints + WebSocket + static serving
│   ├── db.py                  # SQLite setup + queries
│   ├── models.py              # Pydantic schemas
│   └── requirements.txt
├── dashboard/                 # plain HTML/CSS/JS, served by the backend (no build step)
│   ├── index.html
│   ├── style.css
│   └── app.js
├── systemd/                   # auto-start units for headless/enclosure use
│   ├── ai-drive-thru-backend.service
│   └── ai-drive-thru-voice.service
├── data/photos/               # order photos + SQLite DB (created at runtime, gitignored)
├── run.sh                     # starts the backend (+ dashboard) on :8000
└── README.md
```

---

## Raspberry Pi 5 setup (fresh Raspberry Pi OS, 64-bit Bookworm)

### 1. Enable the camera

```bash
sudo raspi-config      # Interface Options → Camera → Enable, then reboot
```

### 2. Install apt packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  python3-venv python3-pip python3-dev \
  portaudio19-dev libportaudio2 libasound2-dev \
  espeak-ng espeak-ng-espeak \
  libgl1 \
  ffmpeg
```

| Package | Why |
|---|---|
| `python3-venv`, `python3-pip`, `python3-dev` | virtualenv + build tools |
| `portaudio19-dev`, `libportaudio2`, `libasound2-dev` | `sounddevice` (mic capture) |
| `espeak-ng`, `espeak-ng-espeak` | TTS — the latter provides the `/usr/bin/espeak` binary `pyttsx3` calls |
| `libgl1` | lets `opencv-python`'s `cv2` import (YOLO needs it) |
| `ffmpeg` | optional; only needed if you swap `faster-whisper` for `openai-whisper` |

`picamera2`/`libcamera` ships with the full image; on the **Lite** image add
`libcamera-apps python3-picamera2`.

### 3. Get the code onto the Pi

```bash
git clone https://github.com/naveenkumar-sudoai/AI-Drive_Thru.git ai_drive_thru
cd ai_drive_thru
```

### 4. Install the backend and run it (first run, for testing)

```bash
chmod +x run.sh
./run.sh
```

`run.sh` creates `.venv`, installs the backend deps once, and starts uvicorn on
`0.0.0.0:8000`. Open **http://\<pi-ip\>:8000** from any device on the WiFi
(`hostname -I` prints the IP).

### 5. Install the voice pipeline (separate venv — pulls in PyTorch)

```bash
cd voice_pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# API key (used by systemd later):
cp .env.example .env
nano .env            # set DEEPSEEK_API_KEY=sk-...
```

First run downloads `yolov8n.pt` (~6 MB) and the Whisper model (~74 MB for
`base`) — do this **while online**, before sealing the enclosure.

### 6. Run the voice pipeline

```bash
cd voice_pipeline && source .venv/bin/activate
export DEEPSEEK_API_KEY="$(cat ../api_key.txt)"   # or: source .env
python main.py
```

On the Pi this is fully automatic (presence-gated). On a camera-less PC it
prints a keypress prompt — press Enter to take an order.

---

## Deploy headless in an enclosure (auto-start, reliable)

For a sealed enclosure there's no keyboard/screen, so run both processes as
systemd services with automatic restart on crash.

```bash
# backend + dashboard
sudo cp systemd/ai-drive-thru-backend.service /etc/systemd/system/
sudo cp systemd/ai-drive-thru-voice.service    /etc/systemd/system/

# edit User= / WorkingDirectory= / ExecStart= to match your Pi user + clone path
sudo nano /etc/systemd/system/ai-drive-thru-backend.service
sudo nano /etc/systemd/system/ai-drive-thru-voice.service

sudo systemctl daemon-reload
sudo systemctl enable --now ai-drive-thru-backend
sudo systemctl enable --now ai-drive-thru-voice
```

Reliability behaviour:

- **Auto-restart** — both units use `Restart=always` (3 s backoff), so a crash
  or transient error recovers by itself.
- **Offline-safe boot** — `run.sh` skips reinstalling dependencies when they're
  already present, so the backend starts fast even with no network.
- **Camera failure** — on a headless Pi the voice pipeline will **not** fall
  back to keypress mode (there's no keyboard); it exits and systemd restarts it,
  retrying the camera.
- **Watch logs** — `journalctl -u ai-drive-thru-backend -f` and
  `journalctl -u ai-drive-thru-voice -f`.

> The voice service reads `DEEPSEEK_API_KEY` from `voice_pipeline/.env`
> (via `EnvironmentFile=`). Make sure that file exists before enabling it.

---

## Tamil / multilingual support

- **Speech-to-text:** Whisper (via `faster-whisper`) is multilingual and
  auto-detects the language from the audio — Tamil included. Force a language
  with `export WHISPER_LANGUAGE=ta` (or `en`) to skip detection and speed it up.
- **Text-to-speech:** the confirmation is spoken in the detected language. Tamil
  uses `espeak-ng -v ta` (a robotic but intelligible voice). For higher-quality
  Tamil/Indian voices later, swap the `_speak_cli` path in `tts_speaker.py` for a
  cloud or Piper TTS voice — the rest of the code doesn't change.

---

## How the voice pipeline behaves

1. `camera.py` runs YOLOv8n on a **320×240** frame sampled every **~0.5 s** to
   keep Pi 5 CPU load low.
2. A person seen continuously for **~1 s** confirms presence; recording starts
   automatically (no push-to-talk). In manual (no-camera) mode, a keypress
   starts the order instead.
3. Recording continues until the person leaves or a **15 s** cap (camera mode),
   or a fixed **8 s** window (manual mode).
4. After DeepSeek parses the order (not on a clarification), a **clean photo is
   captured at that moment** — not mid-sentence (camera mode only).
5. The order is POSTed to `/order`, the photo uploaded to `/upload_photo`, a TTS
   confirmation is spoken (in the customer's language), and the loop resets.

---

## First-run: your menu

The **Menu Editor** tab is the single source of truth — the voice pipeline
fetches it live (`GET /menu`) and feeds it to DeepSeek. A South Indian menu is
already in `voice_pipeline/menu.json` (the offline fallback) and can be seeded
with:

```bash
# per item:
curl -X POST http://localhost:8000/menu -H 'Content-Type: application/json' \
  -d '{"name":"Masala Dosa","price":90,"available":true}'
```

(or just add/edit items in the dashboard). If the backend is down, the pipeline
falls back to `menu.json`.

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/menu` | list menu items |
| `POST` | `/menu` | add item `{name, price, available}` |
| `PUT` | `/menu/{id}` | update item (partial) |
| `DELETE` | `/menu/{id}` | remove item |
| `POST` | `/order` | create order `{order_id, items, total_price, photo_path, timestamp, status}` |
| `GET` | `/orders?status=pending,preparing,ready` | list orders, optional status filter |
| `PATCH` | `/orders/{id}` | update status `{"status": "..."}` |
| `GET` | `/stats` | totals + top items + orders/hour |
| `POST` | `/upload_photo/{order_id}` | multipart image upload → `data/photos/{id}.jpg` |
| `WS` | `/ws` | broadcasts `{"type":"new_order","order":{...}}` and `{"type":"order_updated","order":{...}}` |
| `GET` | `/health` | liveness check |

Order statuses: `pending → preparing → ready → picked_up`.

---

## Performance flags (don't silently change — note & verify)

- **YOLO speed on Pi 5 CPU** (320×240 @ 0.5 Hz): if too slow, **flag it** and
  either (a) export `yolov8n` to ONNX/NCNN, or (b) drop detection resolution
  (256×192 / 224×224) — constructor args in `camera.py`.
- **Whisper speed**: `WHISPER_MODEL=tiny` is near-real-time on Pi; `base` is
  more accurate but slower.

## Troubleshooting

- **`cv2` import error** → install `libgl1`.
- **No audio** → confirm `portaudio19-dev`; test with
  `python -c "import sounddevice as sd; print(sd.query_devices())"`.
- **No voice** → `sudo apt install -y espeak-ng espeak-ng-espeak`.
- **Tamil voice missing** → `espeak-ng --voices=ta` should list a Tamil voice.
- **Dashboard blank** → run `./run.sh` from the repo root (paths resolve relative
  to `backend/app.py`).
- **Photos 404** → ensure `data/photos/` exists (created automatically); the UI
  shows a placeholder avatar otherwise.

## Non-goals (intentionally out of scope)

No facial recognition (staff visually match the photo), no payments, no login,
no cloud — 100% local network, Pi-hosted.
