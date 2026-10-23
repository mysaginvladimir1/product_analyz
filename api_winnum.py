import requests
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from lxml import etree
from urllib.parse import unquote


BASE_URL = "http://172.28.80.15/Winnum/views/pages/app/agw.jsp"
USER = "user"
PASSWORD = "user"

def winnum_request(rpc: str, men: str, appid: str, pid: str,
                   date_from: str, date_till: str, mode: str = "yes", 
                   user=USER, password=PASSWORD, timeout: int = 30) -> str:
    """
    Универсальный вызов Winnum.
    requests сам закодирует пробелы, ':', '&' и т.п. в query-string.
    """
    params = {
        "rpc":  rpc,
        "men":  men,
        "appid": appid,
        "pid":  pid,
        "from": date_from,
        "till": date_till,
        "mode": mode,
        "usr": user,
        "pwd": password,
    }
    r = requests.get(BASE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    """print("Длина ответа:", len(r.text))
    print("Первые 200 символов:", repr(r.text[:200]))
    print("Последние 200 символов:", repr(r.text[-200:]))
    print("Строки 30–45:")"""
    for i, line in enumerate(r.text.splitlines()[29:45], start=30):
        print(f"{i:3}: {line!r}")
    return r.text
    

# ---------------------------------------------------------------
# 1) Данные о работе операторов на оборудовании
# ---------------------------------------------------------------
xml1 = winnum_request(
    rpc="winnum.views.url.WNCNCApplicationOperatorHelper",
    men="getOperatorsLoading",
    appid="winnum.org.app.WNApplicationInstance:9",
    pid="winnum.org.product.WNProduct:17", #станок 9
    date_from="2026-10-04",
    date_till="2026-10-04",
)

root = ET.fromstring(xml1)
for item in root.findall("item"):
    operator = unquote(item.get("operator", ""))   # ← раскодируем кириллицу
    minutes = int(item.get("value")) / 60_000

    print(f"{operator}: {minutes:.1f} мин")
    
# 2) Суммарное время операций
# ---------------------------------------------------------------
"""""
params_summary = {
    "rpc":   "winnum.views.url.WNCNCApplicationCompletedQtyHelper",
    "men":   "getOperationsSummary",
    "appid": "winnum.org.app.WNApplicationInstance:9",
    "pid":   "winnum.org.product.WNProduct:17",   # станок 9
    "from":  "2026-10-04 08:00:00",
    "till":  "2026-10-04 20:00:00",
    "mode":  "yes",
    "usr":   USER,
    "pwd":   PASSWORD,
}

r2 = requests.get(BASE_URL, params=params_summary, timeout=30)
r2.raise_for_status()

print("\nСуммарное время операций:")
print(r2.text)


# ---------- 3) Расчёт длительности выполнения тегов с приоритетностью ----------
params_tags = {
    "rpc":      "winnum.views.url.WNApplicationTagHelper",
    "men":      "getPriorityTagDuration",
    "appid":    "winnum.org.app.WNApplicationInstance:9",
    "pid":      "winnum.org.product.WNProduct:17",   # станок 9
    "from":     "2026-10-04",
    "till":     "2026-10-04",
    "mode":     "yes",
    "usr":      USER,
    "pwd":      PASSWORD,
    "tags":     "NC_WIP",
    # опциональные:
    # "rounding": "60",       # округление (в тех же единицах, что и результат)
    # "onlyLast": "yes",      # только последние интервалы
    # "tags":     "TAG1,TAG2",# свои теги вместо тегов приложения
}

r3 = requests.get(BASE_URL, params=params_tags, timeout=30)
r3.raise_for_status()

print("\nДлительность тегов с приоритетом:")
print(r3.text)
"""""
"""""
# ---------- 4) Расчет времени выполнения или не выполнения тега за указанный период времени ----------
params_tags = {
    "rpc":      "winnum.views.url.WNApplicationTagHelper",
    "men":      "getTagIntervalCalculation",
    "appid":    "winnum.org.app.WNApplicationInstance:9",
    "pid":      "winnum.org.product.WNProduct:17",   # станок 9
    "tid":      "winnum.org.tag.WNTag:32",
    "from":     "2026-10-04 08:00:00",
    "till":     "2026-10-04 08:01:00",
    "timeout":  "30",
    "check_success":    "false",
    "usr":      USER,
    "pwd":      PASSWORD,
    

}

r4 = requests.get(BASE_URL, params=params_tags, timeout=30)
r4.raise_for_status()

print("\nСтанок включен:")
print(r4.text)
"""""
# ---------- 5) Время работы оператора с разбивкой по сменам ----------
params = {
    "rpc":    "winnum.views.url.WNConnectorHelper",
    "men":    "getSignal",
    "uuid":   "BE6D22A6-5613-4D24-A366-9CAACBD7E175",
    "signal": "A212",
    "stype":  "bytime",
    "start":  "2026-10-12 08:00:00",
    "end":    "2026-10-12 08:10:00",
    "order":  "desc",
    "mode":   "yes",
    "usr":    USER,
    "pwd":    PASSWORD,
}

r = requests.get(BASE_URL, params=params, timeout=30)
r.raise_for_status()
root = ET.fromstring(r.content)
print("Оператор на станке 3.3.3 ")
for item in root.findall("item"):
    a = {k: unquote(v) for k, v in item.attrib.items()}
    print(f"{a['event_time']:25}  [{a['signal_id']}]  {a['value']}")

