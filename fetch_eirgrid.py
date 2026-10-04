"""Pull 12 months of 15-min EirGrid Smart Grid Dashboard data (ROI + NI) into raw/*.json."""
import json, urllib.request, datetime as dt, calendar, os
from concurrent.futures import ThreadPoolExecutor
GROUPS = {"a": "windactual,solaractual,co2intensity,windforecast", "d": "demandactual"}  # API returns max 4 fields per call
URL = "https://www.smartgriddashboard.com/api/chart/?region={r}&chartType=default&dateRange=month&dateFrom={a}&dateTo={b}&areas={g}"
fmt = lambda d: d.strftime("%d-%b-%Y")
end = dt.date.today() - dt.timedelta(days=1)
months = []
y, m = end.year - 1, end.month
while (y, m) <= (end.year, end.month):
    a = dt.date(y, m, 1); b = min(dt.date(y, m, calendar.monthrange(y, m)[1]), end)
    months.append((a, b)); y, m = (y + 1, 1) if m == 12 else (y, m + 1)
def get(job):
    r, (a, b), k = job
    out = f"raw/{r}_{k}_{a:%Y%m}.json"
    if os.path.exists(out) and json.load(open(out))["Rows"]: return out  # refetch empty (API flakes)
    with urllib.request.urlopen(URL.format(r=r, a=fmt(a), b=fmt(b), g=GROUPS[k]), timeout=90) as f:
        open(out, "wb").write(f.read())
    return out
os.makedirs("raw", exist_ok=True)
with ThreadPoolExecutor(8) as ex:
    for p in ex.map(get, [(r, mm, k) for r in ("ROI", "NI") for mm in months for k in GROUPS]): print(p)
