"""Turn raw EirGrid + CSO data into web/data.js for the GridSense dashboard.

Model (hackathon-grade, every assumption is named):
  * System data (demand, wind, solar, CO2 intensity, wind forecast) = REAL EirGrid 15-min data, ROI + NI.
  * Area demand = system demand split by REAL CSO MEC03 metered consumption per county / Dublin postal district (ROI)
    or NISRA population (NI), plus a flat data-centre baseload in ROI sized from CSO MEC02 and sited where DCs cluster.
  * Baseline renewable % per area = hourly-matched: sum_h load_a(h) * RES(h) / sum_h load_a(h).
  * GridSense = forecast-led routing. Each day: forecast every area's hourly load (same hour-of-week mean over
    the trailing 8 weeks, trend-adjusted), rank areas by forecast peak, and route recovered wind to them.
    Pool = ROUTE_FRAC of the day's dispatched-down wind (est. = wind forecast - actual when RES >= 60%):
    the network-constraint share, recoverable when transmission + storage are scheduled against the forecast.
    The pool goes to the highest-forecast-peak areas first, capped at their fossil load in the PEAK_HOURS.
"""
import json, glob, datetime as dt, collections, statistics as st

# ---------- areas: (name in geojson, display, region, population, dc_weight, wind_weight, solar_weight)
# ponytail: populations ~Census 2022 / NISRA 2022; wind/solar weights ~ installed capacity by county (EirGrid/SONI connected lists). Swap for exact per-node data when ESB Networks/SONI share it.
A = [
 ("Carlow County","Carlow","ROI",61968,0,1,4),("Cavan County","Cavan","ROI",81704,0,5,1),("Clare County","Clare","ROI",127938,3,14,2),
 ("Cork City","Cork City","ROI",224004,2,0,1),("Cork County","Cork County","ROI",360152,3,40,8),("Donegal County","Donegal","ROI",167084,0,30,1),
 ("Dublin City","Dublin City","ROI",592713,18,0,1),("Dún Laoghaire-Rathdown","Dún Laoghaire-Rathdown","ROI",233860,2,0,1),
 ("Fingal","Fingal","ROI",330506,35,0,2),("Galway City","Galway City","ROI",83456,0,0,0),("Galway County","Galway County","ROI",193975,0,28,2),
 ("Kerry County","Kerry","ROI",156458,0,38,1),("Kildare County","Kildare","ROI",247774,4,1,6),("Kilkenny County","Kilkenny","ROI",104160,0,5,4),
 ("Laois County","Laois","ROI",91877,0,6,4),("Leitrim County","Leitrim","ROI",35199,0,9,0),("Limerick City","Limerick City","ROI",102000,0,0,0),
 ("Limerick County","Limerick County","ROI",107536,0,12,3),("Longford County","Longford","ROI",46751,0,5,1),("Louth County","Louth","ROI",139703,0,1,3),
 ("Mayo County","Mayo","ROI",137970,0,20,1),("Meath County","Meath","ROI",220826,14,1,9),("Monaghan County","Monaghan","ROI",65288,0,3,1),
 ("North Tipperary","North Tipperary","ROI",70000,0,12,3),("Offaly County","Offaly","ROI",83150,0,16,3),("Roscommon County","Roscommon","ROI",70259,0,9,1),
 ("Sligo County","Sligo","ROI",70198,0,6,0),("South Dublin","South Dublin","ROI",301075,19,0,1),("South Tipperary","South Tipperary","ROI",97895,0,12,4),
 ("Waterford City","Waterford City","ROI",60000,0,0,1),("Waterford County","Waterford County","ROI",67363,0,4,5),("Westmeath County","Westmeath","ROI",96221,0,2,2),
 ("Wexford County","Wexford","ROI",163919,0,7,10),("Wicklow County","Wicklow","ROI",155851,0,5,2),
 ("Antrim and Newtownabbey","Antrim & Newtownabbey","NI",145000,0,6,2),("East Coast","Ards & North Down","NI",164000,0,3,1),
 ("Armagh Banbridge and Craigavon","Armagh, Banbridge & Craigavon","NI",220000,0,4,3),("Belfast","Belfast","NI",348000,0,0,1),
 ("Causeway Coast and Glens","Causeway Coast & Glens","NI",142000,0,20,1),("Derry and Strabane","Derry & Strabane","NI",151000,0,30,1),
 ("Fermanagh and Omagh","Fermanagh & Omagh","NI",118000,0,28,1),("Lisburn and Castlereagh","Lisburn & Castlereagh","NI",150000,0,1,2),
 ("Mid Ulster","Mid Ulster","NI",152000,0,14,2),("Mid and East Antrim","Mid & East Antrim","NI",140000,0,12,1),("Newry Mourne and Down","Newry, Mourne & Down","NI",183000,0,6,2),
]
ROUTE_FRAC, PEAK_HOURS, GREEN_RES = 0.5, 6, 0.6  # ponytail: ~half of Irish dispatch-down is network constraints (EirGrid annual reports); calibrate per node when SONI/EirGrid share it

# ---------- CSO MEC02: real data-centre share of ROI metered electricity (last 4 quarters)
cso = json.load(open("raw/cso_mec02.json"))
q = list(cso["dimension"]["TLIST(Q1)"]["category"]["index"]); n = len(q); v = cso["value"]
allc, dcc = v[0::3], v[1::3]  # cube order: quarter x consumer-type
DC_FRAC = sum(dcc[-4:]) / sum(allc[-4:])
CSO = {"quarters": q, "all": allc, "dc": dcc}
print(f"CSO MEC02: data centres = {DC_FRAC:.1%} of ROI metered electricity ({q[-4]}..{q[-1]})")

# ---------- CSO MEC03: real metered consumption per county + Dublin postal district (latest year, all sectors).
# "Not coded" (~30%, almost all non-residential: data centres + large users) is left out; MEC02 places the DC part.
m3 = json.load(open("raw/cso_mec03.json")); yrs3 = list(m3["dimension"]["TLIST(A1)"]["category"]["index"])
geo3 = m3["dimension"]["C03815V04565"]["category"]; gi3 = list(geo3["index"]); sz = m3["size"]
MEC = {geo3["label"][g]: m3["value"][(len(yrs3) - 1) * sz[2] * sz[3] + i * sz[3]] for i, g in enumerate(gi3)}
# ponytail: postal districts -> local authorities by where most of each district sits; "Co. Dublin" (non-district
# addresses) and city/county pairs (Cork, Galway, Limerick, Waterford, Tipperary N/S) split by population.
D_CITY = [1, 2, 3, 4, 5, 6, "6W", 7, 8, 9, 10, 11, 12, 13, 17, 20]
DUB = {"Dublin City": sum(MEC[f"Dublin {d}"] for d in D_CITY), "Fingal": MEC["Dublin 15"],
       "South Dublin": MEC["Dublin 22"] + MEC["Dublin 24"] + MEC["Dublin 16"] / 2,
       "Dún Laoghaire-Rathdown": MEC["Dublin 14"] + MEC["Dublin 18"] + MEC["Dublin 16"] / 2}
CO_OF = {"Cork City": "Cork", "Cork County": "Cork", "Galway City": "Galway", "Galway County": "Galway", "Limerick City": "Limerick",
         "Limerick County": "Limerick", "Waterford City": "Waterford", "Waterford County": "Waterford", "North Tipperary": "Tipperary",
         "South Tipperary": "Tipperary", "Dún Laoghaire-Rathdown": "Dublin", "Fingal": "Dublin", "South Dublin": "Dublin"}
def mec_gwh(a):
    co = CO_OF.get(a[1], a[1].replace(" County", ""))
    sib = [b for b in A if CO_OF.get(b[1], b[1].replace(" County", "")) == co and b[2] == "ROI"]
    if a[1] == "Dublin City": return DUB[a[1]]
    if co == "Dublin":
        sib = [b for b in sib if b[1] != "Dublin City"]
        return DUB[a[1]] + MEC["Co. Dublin"] * a[3] / sum(b[3] for b in sib)
    return MEC[f"Co. {co}"] * a[3] / sum(b[3] for b in sib)
LW = {a[1]: mec_gwh(a) if a[2] == "ROI" else a[3] for a in A}  # load weight: GWh (ROI) or people (NI)
assert abs(sum(LW[a[1]] for a in A if a[2] == "ROI") - (MEC["All Counties and Dublin Postal Districts"] - MEC["Not coded"])) < 1, "MEC03 split must sum to coded total"
print(f"CSO MEC03 {yrs3[-1]}: ROI area split from {sum(v for k, v in MEC.items() if k.startswith(('Co.', 'Dublin'))):.0f} GWh of coded metered consumption")

# ---------- EirGrid hourly series per region
F = {"WIND_ACTUAL": "w", "SOLAR_ACTUAL": "s", "CO2_INTENSITY": "i", "WIND_FCAST": "wf", "SYSTEM_DEMAND": "d"}
def load(region):
    acc = collections.defaultdict(lambda: collections.defaultdict(list))
    for p in glob.glob(f"raw/{region}_*.json"):
        for r in json.load(open(p))["Rows"]:
            if r["Value"] is None or r["FieldName"] not in F: continue
            t = dt.datetime.strptime(r["EffectiveTime"], "%d-%b-%Y %H:%M:%S").replace(minute=0)
            acc[t][F[r["FieldName"]]].append(r["Value"])
    hours = sorted(t for t in acc if "d" in acc[t] and "w" in acc[t])
    H = []
    for t in hours:
        x = {k: st.mean(vs) for k, vs in acc[t].items()}
        d, w, s = x["d"], x["w"], max(0, x.get("s", 0))
        res = min(1.0, (w + s) / d) if d > 0 else 0
        dd = max(0, x.get("wf", w) - w) if res >= GREEN_RES else 0  # est. dispatch-down (curtailed/constrained wind)
        H.append(dict(t=t, d=d, w=w, s=s, i=x.get("i", 0), res=res, dd=dd))
    return H
R = {r: load(r) for r in ("ROI", "NI")}

# ---------- area demand model
tot_pop = {r: sum(a[3] for a in A if a[2] == r) for r in R}
tot_lw = {r: sum(LW[a[1]] for a in A if a[2] == r) for r in R}
tot_dc = sum(a[4] for a in A)
tot_w = {r: sum(a[5] for a in A if a[2] == r) for r in R}
tot_s = {r: sum(a[6] for a in A if a[2] == r) for r in R}
daymean = {r: {} for r in R}
for r, H in R.items():
    g = collections.defaultdict(list)
    for h in H: g[h["t"].date()].append(h["d"])
    daymean[r] = {k: st.mean(x) for k, x in g.items()}
def dc_mw(r, day): return DC_FRAC * daymean[r][day] if r == "ROI" else 0.0
def area_load(a, h):
    """returns (dc_part, other_part) MW for area a in hour h"""
    r = a[2]; dcm = dc_mw(r, h["t"].date()) if h["t"].date() in daymean[r] else DC_FRAC * h["d"] * (r == "ROI")
    return dcm * a[4] / tot_dc if r == "ROI" else 0.0, (h["d"] - dcm) * LW[a[1]] / tot_lw[r]

# ---------- forecast: same hour-of-week mean of trailing 8 weeks, scaled by last-week trend
def forecast(H, start_idx, horizon):
    hist = H[max(0, start_idx - 24 * 56):start_idx]
    how = collections.defaultdict(list)
    for h in hist: how[(h["t"].weekday(), h["t"].hour)].append(h["d"])
    base = {k: st.mean(x) for k, x in how.items()}
    last = hist[-168:]; trend = st.mean(h["d"] for h in last) / st.mean(base[(h["t"].weekday(), h["t"].hour)] for h in last)
    t0 = H[start_idx - 1]["t"]
    return [(t0 + dt.timedelta(hours=k + 1), base[((t0 + dt.timedelta(hours=k + 1)).weekday(), (t0 + dt.timedelta(hours=k + 1)).hour)] * trend) for k in range(horizon)]

FC = {}
for r, H in R.items():
    n = len(H)
    back = forecast(H, n - 168, 168)
    mape = st.mean(abs(f - H[n - 168 + k]["d"]) / H[n - 168 + k]["d"] for k, (_, f) in enumerate(back))
    fwd = forecast(H, n, 168)
    FC[r] = dict(mape=round(mape * 100, 2),
                 actual=[[h["t"].isoformat(), round(h["d"])] for h in H[-336:]],
                 backtest=[[t.isoformat(), round(f)] for t, f in back],
                 forward=[[t.isoformat(), round(f)] for t, f in fwd],
                 hourly=[[h["t"].isoformat(), round(h["d"]), round(h["w"]), round(h["s"]), round(h["res"], 3), round(h["i"]), round(h["dd"])] for h in H[-336:]])
    print(f"{r}: {n} hours, forecast backtest MAPE {mape:.1%}")

# ---------- daily baseline vs GridSense routing per area
days = sorted(set(h["t"].date() for h in R["ROI"]) & set(h["t"].date() for h in R["NI"]))
def area_fc(a, f, dcm): return (dcm * a[4] / tot_dc if a[2] == "ROI" else 0.0) + (f - dcm) * LW[a[1]] / tot_lw[a[2]]
def route(areas, fc, hs, pool):
    """priority-weighted by forecast peak^2 (bigger peaks get first call), capped at each area's fossil
    load in the forecast peak hours, overflow re-shared; returns {area: {hour_t: MW routed}}"""
    dcm = DC_FRAC * st.mean(f for _, f in fc) if areas[0][2] == "ROI" else 0.0
    peak_t = {t for t, _ in sorted(fc, key=lambda p: -p[1])[:PEAK_HOURS]}
    ph = [h for h in hs if h["t"] in peak_t]
    caps = {a[1]: {h["t"]: sum(area_load(a, h)) * (1 - h["res"]) for h in ph} for a in areas}
    room = {n: sum(c.values()) for n, c in caps.items()}
    w = {a[1]: max(area_fc(a, f, dcm) for _, f in fc) ** 2 for a in areas}
    take = dict.fromkeys(room, 0.0)
    while pool > 1e-6:
        live = [n for n in room if room[n] - take[n] > 1e-6]
        if not live: break
        W = sum(w[n] for n in live); given = 0.0
        for n in live:
            g = min(pool * w[n] / W, room[n] - take[n]); take[n] += g; given += g
        pool -= given
    return {n: {t: take[n] * c / room[n] for t, c in caps[n].items()} if room[n] else {} for n in caps}, ph
OUT = {a[1]: [] for a in A}   # per day: [demand, ren_base, ren_gs, co2_base_t, co2_gs_t, ren_generated]
ROUTE48 = {}
for r, H in R.items():
    byday = collections.defaultdict(list); first = {}
    for i, h in enumerate(H): byday[h["t"].date()].append(h); first.setdefault(h["t"].date(), i)
    areas = [a for a in A if a[2] == r]
    for day in days:
        hs = byday[day]
        if len(hs) < 20:
            for a in areas: OUT[a[1]].append([0, 0, 0, 0, 0, 0])
            continue
        routed = {}
        if first[day] >= 24 * 14:  # ponytail: needs 2 weeks of history to forecast; earlier days get no routing
            routed, ph = route(areas, forecast(H, first[day], 24), hs, ROUTE_FRAC * sum(h["dd"] for h in hs))
        ints = {h["t"]: h["i"] for h in hs}
        for a in areas:
            dem = ren = co2 = 0.0
            for h in hs:
                l = sum(area_load(a, h)); dem += l; ren += l * h["res"]; co2 += l * h["i"] / 1000
            rt = routed.get(a[1], {}); extra = sum(rt.values())
            gen = sum(h["w"] * a[5] / tot_w[r] + h["s"] * a[6] / tot_s[r] for h in hs)
            OUT[a[1]].append([round(dem, 1), round(ren, 1), round(ren + extra, 1), round(co2, 1), round(co2 - sum(mw * ints[t] for t, mw in rt.items()) / 1000, 1), round(gen, 1)])
            if day >= days[-2]: ROUTE48.setdefault(a[1], {}).update({t.isoformat(): round(mw, 1) for t, mw in rt.items()})

# ---------- week-ahead plan per area: forecast peak + renewables routed to it, using the trailing 4-week average pool
PLAN = {}; FLOWS = []  # week-ahead [source, destination, MWh]
for r, H in R.items():
    areas = [a for a in A if a[2] == r]; fwd = forecast(H, len(H), 168)
    dcm = DC_FRAC * st.mean(f for _, f in fwd) if r == "ROI" else 0.0
    last = H[-24 * 28:]; pool = ROUTE_FRAC * sum(h["dd"] for h in last) / 28
    res_how = collections.defaultdict(list)
    for h in last: res_how[(h["t"].weekday(), h["t"].hour)].append(h["res"])
    for d in range(7):
        fc = fwd[d * 24:(d + 1) * 24]
        pseudo = [dict(t=t, d=f, res=st.mean(res_how[(t.weekday(), t.hour)])) for t, f in fc]
        routed, _ = route(areas, fc, pseudo, pool)
        for a in areas:
            pk = max(fc, key=lambda p: area_fc(a, p[1], dcm))
            p = PLAN.setdefault(a[1], dict(peak=0, at="", routed=0))
            if area_fc(a, pk[1], dcm) > p["peak"]: p.update(peak=round(area_fc(a, pk[1], dcm)), at=pk[0].isoformat())
            p["routed"] = round(p["routed"] + sum(routed.get(a[1], {}).values()))
    # where it comes from: dispatch-down happens at wind farms, so each area supplies the pool in proportion to its wind capacity
    for s_ in areas:
        PLAN[s_[1]]["sent"] = round(sum(PLAN[a[1]]["routed"] for a in areas) * s_[5] / tot_w[r])
        for d_ in areas:
            mwh = PLAN[d_[1]]["routed"] * s_[5] / tot_w[r]
            if mwh >= 1 and s_[1] != d_[1]: FLOWS.append([s_[1], d_[1], round(mwh)])
FLOWS.sort(key=lambda f: -f[2])

areas_meta = {a[1]: dict(region=a[2], pop=a[3], dc=round(a[4] / tot_dc, 4) if a[4] else 0, popshare=LW[a[1]] / tot_lw[a[2]], mec=round(LW[a[1]]) if a[2] == "ROI" else None, geo=a[0]) for a in A}

# ---------- geometry: merge ROI LAs + NI LGDs, cheap simplification
def simp(c):
    if isinstance(c[0], (int, float)): return [round(c[0], 3), round(c[1], 3)]
    if isinstance(c[0][0], (int, float)):
        out = []
        for p in c:
            q = [round(p[0] / .004) * .004, round(p[1] / .004) * .004]  # ponytail: grid-snap ~400m, use mapshaper if borders need to be crisp
            q = [round(q[0], 3), round(q[1], 3)]
            if not out or out[-1] != q: out.append(q)
        return out if len(out) >= 4 else None
    r = [simp(x) for x in c]; return [x for x in r if x]
name2disp = {a[0]: a[1] for a in A}
feats = []
for path, key in (("raw/roi.geojson", "name"), ("raw/ni.geojson", "LGDNAME")):
    for f in json.load(open(path))["features"]:
        nm = f["properties"][key]
        feats.append({"type": "Feature", "properties": {"name": name2disp[nm]}, "geometry": {"type": f["geometry"]["type"], "coordinates": simp(f["geometry"]["coordinates"])}})

data = dict(days=[d.isoformat() for d in days], areas=areas_meta, daily=OUT, forecast=FC, plan=PLAN, flows=FLOWS, route48=ROUTE48, cso=CSO, dc_frac=DC_FRAC,
            params=dict(route_frac=ROUTE_FRAC, peak_hours=PEAK_HOURS, green_res=GREEN_RES),
            geo={"type": "FeatureCollection", "features": feats}, built=dt.datetime.now().isoformat(timespec="minutes"))
open("web/data.js", "w").write("window.GS=" + json.dumps(data, separators=(",", ":")) + ";")

# ---------- self-check
tot = lambda k: sum(sum(x[k] for x in v) for v in OUT.values())
assert abs(tot(0) - sum(h["d"] for r in R for h in R[r] if h["t"].date() in set(days))) / tot(0) < 0.01, "area demand must sum to system demand"
assert tot(2) >= tot(1) and tot(4) <= tot(3), "GridSense can only add renewables / cut CO2"
assert all(x[2] <= x[0] + 0.5 for v in OUT.values() for x in v), "routing can't push an area past 100% renewable"
top = sorted(PLAN, key=lambda a: -PLAN[a]["peak"])[:5]
assert PLAN[top[0]]["routed"] >= PLAN[top[-1]]["routed"], "highest forecast peak gets first call"
assert abs(sum(f[2] for f in FLOWS) + sum(PLAN[a[1]]["routed"] * a[5] / tot_w[a[2]] for a in A) - sum(p["routed"] for p in PLAN.values())) / sum(p["routed"] for p in PLAN.values()) < 0.05, "flows must account for what's routed"
print("top flows:", ", ".join(f"{a}->{b} {m/1e3:.2f} GWh" for a, b, m in FLOWS[:6]))
print("week-ahead peaks:", ", ".join(f"{a} {PLAN[a]['peak']} MW ({PLAN[a]['routed']/1e3:.1f} GWh routed)" for a in top))
print(f"days {days[0]}..{days[-1]} | system RES {tot(1)/tot(0):.1%} -> GridSense {tot(2)/tot(0):.1%} | "
      f"extra renewable {(tot(2)-tot(1))/1000:.0f} GWh | CO2 avoided {(tot(3)-tot(4))/1000:.1f} kt")
