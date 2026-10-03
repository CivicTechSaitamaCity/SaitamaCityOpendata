#!/usr/bin/env python3
"""取得済みデータを集計し、ダッシュボード（dashboard/template.html にデータを埋め込んだ HTML）を生成する.

使い方: python3 scripts/build_dashboard.py [出力先 HTML]
"""
import collections
import csv
import io
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update import CATALOG, ROOT, find_latlon, header_index, parse_csv, to_float  # noqa: E402

WARDS = ['西区', '北区', '大宮区', '見沼区', '中央区', '桜区', '浦和区', '南区', '緑区', '岩槻区']
BBOX = (35.75, 36.05, 139.50, 139.80)

catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
PREF = catalog['pref']


def res_file(dsid, rid):
    f = PREF[str(dsid)]['resources'][str(rid)]['file']
    return ROOT / f['utf8']


def records(dsid, rid):
    return parse_csv(res_file(dsid, rid).read_text(encoding='utf-8'))


def table(dsid, rid):
    """ヘッダ名（前後空白・タブ除去）→ 値 の dict のリスト."""
    recs = records(dsid, rid)
    hi = header_index(recs)
    header = [h.strip().replace('\t', '').lstrip('\ufeff') for h in recs[hi]]
    return [dict(zip(header, r)) for r in recs[hi + 1:]]


def num(v):
    if v is None:
        return None
    v = str(v).replace(',', '').replace('"', '').strip()
    return to_float(v)


def latest_resource(dsid, pattern=None):
    res = [r for r in PREF[str(dsid)]['resources'].values() if r.get('file') and r['file'].get('utf8')
           and (pattern is None or re.search(pattern, r['name']))]
    return max(res, key=lambda r: r['name'])['id']


# ---------------------------------------------------------------- 位置情報

def geo_points(dsid, rid, name_keys=('名称', '施設名', '施設等の名称', '公園名', '施設名等', '施設_名称', '店舗名', '活動名称',
                                       'シニアサポートセンター名称', '販売地点名（名称）', '所在地')):
    recs = records(dsid, rid)
    for hi, row in enumerate(recs[:15]):
        lat, lon = find_latlon(row)
        if lat is None or lon is None:
            continue
        header = [h.strip().replace('\t', '') for h in row]
        tokyo = any('日本測地系' in c for r in recs[:hi + 1] for c in r)
        name_idx = next((header.index(k) for k in name_keys if k in header), None)
        pts = []
        for r in recs[hi + 1:]:
            y = to_float(r[lat]) if lat < len(r) else None
            x = to_float(r[lon]) if lon < len(r) else None
            if y is None or x is None:
                continue
            if tokyo:
                y, x = (y - 0.00010695 * y + 0.000017464 * x + 0.0046017,
                        x - 0.000046038 * y - 0.000083043 * x + 0.010040)
            if not (BBOX[0] <= y <= BBOX[1] and BBOX[2] <= x <= BBOX[3]):
                continue
            name = r[name_idx].replace('\n', ' ').strip() if name_idx is not None and name_idx < len(r) else ''
            pts.append({'y': y, 'x': x, 'name': name, 'row': dict(zip(header, r))})
        return pts
    return []


def ward_in(text):
    for w in WARDS:
        if w in text:
            return w
    return None


# 区の参照点: 区名が明示されたデータ
ref = []
for p in geo_points(1201, 4745):
    if p['row'].get('区名') in WARDS:
        ref.append((p['y'], p['x'], p['row']['区名']))
for p in geo_points(1206, 4750):
    if p['row'].get('区') in WARDS:
        ref.append((p['y'], p['x'], p['row']['区']))
for p in geo_points(1191, 4721):
    w = ward_in(p['row'].get('所在地_市区町村', ''))
    if w:
        ref.append((p['y'], p['x'], w))


def ward_of(y, x, k=5):
    kx = math.cos(math.radians(y))
    d = sorted(((y - ry) ** 2 + ((x - rx) * kx) ** 2, w) for ry, rx, w in ref)[:k]
    return collections.Counter(w for _, w in d).most_common(1)[0][0]


# 地図レイヤー（名称・データセットID・ファイルID）
LAYERS = [
    ('aed', 'AED', 1206, 4750),
    ('park', '公園', 1201, 4745),
    ('school', '市立学校', 1227, None),
    ('shelter', '指定避難所', 1209, 4757),
    ('emergency', '指定緊急避難場所', 1209, 4758),
    ('medical', '医療機関', 1230, 'latest'),
    ('sejutsu', '施術所', 1525, 'latest'),
    ('hoiku', '認可保育施設', 1214, None),
    ('club', '放課後児童クラブ', 1218, 4776),
    ('public', '公共施設', 1191, 4721),
    ('vote', '投票所', 3449, 8925),
    ('wifi', 'Free Wi-Fi', 1194, 4724),
    ('bunkazai', '文化財', 1196, 4726),
    ('shigen', 'いきいき活動', 1220, 4781),
    ('senior', 'シニアサポートセンター', 1221, 4782),
    ('kaden', '小型家電回収ボックス', 1229, 4809),
    ('health', '健康づくり協力店', 1200, 4742),
    ('ido', '移動販売', 2466, 7229),
    ('absentee', '不在者投票指定施設', 1203, 4747),
    ('greens', '公開型緑地', 1211, 4761),
    ('kodomo', '放課後子ども居場所', 2615, 7470),
    ('muryo', '無料低額診療施設', 1217, 4774),
    ('land', '未利用市有地', 1193, 4723),
    ('tank', '防火水槽', 1208, 4756),
]


def layer_points(dsid, rid):
    if rid == 'latest':
        rid = latest_resource(dsid)
    if rid is not None:
        return geo_points(dsid, rid)
    pts = []
    for r in PREF[str(dsid)]['resources'].values():
        if r.get('file') and r['file'].get('umap'):
            pts += geo_points(dsid, r['id'])
    return pts


ORIGIN = (35.75, 139.50)


def q(y, x):
    return [round((x - ORIGIN[1]) * 1e5), round((y - ORIGIN[0]) * 1e5)]


layers, matrix = [], {}
for key, label, dsid, rid in LAYERS:
    pts = layer_points(dsid, rid)
    wc = collections.Counter()
    out = []
    for p in pts:
        w = ward_of(p['y'], p['x'])
        wc[w] += 1
        out.append(q(p['y'], p['x']) + [p['name'][:40], WARDS.index(w)])
    layers.append({'key': key, 'name': label, 'dataset': dsid, 'count': len(out), 'pts': out})
    matrix[key] = [wc[w] for w in WARDS]
    print(f'layer {label}: {len(out)}', file=sys.stderr)

hydrants = geo_points(1208, 4755)
base = [v for p in hydrants for v in q(p['y'], p['x'])]

# 区の重心（参照点の平均）
cent = {}
for w in WARDS:
    ps = [(y, x) for y, x, ww in ref if ww == w]
    cent[w] = q(sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps))

# ---------------------------------------------------------------- 人口

pop_rows = table(978, 4463)
by_date = collections.defaultdict(lambda: collections.Counter())
for r in pop_rows:
    p, h = num(r['人口総数']), num(r['世帯数'])
    if p is None:
        continue
    y, m, _ = r['時点'].split('/')
    d = f'{y}-{int(m):02d}'
    by_date[d]['city'] += p
    by_date[d]['hh'] += h or 0
    by_date[d][r['区名']] += p
    by_date[d]['hh:' + r['区名']] += h or 0
dates = sorted(by_date)
pop = {
    'dates': dates,
    'city': [by_date[d]['city'] for d in dates],
    'hh': [by_date[d]['hh'] for d in dates],
    'wards': {w: [by_date[d][w] for d in dates] for w in WARDS},
}

# 年齢別人口（町丁字別・2026）→ 5歳階級ピラミッド
age_rows = table(3314, 8695)
BINS = [f'{i}–{i + 4}' for i in range(0, 100, 5)] + ['100+']
pyr = {w: {'m': [0] * len(BINS), 'f': [0] * len(BINS)} for w in ['市全体'] + WARDS}
single_age = collections.Counter()
for r in age_rows:
    a = r['年齢'].replace('~', '').replace('〜', '')
    a = int(num(a)) if num(a) is not None else None
    if a is None:
        continue
    b = min(a // 5, 20)
    m, f = num(r['男']) or 0, num(r['女']) or 0
    single_age[min(a, 100)] += m + f
    for w in ('市全体', r['区名']):
        if w in pyr:
            pyr[w]['m'][b] += m
            pyr[w]['f'][b] += f
pyramid = {'bins': BINS, 'year': age_rows[0]['年'], 'data': pyr, 'single': [single_age[i] for i in range(101)]}

# 区別人口・高齢化率（年齢別人口表の最新時点）
ap = table(2412, 7164)
last = max(r['調査年月日'] for r in ap)
wardstat = []
for r in ap:
    if r['調査年月日'] != last or r['地域名'] not in WARDS:
        continue
    total = num(r['総人口'])
    old = sum(num(v) or 0 for k, v in r.items() if re.match(r'(6[5-9]|[7-9]\d)-?\d*歳|85歳以上', k))
    young = sum(num(v) or 0 for k, v in r.items() if re.match(r'(0-4|5-9|10-14)歳', k))
    wardstat.append({'ward': r['地域名'], 'pop': total, 'hh': by_date[dates[-1]]['hh:' + r['地域名']], 'old': old / total, 'young': young / total})
wardstat.sort(key=lambda s: WARDS.index(s['ward']))
ward_pop = {s['ward']: s['pop'] for s in wardstat}

# ---------------------------------------------------------------- 医療・施術所

med = table(1230, latest_resource(1230))
med_types = collections.Counter(r['医療機関の種類'].strip() for r in med)
med_month = []
for r in sorted(PREF['1230']['resources'].values(), key=lambda r: r['name']):
    if r.get('file') and r['file'].get('rows'):
        m = re.search(r'令和(\d+)年(\d+)月', r['name'])
        med_month.append({'label': f'{2018 + int(m.group(1))}-{int(m.group(2)):02d}', 'count': r['file']['rows']})
sej = table(1525, latest_resource(1525))
SEJ = {'柔': '柔道整復', 'あ': 'あん摩マッサージ指圧', 'は': 'はり', 'き': 'きゅう'}
sej_types = collections.Counter()
for r in sej:
    for k in re.split(r'[\s　・]+', r['業種'].strip()):
        if k in SEJ:
            sej_types[SEJ[k]] += 1
sej_years = collections.Counter(r['開設年月日'][:4] for r in sej if r['開設年月日'][:4].isdigit())

# ---------------------------------------------------------------- 公園

parks = table(1201, 4745)
pt = collections.defaultdict(lambda: [0, 0.0])
pw = collections.defaultdict(lambda: [0, 0.0])
for r in parks:
    a = num(r['最終開設面積（ha）']) or 0
    pt[r['種別']][0] += 1
    pt[r['種別']][1] += a
    if r['区名'] in WARDS:
        pw[r['区名']][0] += 1
        pw[r['区名']][1] += a
park = {
    'types': sorted([{'type': k, 'count': v[0], 'area': round(v[1], 2)} for k, v in pt.items()], key=lambda d: -d['area']),
    'wards': [{'ward': w, 'count': pw[w][0], 'area': round(pw[w][1], 2),
               'per': round(pw[w][1] * 10000 / ward_pop[w], 2)} for w in WARDS],
    'total': len(parks), 'area': round(sum(v[1] for v in pt.values()), 1),
}

# ---------------------------------------------------------------- 水質

water = collections.defaultdict(lambda: collections.defaultdict(list))
for r in PREF['1213']['resources'].values():
    for row in table(1213, r['id']):
        place = row.get('採水場所') or row.get('場所') or ''
        if not place.startswith('給水'):
            continue
        ym = re.search(r'(\d{4})年(\d+)月', row.get('採水年月') or row.get('採水年月日') or '')
        item = (row.get('項目') or '').strip()
        v = num(row.get('検査結果'))
        if ym and v is not None and item in ('水温', '遊離残留塩素', '気温', 'カルシウム，マグネシウム等（硬度）'):
            water[item][f'{ym.group(1)}-{int(ym.group(2)):02d}'].append(v)
wmonths = sorted(set(water['水温']) | set(water['遊離残留塩素']))
avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
wq = {'months': wmonths, 'series': {k: [avg(water[k].get(m, [])) for m in wmonths] for k in water}}

# ---------------------------------------------------------------- スマート水道メーター

meter = table(3076, 8306)
hourly = collections.defaultdict(list)
daily = collections.Counter()
for r in meter:
    v = num(r.get('合計水量 （立方メートル）') or next((x for k, x in r.items() if k.startswith('合計水量')), None))
    if v is None:
        continue
    h = int(r['データ取得開始時刻'].split(':')[0]) if r.get('データ取得開始時刻') else None
    if h is None:
        h = int(next(x for k, x in r.items() if k.startswith('データ取得開始')).strip().split(':')[0])
    hourly[h].append(v)
    y, m, d = r['日付'].split('/')
    daily[f'{y}-{int(m):02d}'] += v
meter_out = {'hourly': [round(sum(hourly[h]) / len(hourly[h]), 4) for h in range(24)],
             'monthly': [{'m': k, 'v': round(daily[k], 1)} for k in sorted(daily)],
             'meters': 28}

# ---------------------------------------------------------------- 人流

flow = {}
for r in PREF['1224']['resources'].values():
    recs = records(1224, r['id'])
    hi = next(i for i, row in enumerate(recs) if row and row[0] == '年月')
    area = recs[hi - 1][0]
    by_month = collections.defaultdict(lambda: {'wd': {}, 'hd': {}})
    for row in recs[hi + 1:]:
        if not row[0] or '_' not in row[0]:
            continue
        h = int(row[1].replace('時', ''))
        vals = [num(v) or 0 for v in row[2:24]]
        wd, hd = vals[:11], vals[11:22]
        by_month[row[0]]['wd'][h] = wd
        by_month[row[0]]['hd'][h] = hd
    months = sorted(by_month)
    flow[area] = {
        'months': [m.replace('_', '-') for m in months],
        'wd14': [sum(by_month[m]['wd'].get(14, [0, 0])[:2]) for m in months],
        'hd14': [sum(by_month[m]['hd'].get(14, [0, 0])[:2]) for m in months],
    }
    recent = months[-12:]
    for kind in ('wd', 'hd'):
        prof = []
        for h in range(5, 29):
            xs = [sum(by_month[m][kind][h][:2]) for m in recent if h in by_month[m][kind]]
            prof.append(round(sum(xs) / len(xs)) if xs else None)
        flow[area][f'{kind}_profile'] = prof
        comp = [0, 0, 0]
        for m in recent:
            v = by_month[m][kind].get(14)
            if v:
                comp = [comp[i] + v[8 + i] for i in range(3)]
        flow[area][f'{kind}_comp'] = [round(c / len(recent)) for c in comp]
    ages = [0] * 6
    for m in recent:
        v = by_month[m]['wd'].get(14)
        if v:
            ages = [ages[i] + v[2 + i] for i in range(6)]
    flow[area]['ages'] = [round(a / len(recent)) for a in ages]
    flow[area]['recent'] = f'{recent[0].replace("_", "-")}〜{recent[-1].replace("_", "-")}'

# ---------------------------------------------------------------- 行政

eapp = [{'fy': r['年度'], 'm': r['月'], 'v': num(r['件数'])} for r in table(1195, 4725) if num(r.get('件数')) is not None]

teian_recs = records(1202, 4746)
teian = []
for row in teian_recs:
    m = re.match(r'令和(\d+)年度', row[0].replace(' ', ''))
    if m and len(row) > 13:
        teian.append({'fy': f'{2018 + int(m.group(1))}年度', 'months': [num(v) for v in row[1:13]], 'total': num(row[13])})
teian_age, sec = [], False
for row in teian_recs:
    if row[0].startswith('「わたしの提案」') and '年代別' in row[0]:
        sec = True
        continue
    if sec and row[0].startswith('年'):
        continue
    if sec and row[0] and len(row) > 13 and num(row[13]) is not None:
        if row[0].startswith('合'):
            break
        teian_age.append({'age': row[0], 'v': num(row[13])})

sewer_trend = [{'fy': f'{2000 + int(num(r["年度"])) - 12 if num(r["年度"]) > 20 else 2018 + int(num(r["年度"]))}年度',
                'v': num(r['さいたま市の下水道普及率（％）'])} for r in table(1210, 4760) if num(r.get('年度')) is not None]
sw = table(1210, 4759)
cols = list(sw[0].keys())
sewer_wards = [{'ward': r[cols[0]].replace('　', ''), 'now': num(r[cols[1]]), 'before': num(r[cols[2]])} for r in sw]
sewer = {'trend': sewer_trend, 'wards': sewer_wards,
         'labels': [re.sub(r'\s*下水道普及率.*', '', c).replace('　', '') for c in cols[1:3]]}

tax_rows = table(1205, 4749)
TAX_USE = ['保健・福祉・医療', '子育て・教育等', '道路・住宅・街作り等', '公債返済', '市民活動・防犯防災等', 'ごみ処理・環境保全等', 'その他']
TAX_SRC = ['個人市民税', '固定資産税', '都市計画税', '軽自動車税']
tax = {'uses': TAX_USE, 'sources': TAX_SRC,
       'models': [{'name': r['世帯モデル'].replace('　', ' '), 'use': [num(r[k]) or 0 for k in TAX_USE],
                   'src': [num(r[k]) or 0 for k in TAX_SRC]} for r in tax_rows]}

redev = []
for r in table(1231, 4811):
    redev.append({'name': r['地区名'], 'ward': r['区'], 'start': num(r['事業開始年度']), 'end': num(r['事業完了年度']),
                  'status': r['進捗'], 'cost': num(r['総事業費（円）']), 'floor': num(r['延床面積（m2）']),
                  'use': r['主要用途等'], 'scale': r['規模']})
kukaku = collections.defaultdict(lambda: [0, 0.0])
for r in table(1247, 4829):
    kukaku[r['施行状況']][0] += 1
    kukaku[r['施行状況']][1] += num(r['施行面積(ha)']) or 0
kukaku = [{'status': k, 'count': v[0], 'area': round(v[1], 1)} for k, v in kukaku.items()]

shelters = []
em = table(1209, 4758)
for hz in ('地震', '洪水', '崖崩れ', '大規模な火事'):
    c = collections.Counter()
    for r in em:
        v = (r.get(hz) or '').strip()
        c['可' if v == '可' else '条件付き' if re.search(r'[ＦF]以上|階', v) else '不可' if v == '不可' else '指定なし'] += 1
    shelters.append({'hazard': hz, **c})

shitei = table(1204, 4748)
skey = next(k for k in shitei[0] if k.startswith('選定'))
senkou = collections.Counter(r[skey].strip() for r in shitei if r[skey].strip())
shokan = collections.Counter(r['所管課'].strip() for r in shitei if r.get('所管課', '').strip())

shigen = collections.Counter(r['活動内容'].strip() for r in table(1220, 4781) if r['活動内容'].strip())

hoiku = collections.defaultdict(lambda: [0, 0])
for r in PREF['1214']['resources'].values():
    if not (r.get('file') and r['file'].get('utf8')):
        continue
    for row in table(1214, r['id']):
        t = (row.get('施設_種別') or '').strip()
        capk = next((k for k in row if '定員' in k or '収容人数' in k), None)
        if t:
            hoiku[t][0] += 1
            hoiku[t][1] += int(num(row.get(capk)) or 0)
ninkagai = table(1215, 4771)
hoiku['認可外保育施設'] = [len(ninkagai), int(sum(num(r.get('定員')) or 0 for r in ninkagai))]
hoiku = sorted([{'type': k, 'count': v[0], 'cap': v[1]} for k, v in hoiku.items()], key=lambda d: -d['cap'])

land = collections.Counter(r['面積区分'] for r in table(1193, 4723))
bunka = collections.Counter((r['指定'].strip(), r['種別'].strip()) for r in table(1196, 4726))
hyd_station = collections.Counter(p['row'].get('管轄署所', '').strip() for p in hydrants)

vote_ward = matrix['vote']

# ---------------------------------------------------------------- 追加データ（応急給水・市営住宅・自治会・浄化槽）

def ward_counts(pairs, keys):
    c = collections.Counter(pairs)
    return {k: [c[(w, k)] for w in WARDS] for k in keys}


kyusui_rows = table(1207, 4752)
kyusui_types = [k for k, _ in collections.Counter(r['種類'] for r in kyusui_rows).most_common()]
kyusui = {'types': kyusui_types, 'counts': ward_counts(((r['区名'], r['種類']) for r in kyusui_rows), kyusui_types)}

shiei_rows = table(1192, 4722)
shiei_types = [k for k, _ in collections.Counter(r['種別'] for r in shiei_rows).most_common()]
shiei = {'types': shiei_types, 'counts': ward_counts(((ward_in(r['住所']) or ward_in(r['住所（所在地）']), r['種別']) for r in shiei_rows), shiei_types),
         'items': [[r['名称'], r['種別'], r['住所']] for r in shiei_rows]}

jichikai, cur = {w: {'total': 0, 'unions': []} for w in WARDS}, None
for row in records(1199, 4740):
    head = row[0].replace('　', '').replace(' ', '')
    if head in WARDS:
        cur = head
        continue
    if not cur:
        continue
    n = num(row[3]) if len(row) > 3 else None
    if head.isdigit() and n:
        jichikai[cur]['unions'].append([row[1], int(n)])
    elif head.startswith('地区連合会未加入') and n:
        jichikai[cur]['unions'].append(['連合会未加入', int(n)])
    elif head.endswith('合計') and head.startswith(cur) and n:
        jichikai[cur]['total'] = int(n)

jokaso_out = collections.Counter()
jokaso_in = collections.Counter()
for r in table(1219, 4777):
    addr = r['住所'].replace(' ', '')
    if addr.startswith('さいたま市'):
        jokaso_in[ward_in(addr) or '不明'] += 1
    else:
        m = re.match(r'(?:埼玉県)?(.+?[市町村])', addr)
        jokaso_out[m.group(1) if m else 'その他'] += 1
seisou = collections.Counter(ward_in(r['住所']) or '市外' for r in table(1219, 4778))
jokaso = {'in': [jokaso_in[w] for w in WARDS], 'out': jokaso_out.most_common(10), 'out_total': sum(jokaso_out.values()),
          'seisou': [seisou[w] for w in WARDS], 'seisou_out': seisou['市外']}

# ---------------------------------------------------------------- GTFS（コミュニティバス・乗合タクシー）

def gtfs_feeds():
    feeds = []
    for rid, kind in ((4797, 'コミュニティバス'), (6357, '乗合タクシー')):
        base = ROOT / PREF['1225']['resources'][str(rid)]['file']['utf8']
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            def rd(name):
                f = d / name
                return list(csv.DictReader(io.StringIO(f.read_text(encoding='utf-8')))) if f.exists() else []
            weekday = {c['service_id'] for c in rd('calendar.txt') if c.get('monday') == '1'}
            trips = rd('trips.txt')
            routes = {r['route_id']: r.get('route_long_name') or r.get('route_short_name') or '' for r in rd('routes.txt')}
            shapes = collections.defaultdict(list)
            for s in rd('shapes.txt'):
                shapes[s['shape_id']].append((int(s['shape_pt_sequence']), float(s['shape_pt_lat']), float(s['shape_pt_lon'])))
            used = {t.get('shape_id') for t in trips if t.get('shape_id')}
            lines = []
            for sid in sorted(used):
                pts = [q(y, x) for _, y, x in sorted(shapes.get(sid, []))]
                if len(pts) > 1:
                    lines.append([v for p in pts for v in p])
            stops = [q(float(s['stop_lat']), float(s['stop_lon'])) + [s['stop_name']] for s in rd('stops.txt')
                     if s.get('stop_lat') and s.get('location_type', '0') in ('', '0')]
            wd_trips = sum(1 for t in trips if t['service_id'] in weekday)
            feeds.append({'name': d.name.rsplit('_', 1)[0], 'kind': kind, 'routes': sorted(set(routes.values())),
                          'trips_wd': wd_trips, 'n_stops': len(stops), 'lines': lines, 'stops': stops})
    return feeds


gtfs = gtfs_feeds()

# ---------------------------------------------------------------- さいたま市統計書（最新版）

STAT_RID = max((r for r in PREF['2318']['resources'].values() if r.get('file') and r['file'].get('utf8')), key=lambda r: r['name'])
STAT_DIR = ROOT / STAT_RID['file']['utf8']


def stat(book_prefix, sheet):
    book = next(p for p in STAT_DIR.iterdir() if p.is_dir() and re.match(rf'{book_prefix}\D', p.name))
    rows = list(csv.reader(io.StringIO((book / f'{sheet}.csv').read_text(encoding='utf-8'))))
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, r)) for r in rows[1:] if any(r)]


def col(row, prefix):
    k = next((k for k in row if k.startswith(prefix)), None)
    return num(row[k]) if k else None


def wareki(label):
    m = re.match(r'(令和|平成)(\d+|元)年', label)
    if not m:
        return label
    n = 1 if m.group(2) == '元' else int(m.group(2))
    return str((2018 if m.group(1) == '令和' else 1988) + n)


stat_index = []
for book in sorted((p for p in STAT_DIR.iterdir() if p.is_dir()), key=lambda p: int(re.match(r'\d+', p.name).group())):
    idx = list(csv.DictReader(io.StringIO((book / '_目次.csv').read_text(encoding='utf-8')))) if (book / '_目次.csv').exists() else []
    stat_index.append({'chapter': re.sub(r'^\d+\s*', '', book.name), 'no': int(re.match(r'\d+', book.name).group()),
                       'tables': [[i['シート'], i['表題']] for i in idx]})

wx = [r for r in stat('1', '1-7') if '月' in r['年月']]
weather = {'label': wareki(wx[0]['年月']) if wx else '', 'months': [re.search(r'(\d+)月', r['年月']).group(1) + '月' for r in wx],
           'avg': [col(r, '平均気温') for r in wx], 'max': [col(r, '最高気温') for r in wx], 'min': [col(r, '最低気温') for r in wx],
           'rain': [col(r, '合計降水量') for r in wx], 'sun': [col(r, '合計日照時間') for r in wx],
           'years': [[wareki(r['年月']), col(r, '平均気温'), col(r, '合計降水量')] for r in stat('1', '1-7') if '月' not in r['年月']]}

jr = stat('13', '13-1')
jr_daily = next(r for r in jr if '1日平均' in ''.join(r.values()))
stations = [k for k in jr[0] if k and k not in ('', '時間軸コード', '年度', '平均')]
jr_out = {'daily': sorted([[s.split('_')[0], num(jr_daily[s])] for s in stations if num(jr_daily.get(s)) is not None], key=lambda x: -x[1]),
          'years': [[wareki(r['年度']), sum(num(r[s]) or 0 for s in stations)] for r in jr if '1日平均' not in ''.join(r.values())],
          'fy': wareki(jr_daily['年度'])}

vital = [r for r in stat('2', '2-8') if r.get('市区名', '').startswith('さいたま市')]
vital_w = [r for r in stat('2', '2-8') if any(r.get('市区名', '').startswith(w) for w in WARDS)]
vit = {'years': [wareki(r['年次']) for r in vital], 'births': [col(r, '出生_総数') for r in vital], 'deaths': [col(r, '死亡_総数') for r in vital],
       'tfr': [col(r, '合計特殊出生率') for r in vital], 'marriage': [col(r, '婚姻') for r in vital],
       'wards': [[next(w for w in WARDS if r['市区名'].startswith(w)), col(r, '出生_総数'), col(r, '死亡_総数')] for r in vital_w]}

death = stat('16', '16-14')
last_d = death[-1]
death_out = {'year': wareki(last_d['年次']), 'causes': sorted([[k.split('_')[0], num(v)] for k, v in last_d.items()
                                                              if k not in ('', '時間軸コード', '年次') and not k.startswith('総数') and '_うち' not in k and num(v)], key=lambda x: -x[1]),
             'trend': [[wareki(r['年次']), col(r, '総数'), col(r, '悪性新生物'), col(r, '老衰')] for r in death]}

gomi = stat('16', '16-16')
gomi_out = [[wareki(r['年度']), col(r, '収集量_総数_（'), col(r, '人口'),
             round(col(r, '収集量_総数_（') * 1e6 / col(r, '人口') / 365, 1), col(r, '資源化・有効利用_総数')] for r in gomi]

fire = [r for r in stat('18', '18-12') if r.get('市区名', '').startswith('さいたま市')]
fire_out = [[wareki(r['年次']), col(r, '火災発生件数_総数'), col(r, '死傷者数_死者'), col(r, '死傷者数_傷者')] for r in fire]
amb = stat('18', '18-14')
amb_city = [r for r in amb if r.get('消防署別', '').startswith('総数')]
kinds = [k for k in amb[0] if k.startswith('事故種別出場件数_') and not k.endswith('総数_（件）')]
amb_out = {'years': [[wareki(r['年次']), col(r, '事故種別出場件数_総数'), col(r, '搬送人員_総数') or next((num(v) for k, v in r.items() if '搬送' in k and '総数' in k), None)] for r in amb_city],
           'kinds': sorted([[k.split('_')[1], num(amb_city[-1][k]) or 0] for k in kinds], key=lambda x: -x[1]),
           'year': wareki(amb_city[-1]['年次'])}

fin = stat('20', '20-1')
fin_last = max((r['年度'] for r in fin if r['区分2'] == '総額' and r['区分1'].startswith('歳出') and (col(r, '決算額') or 0) > 0), key=wareki)
fin_out = {'fy': wareki(fin_last) + '年度',
           'rev': sorted([[r['区分2'], col(r, '決算額')] for r in fin if r['年度'] == fin_last and r['区分1'].startswith('歳入') and r['区分2'] != '総額' and col(r, '決算額')], key=lambda x: -x[1]),
           'exp': sorted([[r['区分2'], col(r, '決算額')] for r in fin if r['年度'] == fin_last and r['区分1'].startswith('歳出') and r['区分2'] != '総額' and col(r, '決算額')], key=lambda x: -x[1]),
           'total': [[wareki(r['年度']), r['区分1'][:2], col(r, '決算額')] for r in fin if r['区分2'] == '総額' and (col(r, '決算額') or 0) > 0]}

tour = [[wareki(r['年次']), col(r, '観光地点'), col(r, 'イベント')] for r in stat('15', '15-31')]

# ---------------------------------------------------------------- 水質（全項目）

wq_all = collections.defaultdict(lambda: collections.defaultdict(list))
wq_std, wq_unit, wq_nd = {}, {}, collections.Counter()
for r in PREF['1213']['resources'].values():
    for row in table(1213, r['id']):
        place = row.get('採水場所') or row.get('場所') or ''
        if not place.startswith('給水'):
            continue
        ym_ = re.search(r'(\d{4})年(\d+)月', row.get('採水年月') or row.get('採水年月日') or '')
        item = re.sub(r'\s+', '', row.get('項目') or '')
        if not ym_ or not item or item.startswith('"') or len(item) < 2:
            continue
        key = f'{ym_.group(1)}-{int(ym_.group(2)):02d}'
        raw_v = (row.get('検査結果') or '').strip()
        v = num(raw_v)
        wq_std.setdefault(item, (row.get('水質基準値') or '').strip())
        wq_unit.setdefault(item, (row.get('単位') or '').strip())
        if v is None:
            wq_nd[item] += 1
        else:
            wq_all[item][key].append(v)
wq_items = []
for item in sorted(wq_all, key=lambda k: -sum(len(v) for v in wq_all[k].values())):
    if sum(len(v) for v in wq_all[item].values()) < 10:
        continue
    wq_items.append({'name': item, 'std': wq_std.get(item, ''), 'unit': wq_unit.get(item, ''), 'nd': wq_nd[item],
                     'avg': [avg(wq_all[item].get(m, [])) for m in wmonths],
                     'min': [min(wq_all[item][m]) if wq_all[item].get(m) else None for m in wmonths],
                     'max': [max(wq_all[item][m]) if wq_all[item].get(m) else None for m in wmonths]})
non_numeric = sorted(k for k in wq_nd if k not in {i['name'] for i in wq_items})

# ---------------------------------------------------------------- 医療機関・施術所の開設・廃止

def churn(dsid, name_key, addr_key):
    res = sorted((r for r in PREF[str(dsid)]['resources'].values() if r.get('file') and r['file'].get('utf8')), key=lambda r: r['name'])
    out, prev = [], None
    for r in res:
        m = re.search(r'令和(\d+)年(\d+)月', r['name'])
        label = f'{2018 + int(m.group(1))}-{int(m.group(2)):02d}'
        rows = table(dsid, r['id'])
        cur = {(x[name_key].strip(), re.sub(r'\s', '', x[addr_key])): x for x in rows}
        if prev is not None:
            new, gone = cur.keys() - prev.keys(), prev.keys() - cur.keys()
            out.append({'month': label, 'new': len(new), 'closed': len(gone),
                        'new_list': sorted(k[0] for k in new)[:30], 'closed_list': sorted(k[0] for k in gone)[:30]})
        prev = cur
    return out


churn_med = churn(1230, '名称', '所在地＿連結表記')
churn_sej = churn(1525, '施設名', '所在地')

# ---------------------------------------------------------------- 年齢別人口の推移・世帯・水道メーター集計・指定管理

age_series = []
for d in sorted({r['調査年月日'] for r in ap}):
    rs = [r for r in ap if r['調査年月日'] == d and r['地域名'] in WARDS]
    tot = sum(num(r['総人口']) for r in rs)
    old = sum(num(v) or 0 for r in rs for k, v in r.items() if re.match(r'(6[5-9]|[7-9]\d)-?\d*歳|85歳以上', k))
    young = sum(num(v) or 0 for r in rs for k, v in r.items() if re.match(r'(0-4|5-9|10-14)歳', k))
    age_series.append([d[:7], tot, round(old / tot * 100, 2), round(young / tot * 100, 2)])

hh_rows = table(3314, 8694)
hh_ward = collections.Counter()
for r in hh_rows:
    hh_ward[r['区名']] += num(r['世帯数']) or 0
pop_ward26 = {w: sum(pyramid['data'][w]['m']) + sum(pyramid['data'][w]['f']) for w in WARDS}
households = {'wards': [[w, hh_ward[w], round(pop_ward26[w] / hh_ward[w], 2) if hh_ward[w] else None] for w in WARDS],
              'top': sorted([[r['町（丁）字名'].replace(r['区名'], ''), r['区名'], num(r['世帯数'])] for r in hh_rows], key=lambda x: -(x[2] or 0))[:15],
              'year': hh_rows[0]['年'], 'towns': len(hh_rows)}

meter_sum = [{k.strip(): v for k, v in r.items()} for r in table(3076, 8307)]

def reiwa_end(period):
    m = re.search(r'[～〜~]\s*([RHＲ])(\d+)\.', period)
    if not m:
        return None
    return (2018 if m.group(1) in 'RＲ' else 1988) + int(m.group(2))

shitei_end = collections.Counter(reiwa_end(r['指定期間']) for r in shitei if reiwa_end(r.get('指定期間', '')))
shitei_ops = collections.Counter(r['指定管理者'].replace('\n', '').strip() for r in shitei if r.get('指定管理者', '').strip())

# ---------------------------------------------------------------- カタログ

cat_rows = table(1151, 4651)
cat_of = {r['データセット_ID']: r.get('データセット_分類', '') for r in cat_rows}
datasets = []
for ds in sorted(PREF.values(), key=lambda d: d['metadata_modified'], reverse=True):
    res = []
    for r in ds['resources'].values():
        f = r.get('file') or {}
        res.append({'name': r['name'].replace('【さいたま市】', ''), 'fmt': (r['format'] or '?').upper(),
                    'rows': f.get('rows'), 'geo': bool(f.get('umap')), 'size': r['size']})
    datasets.append({'id': ds['id'], 'title': ds['title'].replace('【さいたま市】', ''), 'freq': ds['frequency'],
                     'modified': ds['metadata_modified'][:10], 'release': ds['release_date'],
                     'cat': cat_of.get(str(ds['id']), '') or 'その他', 'tags': ds['tags'], 'res': res,
                     'author': ds['author']})
gsp = [{'title': d['title'].replace('111007_埼玉県_さいたま市_', ''), 'name': d['name'], 'modified': d['metadata_modified'][:10],
        'n': len(d['resources']), 'fmts': sorted({(r['format'] or '?').upper() for r in d['resources'].values()}),
        'size': sum(r['size'] or 0 for r in d['resources'].values())}
       for d in sorted(catalog['gsp'].values(), key=lambda d: d['metadata_modified'], reverse=True)]

reports = sorted((ROOT / 'reports').glob('*/*.json'))
latest = json.loads(reports[-1].read_text(encoding='utf-8')) if reports else None

all_res = [r for d in PREF.values() for r in d['resources'].values()]
csv_files = [r for r in all_res if r.get('file') and r['file'].get('rows') is not None]
meta = {
    'checked': latest['checked_at'][:10] if latest else '',
    'n_pref': len(PREF), 'n_gsp': len(catalog['gsp']), 'n_res': len(all_res), 'n_csv': len(csv_files),
    'rows': sum(r['file']['rows'] for r in csv_files),
    'geo_files': sum(1 for r in csv_files if r['file'].get('umap')),
    'points': sum(l['count'] for l in layers) + len(hydrants),
    'hydrants': len(hydrants),
    'formats': collections.Counter((r['format'] or '?').upper() for r in all_res),
}

DATA = {
    'meta': meta, 'wards': WARDS, 'origin': ORIGIN, 'centroids': cent,
    'layers': layers, 'base': base, 'matrix': matrix, 'wardstat': wardstat,
    'pop': pop, 'pyramid': pyramid,
    'medical': {'types': med_types.most_common(), 'monthly': med_month, 'sej': sej_types.most_common(),
                'sej_years': sorted(sej_years.items())},
    'park': park, 'water': wq, 'meter': meter_out, 'flow': flow,
    'eapp': eapp, 'teian': teian, 'teian_age': teian_age, 'sewer': sewer, 'tax': tax,
    'redev': redev, 'kukaku': kukaku, 'shelters': shelters,
    'shitei': {'senkou': senkou.most_common(), 'shokan': shokan.most_common(12), 'total': len(shitei)},
    'shigen': shigen.most_common(), 'hoiku': hoiku, 'land': sorted(land.items()),
    'bunka': [[a, b, n] for (a, b), n in bunka.items()], 'hyd_station': hyd_station.most_common(),
    'datasets': datasets, 'gsp': gsp,
    'kyusui': kyusui, 'shiei': shiei, 'jichikai': jichikai, 'jokaso': jokaso, 'gtfs': gtfs,
    'stat': {'edition': STAT_RID['name'].replace('さいたま市統計書', '').strip('()（）'), 'index': stat_index, 'weather': weather, 'jr': jr_out,
             'vital': vit, 'death': death_out, 'gomi': gomi_out, 'fire': fire_out, 'amb': amb_out, 'fin': fin_out, 'tour': tour},
    'wq_items': wq_items, 'wq_nonnum': non_numeric, 'churn': {'med': churn_med, 'sej': churn_sej},
    'age_series': age_series, 'households': households, 'meter_sum': meter_sum,
    'shitei_end': sorted(shitei_end.items()), 'shitei_ops': shitei_ops.most_common(10),
}

out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'dashboard' / 'index.html'
template = (ROOT / 'dashboard' / 'template.html').read_text(encoding='utf-8')
payload = json.dumps(DATA, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(template.replace('/*__DATA__*/null', payload), encoding='utf-8')
print(f'{out} ({out.stat().st_size / 1024:.0f} KB)', file=sys.stderr)
