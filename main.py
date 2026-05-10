#!/usr/bin/env python3
import os
import datetime
import json
import re
import urllib.request
import urllib.parse
import ssl

# === Config ===
SECEDA_TARGET_DATE = "2026-07-20"
PARKING_TARGET_DATE = "2026-07-21"
KONIGSSEE_TARGET_DATES = ["2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22", "2026-07-29"]

LINE_CLIENT_ID = os.environ.get("LINE_CLIENT_ID")
LINE_CLIENT_SECRET = os.environ.get("LINE_CLIENT_SECRET")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# === LINE Messaging ===

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


# === Seceda Cable Car Ticket Monitor ===

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
                if day.get("ValidFrom", "").startswith(SECEDA_TARGET_DATE):
                    slots = day.get("TimeSlotInfo", [])
                    if slots:
                        capacity = slots[0].get("AvailableSlots", 0)
                        return {"status": "Available" if capacity > 0 else "Sold Out", "capacity": capacity}
                    else:
                        return {"status": "Sold Out", "capacity": 0}
            return {"status": "Date Not Found", "capacity": 0}

    except Exception as e:
        print(f"Seceda API Error: {e}")
        return None


# === Parking Zans (Odles Dolomites) Monitor ===

def check_parking_zans():
    """
    Check parking availability at Parking Zans (Val di Funes / Villnöss)
    via the LTS Shop Widget API at apps.lts.it.

    The API returns HTML containing date entries with availability info.
    We parse the HTML to extract the target date's status.
    """
    url = "https://apps.lts.it/cart/lts-widget/parse-request"

    # Determine the month start for the query
    target_month_start = PARKING_TARGET_DATE[:8] + "01"  # e.g. "2026-07-01"

    params = {
        "data[data][posid]": "B5DA4AD9952440B6A4B54351CFC5A328",
        "data[data][eventid]": "BBF4CDE820A14B16A5EA28AB3FCF697F",
        "data[data][language]": "en",
        "data[data][from]": target_month_start,
        "data[data][scrolltovariants]": "true",
        "data[data][maincolor]": "#513c28",
        "data[referrer]": "www.odlesdolomites.com",
        "data[main_container_id]": "lts-wc-id-monitor",
        "url": "site/index"
    }

    data = urllib.parse.urlencode(params).encode('utf-8')
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://www.odlesdolomites.com",
        "Referer": "https://www.odlesdolomites.com/"
    }

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            res = json.loads(response.read().decode('utf-8'))

        # Debug: show top-level keys in response
        print(f"[DEBUG] Parking API response keys: {list(res.keys())}")

        # The HTML is in domContent under the container ID key
        dom_key = "#lts-wc-id-monitor"
        dom_content = res.get("domContent", {})
        print(f"[DEBUG] domContent keys: {list(dom_content.keys())}")
        html = dom_content.get(dom_key, "")

        if not html:
            print("Parking API: No HTML content in response.")
            return None

        print(f"[DEBUG] HTML length: {len(html)} chars")

        # Debug: find the snippet around the target date
        date_idx = html.find(PARKING_TARGET_DATE)
        if date_idx == -1:
            print(f"[DEBUG] Target date '{PARKING_TARGET_DATE}' not found in HTML.")
            print(f"[DEBUG] HTML preview (first 500 chars): {html[:500]}")
        else:
            print(f"[DEBUG] Found target date at index {date_idx}. Snippet: {html[date_idx:date_idx+300]}")

        # Parse the target date entry from the HTML
        # Pattern: data-date="2026-07-21" ... Available tickets: 10+ (or Sold out / Expired)
        pattern = (
            rf'data-date="{re.escape(PARKING_TARGET_DATE)}"'
            r'.*?(?:Available tickets:\s*(\d+\+?)|Sold out|Expired)'
        )
        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)

        if match:
            available_str = match.group(1)
            matched_text = match.group(0)

            if available_str:
                return {"status": "Available", "available": available_str}
            elif re.search(r'sold out', matched_text, re.IGNORECASE):
                return {"status": "Sold Out", "available": "0"}
            elif re.search(r'expired', matched_text, re.IGNORECASE):
                return {"status": "Expired", "available": "0"}
        else:
            print(f"[DEBUG] Regex did not match. Pattern was: {pattern}")

        return {"status": "Date Not Found", "available": "N/A"}

    except Exception as e:
        print(f"Parking API Error: {e}")
        return None


# === Königssee Ticket Monitor ===

def check_konigssee_tickets(date_str):
    url = "https://shop-ks.seenschifffahrt.de/ajax/onlinereservierungv4.php"
    payload = {
        "aktion": "fahrplan_load",
        "datum": date_str,
        "station_start": "1",  # Seelände
        "station_stop": "3",   # Salet (Full route)
        "lang": "en"
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            res = json.loads(response.read().decode('utf-8'))
            if not res.get("success"):
                return {"status": "Error", "total_frei": 0}
            
            fahrplan = res.get("fahrplan", [])
            if not fahrplan:
                return {"status": "No Schedule", "total_frei": 0}
            
            total_frei = sum(f.get("frei", 0) for f in fahrplan if f.get("frei", 0) > 0)
            status = "Available" if total_frei > 0 else "Sold Out"
            return {"status": status, "total_frei": total_frei}
    except Exception as e:
        print(f"Königssee API Error for {date_str}: {e}")
        return None


# === Main ===

def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    # --- Seceda cable car ---
    seceda_result = check_seceda_tickets()
    if seceda_result:
        seceda_text = (
            f"總剩餘票證容量：{seceda_result['capacity']} 張\n"
            f"開放狀態：{seceda_result['status']}"
        )
    else:
        seceda_text = "⚠️ 取得資料失敗，請以網頁實際狀況為主。"

    # --- Parking Zans ---
    parking_result = check_parking_zans()
    if parking_result:
        parking_text = (
            f"可預約車位：{parking_result['available']}\n"
            f"開放狀態：{parking_result['status']}"
        )
    else:
        parking_text = "⚠️ 取得資料失敗，請以網頁實際狀況為主。"

    # --- Königssee ---
    konigssee_text_lines = []
    for d in KONIGSSEE_TARGET_DATES:
        res = check_konigssee_tickets(d)
        if res:
            konigssee_text_lines.append(f"  - {d}: {res['status']} (剩餘 {res['total_frei']} 張)")
        else:
            konigssee_text_lines.append(f"  - {d}: ⚠️ 取得資料失敗")
    konigssee_text = "\n".join(konigssee_text_lines)

    # --- Compose notification ---
    body = (
        f"📅 報告時間：{today}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"🚠 Seceda 纜車 {SECEDA_TARGET_DATE} 單程票\n"
        f"【當日總量狀態】\n"
        f"{seceda_text}\n"
        f"🔗 https://seceda.axess.shop/en/Products/Tickets/Calendar/1161/850/4351\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"🅿️ Parking Zans 停車位 {PARKING_TARGET_DATE}\n"
        f"【當日預約狀態】\n"
        f"{parking_text}\n"
        f"🔗 https://www.odlesdolomites.com/en/events/BBF4CDE820A14B16A5EA28AB3FCF697F\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"⛴️ Königssee 國王湖船票 (Seelände -> Salet)\n"
        f"【各日期剩餘總票數】\n"
        f"{konigssee_text}\n"
        f"🔗 https://shop-ks.seenschifffahrt.de/?lang=en\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 此為自動化播報服務 (GitHub Actions)"
    )

    print("Execution result:")
    print(body)

    line_token = get_line_token()
    if line_token:
        send_line_broadcast(line_token, body)
    else:
        print("Skipping LINE broadcast as tokens are not configured.")


if __name__ == "__main__":
    main()
