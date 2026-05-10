import urllib.request
import urllib.parse
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

url = "https://shop-ks.seenschifffahrt.de/ajax/onlinereservierungv4.php"
payload = {
    "aktion": "fahrplan_load",
    "datum": "2026-07-01",
    "station_start": "1",
    "station_stop": "2",
    "lang": "en"
}
data = urllib.parse.urlencode(payload).encode('utf-8')
headers = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

req = urllib.request.Request(url, data=data, headers=headers)
try:
    with urllib.request.urlopen(req, context=ctx) as response:
        html = response.read().decode('utf-8')
        print(html)
except Exception as e:
    print(f"Error: {e}")
