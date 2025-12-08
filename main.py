#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import asyncio
import numpy as np
import platform
from datetime import datetime
import json
import os
import threading
from flask import Flask, render_template, request, jsonify

# --- 硬體與環境設定 ---
try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None

try:
    import cv2
except Exception:
    cv2 = None

try:
    import requests
except Exception:
    requests = None

# RFID 套件
try:
    from mfrc522 import SimpleMFRC522
except Exception:
    SimpleMFRC522 = None

# 藍牙套件
try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None

# 初始化 RFID
rfid_reader = None
if SimpleMFRC522:
    try:
        rfid_reader = SimpleMFRC522()
        print("[INIT] RFID Reader (RC522) 初始化成功")
    except Exception as e:
        print(f"[WARN] RFID Reader 初始化失敗: {e}")

app = Flask(__name__)

# 全域設定與路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'data.json')
data_lock = threading.Lock()

# ==========================================================
# 資料庫功能 (JSON)
# ==========================================================

def load_data():
    with data_lock:
        if not os.path.exists(DATA_FILE):
            default = {
                "system_enabled": True,
                "line_token": "",
                "line_user_id": "",
                "items": []
            }
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=2, ensure_ascii=False)
            return default

        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            data = {"system_enabled": True, "items": []}
        return data

def save_data(data):
    with data_lock:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
manual_trigger_event = threading.Event()

# ==========================================================
# 全域常數
# ==========================================================
PIR_PIN = 18  # 請確認您的 PIR 實際腳位
SCAN_DURATION = 10  # 藍牙掃描時間 (秒)

STATE_STANDBY = "STANDBY"
STATE_WAKEUP = "WAKEUP"
STATE_TRACKING = "TRACKING"
STATE_RESET = "RESET"
EXIT_RESULT_EXITED = "EXITED"
EXIT_RESULT_NOT_EXIT = "NOT_EXIT"
EXIT_RESULT_CAMERA_ERROR = "CAMERA_ERROR"

MOTION_THRESHOLD = 15000 
MOTION_WARMUP_FRAMES = 15
MOTION_CONSECUTIVE_FRAMES = 3

# ==========================================================
# 1. PIR: 待機 -> 偵測
# ==========================================================
def setup_pir():
    if GPIO is None: return
    try:
        GPIO.cleanup()
    except:
        pass
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM) 
    GPIO.setup(PIR_PIN, GPIO.IN)
    print(f"[INIT] PIR 設定完成, PIN ={PIR_PIN} (BCM)")

def wait_pir_trigger():
    print("[STANDBY] 等待 PIR 觸發中...")
    while True:
        if manual_trigger_event.is_set():
            manual_trigger_event.clear()
            print("⚡ 手動觸發! 進入喚醒流程")
            return

        try:
            if GPIO and GPIO.input(PIR_PIN) == 1:
                print("⚡ PIR 觸發! 進入喚醒流程")
                return
        except Exception as e:
            print(f"[ERROR] 讀取 PIR 失敗: {e}")
            time.sleep(1)
        time.sleep(0.2)

# ==========================================================
# 2. 鏡頭: 出門判定
# ==========================================================
def detect_exit_by_camera(timeout_seconds=5) -> str:
    print("[WAKEUP] 啟動鏡頭, 偵測出門動作中...")
    if cv2 is None:
        print("[ERROR] OpenCV 不可用")
        return EXIT_RESULT_EXITED # 模擬模式直接回傳成功

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] 無法開啟鏡頭")
        return EXIT_RESULT_CAMERA_ERROR

    back_sub = cv2.createBackgroundSubtractorMOG2()

    # 1. 暖機
    for i in range(MOTION_WARMUP_FRAMES):
        ret, frame = cap.read()
        if not ret:
            cap.release()
            return EXIT_RESULT_CAMERA_ERROR
        back_sub.apply(frame)

    print(f"[MAKEUP] 暖機完成")

    start_time = time.time()
    moved = False
    consecutive = 0
    
    while time.time() - start_time < timeout_seconds:
        ret, frame = cap.read()
        if not ret: break

        # 取畫面中間 1/3
        h, w = frame.shape[:2]
        roi = frame[:, w // 3 : 2 * w // 3]

        fg_mask = back_sub.apply(roi)
        moving_pixels = int((fg_mask > 0).sum())

        if moving_pixels > MOTION_THRESHOLD:
            consecutive += 1
            if consecutive >= MOTION_CONSECUTIVE_FRAMES:
                print("偵測到連續移動, 視為『出門』")
                moved = True
                break
        else:
            consecutive = 0 

    cap.release()
    if moved:
        return EXIT_RESULT_EXITED
    return EXIT_RESULT_NOT_EXIT

# ==========================================================
# 3. RFID 偵測邏輯
# ==========================================================
def read_rfid_once() -> bool:   
    global rfid_reader
    if SimpleMFRC522 is None: return False
    
    if rfid_reader is None:
        try:
            rfid_reader = SimpleMFRC522()
        except:
            return False

    try:
        # read_no_block 非阻塞讀取
        id_val, text = rfid_reader.read_no_block()
        if id_val:
            print(f"[RFID] 偵測到卡片（ID={id_val}）")
            return True
    except Exception as e:
        print(f"[ERROR] RFID 讀取失敗: {e}")
    return False

def check_rfid_presence(check_times: int = 10) -> bool:
    """多次嘗試讀取 RFID 標籤"""
    print(f"[RFID] 開始掃描確認物品 ({check_times}次)...")
    for attempt in range(check_times):
        if read_rfid_once():
            return True # 有讀到 = 東西還在 (遺漏)
        time.sleep(0.1)
    return False # 沒讀到 = 東西不在 (已帶走)

# ==========================================================
# 4. 藍牙 (BLE) 偵測邏輯
# ==========================================================
def analyze_movement(data_points):
    """
    分析演算法：計算 RSSI 頭尾差值
    回傳: True (還在/遺漏), False (已遠離/帶走)
    """
    if len(data_points) < 2: 
        print(f"[BLE] 數據不足 ({len(data_points)}筆) -> 視為沒掃到 (已帶走)")
        return False 
    
    # 取得 RSSI 列表
    rssis = [x[1] for x in data_points]
    
    first_rssi = rssis[0]
    last_rssi = rssis[-1]
    
    # 計算絕對差值
    diff = abs(last_rssi - first_rssi)
    
    print(f"[BLE 分析] 第一筆: {first_rssi}, 最後一筆: {last_rssi}, 絕對差值: {diff}")

    # 判斷邏輯：
    # 如果變動幅度 <= 5 => 訊號穩定 => 東西還在 (True)
    # 如果變動幅度 > 5 => 正在移動 => 東西帶走了 (False)
    
    if diff <= 5:
        print(f"=> 判定結果：訊號穩定 (差值 {diff} <= 5) -> 【遺漏】")
        return True
    else:
        print(f"=> 判定結果：訊號變動大 (差值 {diff} > 5) -> 【已帶走】")
        return False

async def run_targeted_scan(target_mac):
    """針對特定 MAC 進行掃描"""
    if BleakScanner is None:
        print("[ERROR] Bleak 未安裝")
        return False

    rssi_data_points = []

    def detection_callback(device, advertisement_data):
        if device.address.upper() == target_mac.upper():
            current_time = time.time()
            rssi = advertisement_data.rssi
            rssi_data_points.append((current_time, rssi))
            print(f"[BLE] {target_mac} RSSI={rssi}")

    print(f"[BLE] 正在搜尋: {target_mac} ({SCAN_DURATION}秒)...")
    scanner = BleakScanner(detection_callback)
    await scanner.start()
    await asyncio.sleep(SCAN_DURATION)
    await scanner.stop()
    
    print(f"[BLE] 掃描結束，收集 {len(rssi_data_points)} 筆資料")
    return analyze_movement(rssi_data_points)

# ==========================================================
# 5. LINE 通知功能
# ==========================================================
_last_notify_time = 0
MIN_NOTIFY_INTERVAL_SECONDS = 15 

def send_line_message(msg_text: str):
    global _last_notify_time
    now = time.time()
    
    if now - _last_notify_time < MIN_NOTIFY_INTERVAL_SECONDS:
        print(f"[LINE] 節流中，跳過此通知")
        return

    data = load_data()
    token = data.get("line_token")
    user_id = data.get("line_user_id")

    if not token or not user_id:
        print("[LINE] Token 未設定")
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "to": user_id,
        "messages": [{"type": "text", "text": msg_text}]
    }
    try:
        requests.post(url, headers=headers, json=body)
        print(f"[LINE] 訊息已發送: {msg_text}")
        _last_notify_time = time.time()
    except Exception as e:
        print(f"[ERROR] LINE 發送失敗：{e}")

# ==========================================================
# 主流程 (監控迴圈)
# ==========================================================
def main_loop():
    while True:
        # 1. 讀取設定
        cfg = load_data()
        if not cfg.get("system_enabled", True):
            time.sleep(2)
            continue
        
        # 2. 等待觸發
        wait_pir_trigger()

        # 3. 鏡頭偵測
        exit_result = detect_exit_by_camera()
        if exit_result != EXIT_RESULT_EXITED:
            print("[INFO] 未偵測到出門，返回待機")
            time.sleep(1)
            continue

        print("[INFO] 確認出門，開始檢查物品...")
        now_time = datetime.now().strftime("%H:%M")
        
        items_to_check = []
        for item in cfg.get("items", []):
            enabled = item.get("enabled", True)
            start_t = item.get("start_time", "00:00")
            end_t = item.get("end_time", "23:59")

            if enabled and (start_t <= now_time <= end_t):
                items_to_check.append(item)

        if not items_to_check:
            print("[INFO] 無需檢查的物品")
            time.sleep(2)
            continue

        # 4. 逐一檢查物品 (RFID vs BLE)
        forgotten_items = []

        for item in items_to_check:
            target_mac = item.get("mac", "").strip().upper()            
            is_present = False # True=遺漏(還在), False=已帶走

            # --- [關鍵邏輯] 判斷是否有 MAC ---
            if target_mac == "" or target_mac == "VVVIP ONLY": # 空值或預設文字視為無MAC
                # 使用 RFID
                print(f"📡 正在檢查 [RFID] ")
                is_present = check_rfid_presence() 
            else:
                # 使用 藍牙
                print(f"📡 正在檢查 [BLE]  (MAC: {target_mac})")
                try:
                    # 建立獨立的 asyncio loop 來執行藍牙掃描
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    is_present = loop.run_until_complete(run_targeted_scan(target_mac))
                    loop.close()
                except Exception as e:
                    print(f"[ERROR] BLE 執行錯誤: {e}")
                    is_present = False # 出錯視為沒掃到(帶走)

        # 5. 發送通知
        if is_present:
            names_str = "、".join(forgotten_items)
            msg = f"親愛的，您忘記帶 {names_str} 跟忘忘仙貝出門了！"
            send_line_message(msg)
        else:
            print("[INFO] 物品確認全部帶走")
                
        print("[INFO] 流程結束，冷卻 5 秒...")
        time.sleep(5)

# =========================================================
# Flask 網頁介面
# ==========================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data', methods=['GET', 'POST'])
def api_data():
    if request.method == 'POST':
        try:
            new_data = request.json
            save_data(new_data)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
    return jsonify(load_data())

@app.route('/api/trigger', methods=['POST'])
def api_trigger():
    manual_trigger_event.set()
    return jsonify({"status": "triggered"})

# =========================================================
# 入口點
# ==========================================================
if __name__ == '__main__':
    setup_pir()
    
    if SimpleMFRC522:
        print("[INFO] RFID 模組已載入")
    else:
        print("[WARN] RFID 模組未安裝")

    t = threading.Thread(target=main_loop, name="MonitorThread", daemon=True)
    t.start()

    print("🌐 Web Server 啟動中...")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)