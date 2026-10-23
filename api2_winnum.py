import requests
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from datetime import datetime

URL      = "http://172.28.80.15/Winnum/views/pages/app/agw.jsp"
USER     = "user"
PASSWORD = "user"
APPID    = "winnum.org.app.WNApplicationInstance:9"
PID      = "winnum.org.product.WNProduct:17"     # станок 9

# какие состояния нас интересуют
WANTED = {"Программа выполняется", "Ожидание резки (по плану)"}

params = {
    "rpc":   "winnum.views.url.WNApplicationTagHelper",
    "men":   "getPriorityTagDuration",
    "appid": APPID,
    "pid":   PID,
    "from":  "2026-10-10",
    "till":  "2026-10-11",
    "mode":  "yes",
    "usr":   USER,
    "pwd":   PASSWORD,
}

r = requests.get(URL, params=params, timeout=60)
r.raise_for_status()

root = ET.fromstring(r.content)
if root.get("status") != "success":
    raise RuntimeError(unquote((root.text or "").strip()))

# парсим и фильтруем
def _dt(s):
    for fmt in ("%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

intervals = []
for it in root.findall("item"):
    a = {k: unquote(v) for k, v in it.attrib.items()}
    tag = a.get("TAG", "")
    if tag not in WANTED:
        continue
    s, e = _dt(a.get("START", "")), _dt(a.get("END", ""))
    if not s or not e:
        continue
    intervals.append({
        "state":   tag,
        "start":   s,
        "end":     e,
        "minutes": round((e - s).total_seconds() / 60, 1),
    })

intervals.sort(key=lambda x: x["start"])

for iv in intervals:
    print(f"{iv['start']:%d.%m %H:%M:%S} → {iv['end']:%H:%M:%S}  "
          f"{iv['minutes']:>5} мин  {iv['state']}")