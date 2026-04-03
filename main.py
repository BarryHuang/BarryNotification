#!/usr/bin/env python3
import os
import datetime
import json
import urllib.request
import urllib.parse
import ssl

TARGET_DATE = "2026-07-20"
LINE_CLIENT_ID = os.environ.get("LINE_CLIENT_ID")
LINE_CLIENT_SECRET = os.environ.get("LINE_CLIENT_SECRET")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def get_line_token():
    if not LINE_CLIENT_ID or not LINE_CLIENT_SECRET:
        print("Missing LINE credentials in environment variables.")
        return None
    url = "https://api.line.me/v2/oauth/accessToken"
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": LINE_CLIENT_ID,
        "client_secret": LINE_CLIENT_SECRET
    }).encode('utf-8')
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            resp_data = json.loads(response.read().decode('utf-8'))
            return resp_data.get("access_token")
    except Exception as e:
        print(f"Failed to get LINE token: {e}")
        return None

def send_line_broadcast(token, text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = json.dumps({
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }).encode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            print("LINE broadcast sent successfully.")
    except Exception as e:
        print(f"Failed to send LINE message: {e}")

def check_seceda_tickets():
    url = "https://seceda.axess.shop/api/TicketsV4TimeSlotApi/GetReservationTimeSlotsForCalendar"
    
    payload = {
        "ProjNr": "1161",
        "ProductGroupIdentifier": "850",
        "Id": "4351",
        "Month": "7",
        "Year": "2026",
        "CiUppercase": "EN",
        "SubTypeIdentifiers[0][SubTypeIdentifier]": "5303",
        "SubTypeIdentifiers[0][Quantity]": "1",
        "Type": "0"
    }

    data = urllib.parse.urlencode(payload).encode('utf-8')
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest"
    }

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            for day in res_data:
                if day.get("ValidFrom") == f"{TARGET_DATE}T00:00:00":
                    slots = day.get("TimeSlotInfo", [])
                    if slots:
                        capacity = slots[0].get("AvailableSlots", 0)
                        return {"status": "Available" if capacity > 0 else "Sold Out", "capacity": capacity}
                    else:
                        return {"status": "Sold Out", "capacity": 0}
            return {"status": "Date Not Found", "capacity": 0}
            
    except Exception as e:
        print(f"API Error: {e}")
        return None

def main():
    result = check_seceda_tickets()
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    
    if result:
        status_text = f"總剩餘票證容量：{result['capacity']} 張\\n開放狀態：{result['status']}"
    else:
        status_text = "目前取得資料失敗，請以網頁實際狀況為主或稍後再試。"
        
    body = (
        f"📅 報告時間：{today}\\n"
        f"🚠 Seceda 纜車 {TARGET_DATE} 單程票監控 (來自 GitHub Actions)\\n\\n"
        f"【當日總量狀態】\\n"
        f"{status_text}\\n\\n"
        f"🔗 預約網址：https://seceda.axess.shop/en/Products/Tickets/Calendar/1161/850/4351\\n"
        f"🤖 此為自動化播報服務"
    )
    
    print("Execution result:")
    print(body.replace('\\n', '\n'))
    
    line_token = get_line_token()
    if line_token:
        send_line_broadcast(line_token, body.replace('\\n', '\n'))
    else:
        print("Skipping LINE broadcast as tokens are not configured.")

if __name__ == "__main__":
    main()
