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
# 為了避免在沒有硬體的電腦上跑不動，這裡用了 try-except 做軟體模擬防呆
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

# RFID 套件引入
try:
    from mfrc522 import SimpleMFRC522
except Exception:
    SimpleMFRC522 = None

# 藍牙 BLE 套件引入
try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None

# 初始化 RFID 硬體物件
rfid_reader = None
if SimpleMFRC522:
    try:
        rfid_reader = SimpleMFRC522()
        print("[INIT] RFID Reader (RC522) 初始化成功")
    except Exception as e:
        print(f"[WARN] RFID Reader 初始化失敗: {e}")

app = Flask(__name__)

# 路徑設定 (確保資料存在同一資料夾)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'data.json')
data_lock = threading.Lock() # 避免多執行緒同時寫入檔案造成損壞

# ==========================================================
# 資料庫功能 (JSON)
# ==========================================================

def load_data():
    """讀取設定檔，如果不存在就建立預設值"""
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
    """寫入設定檔"""
    with data_lock:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
manual_trigger_event = threading.Event()

# ==========================================================
# 全域參數設定
# ==========================================================
PIR_PIN = 18          # PIR 紅外線接腳
SCAN_DURATION = 10    # 藍牙掃描持續時間
EXIT_RESULT_EXITED = "EXITED"
EXIT_RESULT_NOT_EXIT = "NOT_EXIT"
EXIT_RESULT_CAMERA_ERROR = "CAMERA_ERROR"

MOTION_THRESHOLD = 15000       # 判定移動的像素門檻
MOTION_WARMUP_FRAMES = 15      # 鏡頭暖機幀數
MOTION_CONSECUTIVE_FRAMES = 3  # 連續幾幀移動才算數

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
    """迴圈監聽：等待紅外線訊號或網頁手動觸發"""
    print("[STANDBY] 等待 PIR 觸發中...")
    while True:
        # 1. 檢查網頁手動觸發
        if manual_trigger_event.is_set():
            manual_trigger_event.clear()
            print("⚡ 手動觸發! 進入喚醒流程")
            return

        # 2. 檢查實體 PIR 訊號
        try:
            if GPIO and GPIO.input(PIR_PIN) == 1:
                print("⚡ PIR 觸發! 進入喚醒流程")
                return
        except Exception as e:
            print(f"[ERROR] 讀取 PIR 失敗: {e}")
            time.sleep(1)
        time.sleep(0.2)

# ==========================================================
# 2. 鏡頭: 出門判定 (核心邏輯：背景扣除法)
# ==========================================================
def detect_exit_by_camera(timeout_seconds=5) -> str:
    print("[WAKEUP] 啟動鏡頭, 偵測出門動作中...")
    if cv2 is None:
        print("[ERROR] OpenCV 不可用，跳過鏡頭檢查")
        return EXIT_RESULT_EXITED # 模擬模式直接回傳成功

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] 無法開啟鏡頭")
        return EXIT_RESULT_CAMERA_ERROR

    # 使用 MOG2 演算法去除靜止背景
    back_sub = cv2.createBackgroundSubtractorMOG2()

    # 1. 暖機 (讓演算法適應環境亮度)
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
    
    # 開始計時偵測
    while time.time() - start_time < timeout_seconds:
        ret, frame = cap.read()
        if not ret: break

        # 優化：只取畫面中間 1/3 (通常人走過的路徑)
        h, w = frame.shape[:2]
        roi = frame[:, w // 3 : 2 * w // 3]

        # 取得前景遮罩 (白色=移動, 黑色=背景)
        fg_mask = back_sub.apply(roi)
        # 計算白色點數量
        moving_pixels = int((fg_mask > 0).sum())

        if moving_pixels > MOTION_THRESHOLD:
            consecutive += 1
            # 連續 N 幀都有大動作才算真的出門
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
# 3. RFID 偵測邏輯 (邏輯：讀得到=忘記帶)
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
        # 非阻塞讀取，讀到即回傳 ID
        id_val, text = rfid_reader.read_no_block()
        if id_val:
            print(f"[RFID] 偵測到卡片（ID={id_val}）")
            return True
    except Exception as e:
        print(f"[ERROR] RFID 讀取失敗: {e}")
    return False

def check_rfid_presence(check_times: int = 10) -> bool:
    """多次嘗試，只要有一次讀到就代表東西還在"""
    print(f"[RFID] 開始掃描確認物品 ({check_times}次)...")
    for attempt in range(check_times):
        if read_rfid_once():
            return True # 讀到 = 東西還在 (遺漏)
        time.sleep(0.1)
    return False # 完全沒讀到 = 東西已帶走

# ==========================================================
# 4. 藍牙 (BLE) 偵測邏輯 (邏輯：訊號差值)
# ==========================================================
def analyze_movement(data_points):
    """
    分析演算法：計算 RSSI (訊號強度) 變化
    """
    if len(data_points) < 2: 
        print(f"[BLE] 數據不足 -> 視為沒掃到 (已帶走)")
        return False 
    
    rssis = [x[1] for x in data_points]
    first_rssi = rssis[0]
    last_rssi = rssis[-1]
    
    # 計算頭尾差值
    diff = abs(last_rssi - first_rssi)
    
    print(f"[BLE 分析] 差值: {diff}")

    # 差值小 = 靜止 = 忘記帶
    # 差值大 = 移動中 = 帶走了
    if diff <= 5:
        print(f"=> 訊號穩定 (差值 {diff} <= 5) -> 【遺漏】")
        return True
    else:
        print(f"=> 訊號變動大 (差值 {diff} > 5) -> 【已帶走】")
        return False

async def run_targeted_scan(target_mac):
    """針對特定 MAC 位址進行異步掃描"""
    if BleakScanner is None:
        print("[ERROR] Bleak 未安裝")
        return False

    rssi_data_points = []

    def detection_callback(device, advertisement_data):
        if device.address.upper() == target_mac.upper():
            current_time = time.time()
            rssi = advertisement_data.rssi
            rssi_data_points.append((current_time, rssi))
            # print(f"[BLE] {target_mac} RSSI={rssi}") # Debug用

    print(f"[BLE] 正在搜尋: {target_mac}...")
    scanner = BleakScanner(detection_callback)
    await scanner.start()
    await asyncio.sleep(SCAN_DURATION)
    await scanner.stop()
    
    return analyze_movement(rssi_data_points)

# ==========================================================
# 5. LINE 通知功能
# ==========================================================
_last_notify_time = 0
MIN_NOTIFY_INTERVAL_SECONDS = 15 

def send_line_message(msg_text: str):
    """呼叫 LINE Notify API 推播訊息"""
    global _last_notify_time
    now = time.time()
    
    # 避免短時間重複發送 (防呆)
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
    """系統核心無窮迴圈"""
    while True:
        # 1. 檢查系統是否啟用
        cfg = load_data()
        if not cfg.get("system_enabled", True):
            time.sleep(2)
            continue
        
        # 2. PIR 等待觸發
        wait_pir_trigger()

        # 3. 鏡頭判斷是否出門
        exit_result = detect_exit_by_camera()
        if exit_result != EXIT_RESULT_EXITED:
            print("[INFO] 未偵測到出門，返回待機")
            time.sleep(1)
            continue # 沒出門就回到開頭繼續等 PIR

        print("[INFO] 確認出門，開始檢查物品...")
        now_time = datetime.now().strftime("%H:%M")
        
        # 篩選當下需要檢查的物品 (時間範圍內)
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

        # 4. 逐一檢查物品狀態
        forgotten_items_names = []  # 【修正】這裡需要一個 List 來存被遺忘物品的名稱

        for item in items_to_check:
            item_name = item.get("name", "未命名物品")
            target_mac = item.get("mac", "").strip().upper()            
            is_present = False # True=遺漏(還在), False=已帶走

            # 依據是否有 MAC 位址決定用哪種感測器
            if target_mac == "" or target_mac == "VVVIP ONLY":
                # --- RFID 檢測 ---
                print(f"📡 正在檢查 [RFID] - {item_name}")
                is_present = check_rfid_presence() 
            else:
                # --- 藍牙檢測 ---
                print(f"📡 正在檢查 [BLE] - {item_name} (MAC: {target_mac})")
                try:
                    # 建立臨時 Event Loop 執行異步掃描
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    is_present = loop.run_until_complete(run_targeted_scan(target_mac))
                    loop.close()
                except Exception as e:
                    print(f"[ERROR] BLE 執行錯誤: {e}")
                    is_present = False # 出錯假設為已帶走，避免誤報

            # 【修正關鍵邏輯】如果物品還在 (is_present=True)，加入遺忘清單
            if is_present:
                print(f"❌ 慘了！ {item_name} 忘記帶了！")
                forgotten_items_names.append(item_name)
            else:
                print(f"✅ {item_name} 已帶走")

        # 5. 發送通知 (如果有東西忘記帶)
        if forgotten_items_names:
            names_str = "、".join(forgotten_items_names)
            msg = f"親愛的，您忘記帶 {names_str} 出門了！趕快回家拿！"
            send_line_message(msg)
        else:
            print("[INFO] 太棒了！物品確認全部帶走")
                
        print("[INFO] 流程結束，冷卻 5 秒...")
        time.sleep(5)

# ... (Flask web server 程式碼同原版，略) ...

if __name__ == '__main__':
    setup_pir()
    # 啟動監控執行緒 (Daemon=True 代表主程式結束它也會跟著結束)
    t = threading.Thread(target=main_loop, name="MonitorThread", daemon=True)
    t.start()

    print("🌐 Web Server 啟動中...")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)