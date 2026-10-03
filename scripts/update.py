#!/usr/bin/env python3
"""さいたま市オープンデータの取得・変換・更新レポート作成.

- 埼玉県オープンデータポータル（さいたま市公開分）: メタデータを取得し、CSV をダウンロードして変換する
- G空間情報センター（さいたま市公開分）: メタデータ（データセット一覧）のみ記録する
- 前回の catalog.json と比較し、変更があれば reports/ に日記形式のレポートを残し、docs/ のフィードを更新する

終了コード: 0=正常, 1=致命的エラー（何も書き込まない）, 2=一部失敗（成功分は書き込む）
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
CATALOG = ROOT / 'catalog.json'
CATALOG_MD = ROOT / 'CATALOG.md'
REPORTS = ROOT / 'reports'
DOCS = ROOT / 'docs'

REPO = os.environ.get('GITHUB_REPOSITORY', 'CivicTechSaitamaCity/SaitamaCityOpendata')
REPO_URL = f'https://github.com/{REPO}'
RAW_URL = f'https://raw.githubusercontent.com/{REPO}/main'

PREF_API = 'https://opendata.pref.saitama.lg.jp/ckan_api/package_search'
PREF_HOST = 'opendata.pref.saitama.lg.jp'
PREF_ORG_ID = 25
PREF_DATASET_URL = 'https://opendata.pref.saitama.lg.jp/datasets/{}'
GSP_API = 'https://www.geospatial.jp/ckan/api/3/action/package_search'
GSP_ORG = 'saitama-111007'
GSP_DATASET_URL = 'https://www.geospatial.jp/ckan/dataset/{}'

USER_AGENT = f'SaitamaCityOpendata-bot (+{REPO_URL})'
JST = ZoneInfo('Asia/Tokyo')
MAX_BYTES = 30 * 1024 * 1024
REQUEST_INTERVAL = 1.0
FEED_ENTRIES = 50
SAMPLE_ROWS = 5
SOURCE_NAMES = {'pref': '埼玉県オープンデータポータル', 'gsp': 'G空間情報センター'}


class FatalError(Exception):
    pass


# ---------------------------------------------------------------- HTTP

def http_get(url, retries=3):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read(MAX_BYTES + 1)
        except Exception as e:
            if attempt == retries - 1:
                if isinstance(e, urllib.error.HTTPError):  # アクセス制限の調査用
                    print(f'HTTP {e.code} {url}\n{e.headers}{e.read(500)!r}', file=sys.stderr)
                raise
            time.sleep(5 * (attempt + 1))


def get_json(url):
    d = json.loads(http_get(url))
    if not d.get('success'):
        raise FatalError(f'API エラー: {url}: {d.get("error")}')
    return d['result']


# ---------------------------------------------------------------- メタデータ

def to_int(v):
    return int(v) if str(v or '').isdigit() else None


def fetch_pref():
    packages, start = [], 0
    while True:
        q = urllib.parse.urlencode({'rows': 100, 'start': start})
        result = get_json(f'{PREF_API}?{q}')
        packages += result['results']
        start += len(result['results'])
        if not result['results'] or start >= result['count']:
            break
        time.sleep(REQUEST_INTERVAL)
    datasets = {}
    for p in packages:
        if not p.get('organization') or p['organization'].get('id') != PREF_ORG_ID:
            continue
        ex = {e['key']: e['value'] for e in p.get('extras') or []}
        datasets[str(p['id'])] = {
            'id': p['id'],
            'title': p.get('title') or '',
            'notes': p.get('notes') or '',
            'author': p.get('author') or '',
            'landing_page': ex.get('landingPage') or p.get('url') or '',
            'frequency': ex.get('frequency_of_update') or '',
            'release_date': ex.get('release_date') or '',
            'metadata_modified': p.get('metadata_modified') or '',
            'tags': sorted(t['name'] for t in p.get('tags') or []),
            'resources': {
                str(r['id']): {
                    'id': r['id'],
                    'name': r.get('name') or r.get('title') or '',
                    'format': (r.get('format') or '').lower(),
                    'url': r.get('url') or '',
                    'size': to_int(r.get('size')),
                    'last_modified': r.get('last_modified') or '',
                    'license': r.get('resource_license_id') or '',
                }
                for r in p.get('resources') or []
            },
        }
    return datasets


def fetch_gsp():
    q = urllib.parse.urlencode({'fq': f'organization:{GSP_ORG}', 'rows': 1000})
    result = get_json(f'{GSP_API}?{q}')
    datasets = {}
    for p in result['results']:
        datasets[p['name']] = {
            'id': p['id'],
            'name': p['name'],
            'title': p.get('title') or '',
            'notes': p.get('notes') or '',
            'metadata_modified': p.get('metadata_modified') or '',
            'resources': {
                r['id']: {
                    'id': r['id'],
                    'name': r.get('name') or '',
                    'format': (r.get('format') or '').lower(),
                    'url': r.get('url') or '',
                    'size': to_int(r.get('size')),
                    'last_modified': r.get('last_modified') or '',
                }
                for r in p.get('resources') or []
            },
        }
    return datasets


# ---------------------------------------------------------------- CSV 処理

def decode(raw):
    for enc in ('utf-8-sig', 'cp932', 'euc_jp'):
        try:
            return raw.decode(enc), 'utf-8' if enc == 'utf-8-sig' else enc
        except UnicodeDecodeError:
            pass
    return None, None


def parse_csv(text):
    try:
        records = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return None
    return [r for r in records if any(c.strip() for c in r)]


def header_index(records):
    """タイトル行を読み飛ばし、ヘッダらしい最初の行の位置を返す."""
    width = max((sum(1 for c in r if c.strip()) for r in records[:50]), default=0)
    for i, r in enumerate(records[:15]):
        if sum(1 for c in r if c.strip()) >= max(2, width / 2):
            return i
    return 0


def find_latlon(row):
    lat = lon = None
    for i, c in enumerate(row):
        k = c.strip().lower()
        if lat is None and ('緯度' in k or '北緯' in k or k in ('lat', 'latitude')):
            lat = i
        elif lon is None and ('経度' in k or '東経' in k or k in ('lon', 'lng', 'long', 'longitude')):
            lon = i
    return lat, lon


def to_float(v):
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return None


def tokyo_to_wgs84(lat, lon):
    """日本測地系 → 世界測地系（国土地理院の近似式。誤差は数m程度）."""
    return (lat - 0.00010695 * lat + 0.000017464 * lon + 0.0046017,
            lon - 0.000046038 * lat - 0.000083043 * lon + 0.010040)


def to_umap(records, label, warnings):
    """緯度・経度列を持つ CSV を uMap 用（lat,lon・世界測地系）に変換する. 該当しなければ None."""
    for hi, row in enumerate(records[:15]):
        lat, lon = find_latlon(row)
        if lat is None or lon is None:
            continue
        header = list(row)
        header[lat], header[lon] = 'lat', 'lon'
        body = [list(r) for r in records[hi + 1:]]
        if any('日本測地系' in c for r in records[:hi + 1] for c in r):
            for r in body:
                y = to_float(r[lat]) if lat < len(r) else None
                x = to_float(r[lon]) if lon < len(r) else None
                if y is not None and x is not None:
                    y, x = tokyo_to_wgs84(y, x)
                    r[lat], r[lon] = f'{y:.7f}', f'{x:.7f}'
        valid = swapped = 0
        for r in body:
            y = to_float(r[lat]) if lat < len(r) else None
            x = to_float(r[lon]) if lon < len(r) else None
            if y is None or x is None:
                continue
            if 20 <= y <= 46 and 122 <= x <= 154:
                valid += 1
            elif 20 <= x <= 46 and 122 <= y <= 154:
                swapped += 1
        if swapped > valid:
            warnings.append(f'{label}: 緯度と経度が入れ替わっている可能性があります（{swapped}行）')
        if valid == 0 and swapped == 0:
            warnings.append(f'{label}: 緯度経度列はありますが、数値として読める座標がありません')
            return None
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator='\n')
        w.writerow(header)
        w.writerows(body)
        return buf.getvalue()
    return None


def table_of(text):
    """(ヘッダ, データ行) を返す."""
    records = parse_csv(text) if text is not None else None
    if not records:
        return [], []
    hi = header_index(records)
    return records[hi], records[hi + 1:]


def csv_diff(old_text, new_text):
    old_header, old_rows = table_of(old_text)
    new_header, new_rows = table_of(new_text)
    oc = collections.Counter(tuple(r) for r in old_rows)
    nc = collections.Counter(tuple(r) for r in new_rows)
    added = list((nc - oc).elements())
    removed = list((oc - nc).elements())
    return {
        'rows_before': len(old_rows),
        'rows_after': len(new_rows),
        'rows_added': len(added),
        'rows_removed': len(removed),
        'columns_added': [c for c in new_header if c not in old_header],
        'columns_removed': [c for c in old_header if c not in new_header],
        'header': new_header,
        'old_header': old_header,
        'sample_added': [list(r) for r in added[:SAMPLE_ROWS]],
        'sample_removed': [list(r) for r in removed[:SAMPLE_ROWS]],
    }


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='')


def process_csv(dsid, rid, raw, label, warnings):
    paths = {k: DATA / k / dsid / f'{rid}.csv' for k in ('raw', 'utf8', 'umap')}
    info = {
        'sha256': hashlib.sha256(raw).hexdigest(),
        'bytes': len(raw),
        'raw': paths['raw'].relative_to(ROOT).as_posix(),
        'utf8': None, 'umap': None, 'encoding': None, 'rows': None, 'columns': [],
    }
    paths['raw'].parent.mkdir(parents=True, exist_ok=True)
    paths['raw'].write_bytes(raw)
    for k in ('utf8', 'umap'):
        paths[k].unlink(missing_ok=True)
    if raw[:2] == b'PK':
        warnings.append(f'{label}: CSV として登録されていますが ZIP/Excel 形式のため変換していません')
        return info
    text, enc = decode(raw)
    if text is None:
        warnings.append(f'{label}: 文字コードを判定できないため変換していません')
        return info
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    write_text(paths['utf8'], text)
    info['utf8'] = paths['utf8'].relative_to(ROOT).as_posix()
    info['encoding'] = enc
    records = parse_csv(text)
    if records is None:
        warnings.append(f'{label}: CSV として解析できませんでした')
        return info
    header, rows = table_of(text)
    info['rows'], info['columns'] = len(rows), header
    umap = to_umap(records, label, warnings)
    if umap is not None:
        write_text(paths['umap'], umap)
        info['umap'] = paths['umap'].relative_to(ROOT).as_posix()
    return info


def read_old_utf8(old_file):
    if old_file and old_file.get('utf8'):
        p = ROOT / old_file['utf8']
        if p.exists():
            return p.read_text(encoding='utf-8')
    return None


def remove_files(file_info):
    for k in ('raw', 'utf8', 'umap'):
        if file_info and file_info.get(k):
            p = ROOT / file_info[k]
            p.unlink(missing_ok=True)
            try:
                p.parent.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------- 更新処理

def is_target(res):
    return res['format'] == 'csv' and urllib.parse.urlparse(res['url']).hostname == PREF_HOST


def update_pref(old, new, force, report):
    """CSV を取得・変換し、new の各リソースに 'file' を設定する."""
    for dsid, ds in new.items():
        old_ds = old.get(dsid) or {'resources': {}}
        for rid, res in ds['resources'].items():
            old_res = old_ds['resources'].get(rid)
            old_file = (old_res or {}).get('file')
            res['file'] = None
            if not is_target(res):
                remove_files(old_file)
                continue
            label = f'{ds["title"]} / {res["name"]}'
            if res['size'] and res['size'] > MAX_BYTES:
                report['warnings'].append(f'{label}: サイズ超過（{res["size"]:,} bytes）のため取得していません')
                continue
            meta_changed = old_res is None or any(old_res.get(k) != res[k] for k in ('url', 'size', 'last_modified'))
            raw_exists = old_file and (ROOT / old_file['raw']).exists()
            if not (force or meta_changed or not raw_exists):
                res['file'] = old_file
                continue
            time.sleep(REQUEST_INTERVAL)
            try:
                raw = http_get(res['url'])
                if len(raw) > MAX_BYTES:
                    raise ValueError('サイズ超過')
            except Exception as e:
                report['errors'].append(f'{label}: ダウンロード失敗（{e}）')
                if old_res:  # 次回に再試行するよう前回のメタデータを残す
                    ds['resources'][rid] = old_res
                continue
            same = old_file and old_file.get('sha256') == hashlib.sha256(raw).hexdigest() and raw_exists
            if same and not force:
                res['file'] = old_file
                continue
            old_text = read_old_utf8(old_file)
            res['file'] = process_csv(dsid, rid, raw, label, report['warnings'])
            if old_file and not same:
                res['content_diff'] = csv_diff(old_text, read_old_utf8(res['file']))
    # 削除されたデータセット・リソースのファイルを消す
    for dsid, old_ds in old.items():
        for rid, old_res in old_ds['resources'].items():
            if rid not in (new.get(dsid) or {'resources': {}})['resources']:
                remove_files(old_res.get('file'))


def fmt_size(n):
    if n is None:
        return '不明'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f}{unit}' if unit == 'B' else f'{n:.1f}{unit}'
        n /= 1024


def fmt_date(s):
    return (s or '')[:10] or '不明'


def describe_res(res):
    fmt = (res['format'] or '形式不明').upper()
    f = res.get('file')
    if f and f.get('rows') is not None:
        return f'{res["name"]}（{fmt}・{f["rows"]:,}行）'
    return f'{res["name"]}（{fmt}・{fmt_size(res["size"])}）'


def diff_catalog(source, old, new):
    """データセット単位の変更点を items のリストで返す."""
    items = []
    url_of = (lambda ds: PREF_DATASET_URL.format(ds['id'])) if source == 'pref' else (lambda ds: GSP_DATASET_URL.format(ds['name']))
    for key in sorted(set(new) - set(old), key=lambda k: new[k]['title']):
        ds = new[key]
        items.append({
            'source': source, 'kind': 'added', 'title': ds['title'], 'url': url_of(ds),
            'details': [f'ファイル: {describe_res(r)}' for r in ds['resources'].values()],
            'notes': ds['notes'], 'files': [],
        })
    for key in sorted(set(old) - set(new), key=lambda k: old[k]['title']):
        ds = old[key]
        items.append({'source': source, 'kind': 'removed', 'title': ds['title'], 'url': url_of(ds),
                      'details': [], 'notes': '', 'files': []})
    for key in sorted(set(old) & set(new), key=lambda k: new[k]['title']):
        o, n = old[key], new[key]
        details, files = [], []
        if o['title'] != n['title']:
            details.append(f'タイトル変更: {o["title"]} → {n["title"]}')
        if o.get('notes') != n.get('notes'):
            details.append('説明文が変更されました')
        if o.get('frequency') != n.get('frequency'):
            details.append(f'更新頻度: {o.get("frequency") or "未設定"} → {n.get("frequency") or "未設定"}')
        if o.get('landing_page') != n.get('landing_page'):
            details.append(f'掲載ページ: {n.get("landing_page") or "なし"}')
        for rid in n['resources'].keys() - o['resources'].keys():
            details.append(f'ファイル追加: {describe_res(n["resources"][rid])}')
        for rid in o['resources'].keys() - n['resources'].keys():
            details.append(f'ファイル削除: {o["resources"][rid]["name"]}')
        for rid in sorted(o['resources'].keys() & n['resources'].keys()):
            ro, rn = o['resources'][rid], n['resources'][rid]
            cd = rn.pop('content_diff', None)
            changed = []
            if ro['name'] != rn['name']:
                changed.append(f'名称変更（旧: {ro["name"]}）')
            if ro['last_modified'] != rn['last_modified']:
                changed.append(f'最終更新日 {fmt_date(ro["last_modified"])} → {fmt_date(rn["last_modified"])}')
            if ro['size'] != rn['size'] and not cd:
                changed.append(f'サイズ {fmt_size(ro["size"])} → {fmt_size(rn["size"])}')
            if ro['url'] != rn['url']:
                changed.append('URL変更')
            if cd:
                changed.append(f'行数 {cd["rows_before"]:,} → {cd["rows_after"]:,}'
                               f'（追加 {cd["rows_added"]:,}・削除 {cd["rows_removed"]:,}）')
                if cd['columns_added']:
                    changed.append('列追加: ' + '、'.join(cd['columns_added']))
                if cd['columns_removed']:
                    changed.append('列削除: ' + '、'.join(cd['columns_removed']))
                if not cd['rows_added'] and not cd['rows_removed'] and not cd['columns_added'] and not cd['columns_removed']:
                    changed.append('内容が変更されました（行の並び順・書式・ヘッダ前の行など）')
            if changed:
                details.append(f'ファイル更新: {rn["name"]} — ' + '、'.join(changed))
                if cd and (cd['sample_added'] or cd['sample_removed']):
                    files.append({'name': rn['name'], **{k: cd[k] for k in ('header', 'old_header', 'sample_added', 'sample_removed', 'rows_added', 'rows_removed')}})
        if not details and o.get('metadata_modified') != n.get('metadata_modified'):
            details.append(f'メタデータ更新日のみ変更（{fmt_date(o.get("metadata_modified"))} → {fmt_date(n.get("metadata_modified"))}）')
        if details:
            items.append({'source': source, 'kind': 'updated', 'title': n['title'], 'url': url_of(n),
                          'details': details, 'notes': '', 'files': files})
    for ds in new.values():  # 初回取得などで残った content_diff を除く
        for r in ds['resources'].values():
            r.pop('content_diff', None)
    return items


# ---------------------------------------------------------------- レポート

KIND_LABEL = {'added': '追加', 'updated': '更新', 'removed': '削除'}


def summarize(items):
    c = collections.Counter(i['kind'] for i in items)
    parts = [f'{KIND_LABEL[k]}{c[k]}件' for k in ('added', 'updated', 'removed') if c[k]]
    return 'データセット ' + '・'.join(parts) if parts else '変更なし'


def headline(items, initial):
    """フィードの見出し: 「医療機関一覧」ほか3件を更新 など."""
    if initial:
        return f'初回取得（{len(items)}データセット）'
    order = {'added': 0, 'updated': 1, 'removed': 2}
    top = sorted(items, key=lambda i: (i['source'] != 'pref', order[i['kind']]))[0]
    name = top['title'].removeprefix('【さいたま市】')
    kinds = {i['kind'] for i in items}
    verb = KIND_LABEL[top['kind']] if len(kinds) == 1 else '更新'
    return f'「{name}」を{verb}' if len(items) == 1 else f'「{name}」ほか{len(items) - 1}件を{verb}'


def md_cell(v):
    v = str(v).replace('\n', ' ').replace('|', '\\|')
    return v if len(v) <= 40 else v[:39] + '…'


def md_table(header, rows):
    width = max([len(header)] + [len(r) for r in rows])
    header = list(header) + [''] * (width - len(header))
    lines = ['| ' + ' | '.join(md_cell(c) or ' ' for c in header) + ' |', '|' + '---|' * width]
    for r in rows:
        r = list(r) + [''] * (width - len(r))
        lines.append('| ' + ' | '.join(md_cell(c) for c in r) + ' |')
    return '\n'.join(lines)


def render_md(rep):
    out = [f'# {rep["title"]}', '', f'- 確認日時: {rep["checked_at"]}', f'- 概要: {rep["summary"]}', '']
    if rep.get('note'):
        out += [rep['note'], '']
    for source in ('pref', 'gsp'):
        items = [i for i in rep['items'] if i['source'] == source]
        if not items:
            continue
        out += [f'## {SOURCE_NAMES[source]}', '']
        for kind in ('added', 'updated', 'removed'):
            ks = [i for i in items if i['kind'] == kind]
            if not ks:
                continue
            out += [f'### {KIND_LABEL[kind]}（{len(ks)}件）', '']
            for i in ks:
                out.append(f'#### [{i["title"]}]({i["url"]})')
                out.append('')
                if i['notes']:
                    out += [i['notes'].strip().replace('\n', ' '), '']
                out += [f'- {d}' for d in i['details']]
                out.append('')
                for f in i['files']:
                    for key, label, n in (('sample_added', '追加された行', 'rows_added'), ('sample_removed', '削除された行', 'rows_removed')):
                        if f[key]:
                            header = f['header'] if key == 'sample_added' else f['old_header']
                            out += [f'<details><summary>{html.escape(f["name"])}: {label}（{f[n]:,}件中 先頭{len(f[key])}件）</summary>', '',
                                    md_table(header, f[key]), '', '</details>', '']
    if rep['warnings']:
        out += ['## 注意', ''] + [f'- {w}' for w in rep['warnings']] + ['']
    if rep['errors']:
        out += ['## エラー', ''] + [f'- {e}' for e in rep['errors']] + ['']
    return '\n'.join(out)


def render_html(rep):
    out = [f'<p>{html.escape(rep["summary"])}</p>']
    if rep.get('note'):
        out.append(f'<p>{html.escape(rep["note"])}</p>')
    for source in ('pref', 'gsp'):
        items = [i for i in rep['items'] if i['source'] == source]
        if not items:
            continue
        out.append(f'<h2>{html.escape(SOURCE_NAMES[source])}</h2><ul>')
        for i in items:
            details = ''.join(f'<li>{html.escape(d)}</li>' for d in i['details'])
            out.append(f'<li>【{KIND_LABEL[i["kind"]]}】<a href="{html.escape(i["url"])}">{html.escape(i["title"])}</a>'
                       + (f'<ul>{details}</ul>' if details else '') + '</li>')
        out.append('</ul>')
    out.append(f'<p><a href="{html.escape(rep["url"])}">詳細なレポート</a></p>')
    return ''.join(out)


def report_path(now):
    base = REPORTS / f'{now:%Y}' / f'{now:%Y-%m-%d}'
    path, n = base, 1
    while path.with_suffix('.json').exists():
        n += 1
        path = base.with_name(f'{base.name}-{n}')
    return path


def load_reports():
    reps = [json.loads(p.read_text(encoding='utf-8')) for p in REPORTS.glob('*/*.json')]
    return sorted(reps, key=lambda r: r['checked_at'], reverse=True)


def write_feeds(reps):
    DOCS.mkdir(exist_ok=True)
    reps = reps[:FEED_ENTRIES]
    title = 'さいたま市オープンデータ更新情報'
    desc = '埼玉県オープンデータポータル・G空間情報センターに公開されたさいたま市オープンデータの更新日記'
    updated = reps[0]['checked_at'] if reps else dt.datetime.now(JST).isoformat(timespec='seconds')
    feed_json = {
        'version': 'https://jsonfeed.org/version/1.1',
        'title': title,
        'description': desc,
        'home_page_url': REPO_URL,
        'feed_url': f'{RAW_URL}/docs/feed.json',
        'language': 'ja',
        'items': [{
            'id': r['id'], 'url': r['url'], 'title': r['title'],
            'summary': r['summary'], 'content_html': render_html(r),
            'date_published': r['checked_at'],
            '_saitama': {'counts': r['counts'], 'datasets': [
                {'source': i['source'], 'kind': i['kind'], 'title': i['title'], 'url': i['url']} for i in r['items']]},
        } for r in reps],
    }
    (DOCS / 'feed.json').write_text(json.dumps(feed_json, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    e = html.escape
    entries = ''.join(
        f'  <entry>\n    <id>{e(r["id"])}</id>\n    <title>{e(r["title"])}</title>\n'
        f'    <link rel="alternate" href="{e(r["url"])}"/>\n    <updated>{r["checked_at"]}</updated>\n'
        f'    <summary>{e(r["summary"])}</summary>\n    <content type="html">{e(render_html(r))}</content>\n  </entry>\n'
        for r in reps)
    atom = ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="ja">\n'
            f'  <id>{REPO_URL}</id>\n  <title>{e(title)}</title>\n  <subtitle>{e(desc)}</subtitle>\n'
            f'  <link rel="alternate" href="{REPO_URL}"/>\n  <link rel="self" href="{RAW_URL}/docs/feed.xml"/>\n'
            f'  <updated>{updated}</updated>\n  <author><name>{e(REPO)}</name></author>\n{entries}</feed>\n')
    (DOCS / 'feed.xml').write_text(atom, encoding='utf-8')


def write_report_index(reps):
    lines = ['# 更新レポート', '', 'さいたま市オープンデータの更新日記です（新しい順）。', '',
             f'- フィード: [Atom]({RAW_URL}/docs/feed.xml) / [JSON Feed]({RAW_URL}/docs/feed.json)', '']
    for r in reps:
        rel = Path(r['path']).relative_to('reports').as_posix()
        lines.append(f'- [{r["checked_at"][:10]}]({rel}) {r["summary"]}')
    write_text(REPORTS / 'README.md', '\n'.join(lines) + '\n')


def write_catalog_md(pref, gsp, latest):
    out = ['# データ一覧', '', '`scripts/update.py` が自動生成します。手で編集しないでください。', '']
    if latest:
        out += [f'最新の更新レポート: [{latest["checked_at"][:10]}]({latest["path"]})', '']
    out += [f'## 埼玉県オープンデータポータル（さいたま市公開分・{len(pref)}件）', '',
            'CSV を取得し、`data/raw`（元ファイル）・`data/utf8`（UTF-8変換）・`data/umap`（緯度経度を lat,lon に変換）に保存しています。', '',
            '| ID | データセット | 更新頻度 | 最終更新 | ファイル |', '|---|---|---|---|---|']
    for ds in sorted(pref.values(), key=lambda d: d['id']):
        files = []
        for r in ds['resources'].values():
            f = r.get('file')
            if f:
                links = [f'[raw]({f["raw"]})'] + [f'[{k}]({f[k]})' for k in ('utf8', 'umap') if f.get(k)]
                rows = f'{f["rows"]:,}行・' if f.get('rows') is not None else ''
                files.append(f'{md_cell(r["name"])}（{rows}{"・".join(links)}）')
            else:
                files.append(f'{md_cell(r["name"])}（{(r["format"] or "?").upper()}・[元ファイル]({r["url"]})）')
        out.append(f'| {ds["id"]} | [{md_cell(ds["title"])}]({PREF_DATASET_URL.format(ds["id"])}) | '
                   f'{md_cell(ds["frequency"] or "")} | {fmt_date(ds["metadata_modified"])} | {"<br>".join(files)} |')
    out += ['', f'## G空間情報センター（さいたま市公開分・{len(gsp)}件・一覧のみ）', '',
            '容量が大きいため、ファイルは取得せずメタデータのみ記録しています。', '',
            '| データセット | 更新日 | ファイル |', '|---|---|---|']
    for ds in sorted(gsp.values(), key=lambda d: d['metadata_modified'], reverse=True):
        fmts = collections.Counter((r['format'] or '?').upper() for r in ds['resources'].values())
        out.append(f'| [{md_cell(ds["title"])}]({GSP_DATASET_URL.format(ds["name"])}) | {fmt_date(ds["metadata_modified"])} | '
                   + '、'.join(f'{k}×{v}' for k, v in sorted(fmts.items())) + ' |')
    write_text(CATALOG_MD, '\n'.join(out) + '\n')


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--force', action='store_true', help='メタデータに変化がなくても全 CSV を再取得する')
    ap.add_argument('--note', help='レポート冒頭に載せる補足文')
    ap.add_argument('--commit-message', type=Path, help='コミットメッセージの出力先')
    args = ap.parse_args()

    old = json.loads(CATALOG.read_text(encoding='utf-8')) if CATALOG.exists() else {'pref': {}, 'gsp': {}}
    report = {'warnings': [], 'errors': []}

    try:
        pref = fetch_pref()
    except Exception as e:
        print(f'致命的エラー: 県ポータルのメタデータを取得できません: {e}', file=sys.stderr)
        return 1
    if not pref or len(pref) < len(old['pref']) / 2:
        print(f'致命的エラー: データセット数が異常です（前回 {len(old["pref"])} → 今回 {len(pref)}）', file=sys.stderr)
        return 1
    try:
        gsp = fetch_gsp()
    except Exception as e:
        report['errors'].append(f'G空間情報センターのメタデータを取得できません（{e}）')
        gsp = old['gsp']

    update_pref(old['pref'], pref, args.force, report)
    items = diff_catalog('pref', old['pref'], pref) + diff_catalog('gsp', old['gsp'], gsp)

    now = dt.datetime.now(JST).replace(microsecond=0)
    catalog = {'pref': pref, 'gsp': gsp}
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')

    rep = None
    if items:
        path = report_path(now)
        rel = path.relative_to(ROOT).as_posix()
        rep = {
            'id': f'tag:github.com,{now:%Y}:{REPO}/{rel}',
            'path': f'{rel}.md',
            'url': f'{REPO_URL}/blob/main/{rel}.md',
            'checked_at': now.isoformat(),
            'title': f'{now:%Y-%m-%d} {headline(items, not old["pref"])}',
            'summary': summarize(items),
            'counts': dict(collections.Counter(i['kind'] for i in items)),
            'note': args.note or '',
            'items': items,
            'warnings': report['warnings'],
            'errors': report['errors'],
        }
        write_text(path.with_suffix('.json'), json.dumps(rep, ensure_ascii=False, indent=1) + '\n')
        write_text(path.with_suffix('.md'), render_md(rep))
    reps = load_reports()
    write_report_index(reps)
    write_feeds(reps)
    write_catalog_md(pref, gsp, reps[0] if reps else None)

    text = render_md(rep) if rep else '# 変更なし\n\n' + '\n'.join(f'- {m}' for m in report['warnings'] + report['errors'])
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(text + '\n')
    if args.commit_message:
        msg = f'データ更新: {rep["summary"]}\n\n{rep["url"]}\n' if rep else 'データ更新\n'
        args.commit_message.write_text(msg, encoding='utf-8')
    return 2 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
