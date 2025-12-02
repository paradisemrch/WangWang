#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import os
import threading
import random
import requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify

# --- 硬體與環境設定 (自動切換模擬模式) ---
try:
    import RPi.GPIO as GPIO
    SIMULATION_MODE = False
except ImportError:
    GPIO = None
    SIMULATION_MODE = True
    print("[INFO] RPi.GPIO 未安裝，使用模擬模式")

try:
    import cv2
except ImportError:
    cv2 = None
    print("[INFO] OpenCV 未安裝，使用模擬相機")

# --- RFID 序列埠通訊模組 ---
try:
    import serial
except ImportError:
    serial = None
    print("[INFO] pyserial 未安裝，使用模擬 RFID 掃描 (請執行 pip install pyserial)")

# 全域設定與路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'data.json')

# 硬體腳位與設定
PIR_PIN = 18
RFID_PORT = '/dev/ttyUSB0'  # [設定] 請確認樹莓派上的 USB 裝置名稱
RFID_BAUD = 115200          # [設定] 請確認讀卡機的波特率

app = Flask(__name__)
lock = threading.Lock()

# ==========================================================
# 資料庫功能 (JSON)
# ==========================================================

def load_data():
    with lock:
        if not os.path.exists(DATA_FILE):
            return {"system_enabled": True, "items": [], "line_token": "", "line_user_id": ""}
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {"system_enabled": True, "items": []}

def save_data(data):
    with lock:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

# ==========================================================
# [核心邏輯] 感測與通知
# ==========================================================

def setup_gpio():
    if not SIMULATION_MODE and GPIO:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(PIR_PIN, GPIO.IN)

def send_line_notify(token, user_id, message):
    """發送 LINE Push Message"""
    if not token or not user_id: return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    body = {"to": user_id, "messages": [{"type": "text", "text": message}]}
    try:
        requests.post(url, headers=headers, json=body, timeout=5)
        print("[LINE] 通知已發送")
    except Exception as e:
        print(f"[LINE] 發送失敗: {e}")

def check_camera_motion():
    """相機動態偵測 (判定是否有人移動/出門)"""
    if cv2 is None: return True # 模擬有人
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return False
    
    back_sub = cv2.createBackgroundSubtractorMOG2()
    motion = False
    start = time.time()
    
    # 偵測 3 秒內的變化
    while time.time() - start < 3:
        ret, frame = cap.read()
        if not ret: break
        fg = back_sub.apply(frame)
        if (fg > 0).sum() > 5000: # 閾值可調
            motion = True
            break
    cap.release()
    return motion

# --- RFID 掃描函式 ---
def scan_rfid_tags(scan_time=3):
    """
    開啟 USB Port 掃描 RFID Tags。
    回傳: 所有掃到的 Tag ID 集合 (Set)
    """
    detected = set()
    
    # 模擬模式
    if serial is None or SIMULATION_MODE:
        time.sleep(scan_time)
        # 模擬：隨機掃到一些 ID
        test_data = load_data()
        for item in test_data.get('items', []):
            if random.random() > 0.5: # 50% 機率模擬物品還在
                detected.add(item.get('mac', ''))
        return detected

    # 真實硬體模式
    try:
        ser = serial.Serial(RFID_PORT, RFID_BAUD, timeout=0.1)
        end = time.time() + scan_time
        print(f"[RFID] 開始掃描 ({scan_time}秒)...")
        
        while time.time() < end:
            if ser.in_waiting > 0:
                raw = ser.read(ser.in_waiting)
                # 假設 Reader 回傳 HEX 格式 (依您的硬體調整)
                try:
                    hex_str = raw.hex().upper()
                    # 簡單過濾可能包含的換行符號或雜訊
                    hex_str = hex_str.strip()
                    if hex_str:
                        detected.add(hex_str)
                except:
                    pass
        ser.close()
    except Exception as e:
        print(f"[RFID] 讀取錯誤: {e}")
    
    return detected

# ==========================================================
# 監控執行緒 (Background Loop)
# ==========================================================

def monitor_loop():
    setup_gpio()
    print("🚀 忘忘仙貝監控服務啟動...")

    while True:
        try:
            data = load_data()
            if not data.get("system_enabled", True):
                time.sleep(2)
                continue

            # 1. PIR 觸發檢查
            triggered = False
            if not SIMULATION_MODE and GPIO:
                if GPIO.input(PIR_PIN) == 1:
                    triggered = True
                    print("⚡ PIR 感應觸發")
            
            # Web 手動測試觸發
            if getattr(threading.current_thread(), "force_trigger", False):
                triggered = True
                threading.current_thread().force_trigger = False
                print("⚡ 手動觸發測試")

            if triggered:
                # 2. 相機確認出門
                if check_camera_motion():
                    print("📷 出門事件確認")
                    
                    # 3. 篩選目前時段需檢查的物品
                    now = datetime.now().strftime("%H:%M")
                    check_list = {} # { "ID_KEY": "鑰匙", ... }

                    for item in data.get("items", []):
                        if item.get("enabled"):
                            start = item.get("start_time", "00:00")
                            end = item.get("end_time", "23:59")
                            if start <= now <= end:
                                # 移除冒號並轉大寫，以利比對
                                mac = item.get("mac", "").replace(":", "").upper()
                                if mac:
                                    check_list[mac] = item["name"]
                    
                    if check_list:
                        # 4. 掃描 RFID
                        print(f"📡 正在檢查清單: {list(check_list.values())}")
                        scanned_tags = scan_rfid_tags(3)
                        
                        # 將所有掃到的數據轉成一個大字串，方便做 'in' 比對
                        full_scan_str = "".join(scanned_tags)
                        missing = []
                        
                        # 5. 判斷邏輯：如果 Tag ID 出現在掃描結果 -> 東西還在 -> 遺漏
                        for tag_id, name in check_list.items():
                            if tag_id in full_scan_str:
                                print(f"⚠️ 發現: {name} (還在感應區)")
                                missing.append(name)
                            else:
                                print(f"✅ 未發現: {name} (已帶走)")
                        
                        # 6. 警報與通知 (僅保留 LINE)
                        if missing:
                            msg = f"⚠️ 忘忘仙貝提醒：\n您剛出門，但感測器偵測到以下物品還在原位：\n👉 " + "、".join(missing)
                            send_line_notify(data.get("line_token"), data.get("line_user_id"), msg)
                        else:
                            print("✅ 物品全數帶走")
                    
                    time.sleep(10) # 冷卻時間避免連續觸發

            time.sleep(0.5)

        except Exception as e:
            print(f"❌ 監控錯誤: {e}")
            time.sleep(1)

# ==========================================================
# Web API
# ==========================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data', methods=['GET', 'POST'])
def handle_data():
    if request.method == 'POST':
        try:
            save_data(request.json)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)}), 500
    else:
        return jsonify(load_data())

@app.route('/api/trigger', methods=['POST'])
def manual_trigger():
    for t in threading.enumerate():
        if t.name == "MonitorThread":
            t.force_trigger = True
            return jsonify({"status": "triggered"})
    return jsonify({"status": "error"})

if __name__ == '__main__':
    # 啟動監控執行緒
    t = threading.Thread(target=monitor_loop, name="MonitorThread", daemon=True)
    t.start()
    # 啟動 Web Server
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)