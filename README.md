# さいたま市オープンデータ

さいたま市が公開しているオープンデータを毎日チェックし、取得・加工して保存しています。
変更があった日は、何が変わったかを日記のように[更新レポート](reports/README.md)に残し、フィードでも配信します。

- [データ一覧（CATALOG.md）](CATALOG.md)
- [更新レポート](reports/README.md)
- フィード: [Atom](https://raw.githubusercontent.com/CivicTechSaitamaCity/SaitamaCityOpendata/main/docs/feed.xml) / [JSON Feed](https://raw.githubusercontent.com/CivicTechSaitamaCity/SaitamaCityOpendata/main/docs/feed.json)

## 情報源

さいたま市は自らのサイトでオープンデータ一覧を掲載しておらず、以下の 2 サイトで公開しています（[さいたま市／オープンデータ一覧](https://www.city.saitama.lg.jp/006/013/014/003/p068594.html)）。

| 情報源 | 取得内容 |
|---|---|
| [埼玉県オープンデータポータルサイト（さいたま市公開分）](https://opendata.pref.saitama.lg.jp/datasets?organization_id=25) | CKAN 互換 API でメタデータを取得し、CSV ファイルをダウンロード |
| [G空間情報センター（さいたま市公開分）](https://www.geospatial.jp/ckan/organization/saitama-111007) | 容量が大きいため、データセット一覧（メタデータ）のみ記録 |

PDF・ZIP・画像など CSV 以外のファイルは取得せず、`CATALOG.md` に元ファイルへのリンクを載せています。
データの利用条件は各サイトの利用規約・各データセットのライセンスに従ってください。

## 構成

```
data/
  raw/<データセットID>/<ファイルID>.csv   取得したままのファイル
  utf8/<データセットID>/<ファイルID>.csv  UTF-8（BOMなし・改行LF）に変換したもの
  umap/<データセットID>/<ファイルID>.csv  緯度経度を持つものを uMap 用に変換したもの
catalog.json      データセット・ファイルのメタデータ、ハッシュ、行数など（機械向け）
CATALOG.md        データ一覧（人向け）
reports/          更新レポート（YYYY/YYYY-MM-DD.md と同名の .json）
docs/             フィード（feed.xml / feed.json）
scripts/update.py 取得・変換・レポート作成スクリプト
```

データセット ID・ファイル ID は埼玉県オープンデータポータルの ID です（例: `data/raw/1227/…` は <https://opendata.pref.saitama.lg.jp/datasets/1227>）。

### uMap 用の変換

- `緯度`・`北緯`・`lat` などの列を `lat`、`経度`・`東経`・`lon` などの列を `lon` に置き換えます。
- ヘッダの前にあるタイトル行などは取り除きます。
- ヘッダより前の行に「日本測地系」と書かれているデータ（消火栓・防火水槽など）は、国土地理院の近似式で世界測地系に変換します（誤差は数 m 程度）。

## 更新の仕組み

現在は手元の環境で `scripts/update.py` を実行し、結果を main に push しています（[ローカルでの実行](#ローカルでの実行)）。

> GitHub Actions のワークフロー（[.github/workflows/update.yml](.github/workflows/update.yml)、毎日 6:00 JST）も用意していますが、埼玉県オープンデータポータルが GitHub Actions からのアクセスを 403 で拒否するため、無効にしています。

`scripts/update.py` は次の処理を行います。

1. 両サイトの API からさいたま市分のメタデータを取得し、前回の `catalog.json` と比較する
2. 最終更新日・サイズ・URL が変わった CSV だけをダウンロードし、SHA-256 で内容の変化を確認する
3. 変化したファイルを UTF-8・uMap 用に変換し、行・列の差分を計算する
4. 変更があれば `reports/` にレポートを書き、フィード・`CATALOG.md` を更新する

ダウンロードに失敗したファイルは前回の状態を残して次回に再試行し、終了コード 2 で終わります。

### レポートとフィード

レポートには、データセットの追加・削除、タイトルや更新頻度の変更、ファイルごとの行数の増減・列の増減、追加・削除された行のサンプルを記録します。
`reports/YYYY/YYYY-MM-DD.json` は同じ内容の構造化データです。

フィードは直近 50 件のレポートを配信します。JSON Feed の各項目には、変更されたデータセットの一覧（`_saitama.datasets`）も入っています。

## ローカルでの実行

Python 3.11 以上（標準ライブラリのみ）で動きます。

```sh
git pull --ff-only
python3 scripts/update.py --commit-message /tmp/msg.txt   # 差分取得（--force で全件再取得・再変換）
git add -A data catalog.json CATALOG.md reports docs
git diff --cached --quiet || { git commit -F /tmp/msg.txt && git push; }
```

## 旧データ

2022〜2023 年に手作業で取得したデータ（旧 `raw/`・`utf8/`・`umap/`）は、タグ [`v2022-snapshot`](https://github.com/CivicTechSaitamaCity/SaitamaCityOpendata/tree/v2022-snapshot) から参照できます。
