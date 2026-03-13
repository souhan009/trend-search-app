"""
batch.py
RSS取得 → Geminiでカテゴリー判定 → 詳細抽出 → CSVに追記
GitHub Actionsから定期実行される
"""

import os
import csv
import json
import time
import datetime
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional

import requests
from google import genai
from google.genai import types

# ============================================================
# 設定
# ============================================================
RSS_URL = "https://prtimes.jp/index.rdf"
OUTPUT_CSV = "auto_results.csv"
MODEL_NAME = "gemini-2.0-flash"
TEMPERATURE = 0.0

# 取得対象カテゴリーのキーワード（Gemini判定用）
TARGET_CATEGORIES = ["グルメ", "飲食", "レストラン", "カフェ", "イベント", "新規オープン", "新メニュー", "料理", "食品", "食べ物", "スイーツ"]

# ============================================================
# ユーティリティ
# ============================================================
def normalize_date(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{y}年{mo}月{d}日"
    return t.strip()

def normalize_string(text) -> str:
    if not isinstance(text, str):
        return ""
    t = text.replace(" ", "").replace("　", "")
    t = t.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return t.lower().strip()

def safe_json_parse(json_str: str) -> list:
    if not json_str:
        return []
    s = json_str.replace("```json", "").replace("```", "").strip()
    l = s.find("[")
    r = s.rfind("]")
    if l != -1 and r != -1 and r > l:
        try:
            obj = json.loads(s[l:r+1])
            return obj if isinstance(obj, list) else []
        except Exception:
            pass
    return []

def load_existing_fingerprints(filename: str) -> set:
    fps = set()
    if not os.path.isfile(filename):
        return fps
    try:
        with open(filename, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                n = normalize_string(row.get("name", "") or row.get("イベント名", ""))
                if n:
                    fps.add(n)
    except Exception as e:
        print(f"CSV読み込みエラー: {e}")
    return fps

def append_to_csv(data: Dict, filename: str):
    fieldnames = [
        "release_date", "date_info", "name", "place", "address",
        "latitude", "longitude", "description", "source_url"
    ]
    file_exists = os.path.isfile(filename)
    try:
        with open(filename, mode="a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)
    except Exception as e:
        print(f"CSV書き込みエラー: {e}")

# ============================================================
# RSS取得
# ============================================================
def fetch_rss(url: str) -> List[Dict]:
    print(f"RSS取得中: {url}")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "rss": "http://purl.org/rss/1.0/",
            "dc":  "http://purl.org/dc/elements/1.1/",
        }
        items = []
        for item in root.findall("rss:item", ns):
            title = item.findtext("rss:title", default="", namespaces=ns)
            link  = item.findtext("rss:link",  default="", namespaces=ns)
            desc  = item.findtext("rss:description", default="", namespaces=ns)
            date  = item.findtext("dc:date",   default="", namespaces=ns)
            items.append({
                "title": title.strip(),
                "link":  link.strip(),
                "description": desc.strip(),
                "date": date.strip(),
            })
        print(f"RSS取得完了: {len(items)}件")
        return items
    except Exception as e:
        print(f"RSS取得エラー: {e}")
        return []

# ============================================================
# Gemini: カテゴリー判定（一括・軽量）
# ============================================================
def ai_filter_chunk(client, chunk: List[Dict], offset: int) -> List[int]:
    """チャンク単位でカテゴリー判定し、該当する元のインデックスを返す"""
    lines = []
    for i, item in enumerate(chunk):
        lines.append(f"{i}: {item['title']} / {item['description'][:80]}")
    combined = "\n".join(lines)

    prompt = f"""以下はプレスリリースの一覧です。
各行の形式は「番号: タイトル / 本文冒頭」です。

【対象カテゴリー】
グルメ、飲食店、レストラン、カフェ、イベント、新規オープン、新メニュー、料理、食品、スイーツ、飲み物、お酒、ホテル

上記カテゴリーに少しでも関連する記事の番号をJSON配列で返してください。
迷った場合は含めてください。該当なしの場合は [] を返してください。
番号以外は一切出力しないでください。

一覧:
{combined}
"""
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            res = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            print(f"Gemini応答: {res.text[:200]}")
            indices = safe_json_parse(res.text)
            print(f"パース結果: {indices[:10]}")
            return [offset + i for i in indices if isinstance(i, int) and 0 <= i < len(chunk)]
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt < max_retries:
                    wait = 15 * (attempt + 1)
                    print(f"429エラー、{wait}秒後にリトライ ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
            print(f"カテゴリー判定エラー: {e}")
            break
    return []

def ai_filter_relevant_items(client, items: List[Dict]) -> List[Dict]:
    """50件ずつに分割してGeminiに渡し、関連記事だけを返す"""
    if not items:
        return []

    chunk_size = 50
    all_indices = []
    for offset in range(0, len(items), chunk_size):
        chunk = items[offset:offset + chunk_size]
        indices = ai_filter_chunk(client, chunk, offset)
        all_indices.extend(indices)
        time.sleep(2)

    relevant = [items[i] for i in all_indices if 0 <= i < len(items)]
    print(f"カテゴリー判定: {len(items)}件中 {len(relevant)}件が該当")
    return relevant

# ============================================================
# Gemini: 詳細抽出（1記事ずつ）
# ============================================================
def fetch_article_text(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code == 200:
            # 簡易テキスト抽出
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text("\n", strip=True)[:6000]
    except Exception as e:
        print(f"記事取得エラー {url}: {e}")
    return ""

def ai_extract_events(client, text: str, today: datetime.date) -> List[Dict]:
    if not text or len(text) < 100:
        return []

    prompt = f"""
以下のWebページ本文から、イベント・ニュース情報をJSON配列で漏れなく抽出してください。
【現在日付: {today}】

[抽出ルール]
- イベント（展示、催事、キャンペーン、新規オープン、新メニュー発売等）や日時・期間・場所が書かれている情報を抽出。
- date_info は YYYY年MM月DD日 または期間表現。
- address / latitude / longitude は本文から推定できる範囲でよい（不明なら空文字）。
- 出力は必ずJSONのみ。

[JSON形式]
[
  {{
    "name": "タイトル",
    "place": "場所（不明なら空文字）",
    "address": "住所（不明なら空文字）",
    "latitude": "緯度（不明なら空文字）",
    "longitude": "経度（不明なら空文字）",
    "date_info": "日付や期間（不明なら空文字）",
    "description": "概要（短めに）"
  }}
]

本文:
{text}
"""
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            res = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=TEMPERATURE
                )
            )
            extracted = safe_json_parse(res.text)
            return [
                {
                    "name": str(item.get("name", "")).strip(),
                    "place": str(item.get("place", "")).strip(),
                    "address": str(item.get("address", "")).strip(),
                    "latitude": str(item.get("latitude", "")).strip(),
                    "longitude": str(item.get("longitude", "")).strip(),
                    "date_info": normalize_date(str(item.get("date_info", "")).strip()),
                    "description": str(item.get("description", "")).strip(),
                }
                for item in extracted
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ]
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt < max_retries:
                    wait = 15 * (attempt + 1)
                    print(f"429エラー、{wait}秒後にリトライ ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
            print(f"詳細抽出エラー: {e}")
            break
    return []

# ============================================================
# メイン処理
# ============================================================
def main():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY が設定されていません")
        return

    client = genai.Client(api_key=api_key)
    today = datetime.date.today()
    existing_fps = load_existing_fingerprints(OUTPUT_CSV)
    print(f"既存データ: {len(existing_fps)}件（重複除外用）")

    # 1. RSS取得
    rss_items = fetch_rss(RSS_URL)
    if not rss_items:
        print("RSSが取得できませんでした")
        return

    # 2. Geminiでカテゴリー判定（1リクエストで200件を一括判定）
    relevant_items = ai_filter_relevant_items(client, rss_items)
    if not relevant_items:
        print("該当する記事がありませんでした")
        return

    # 3. 各記事を詳細解析
    new_count = 0
    for i, item in enumerate(relevant_items):
        print(f"詳細解析中 ({i+1}/{len(relevant_items)}): {item['title'][:50]}")

        # 記事本文を取得
        text = fetch_article_text(item["link"])
        if not text:
            # 本文取得できない場合はRSSのdescriptionで代用
            text = item["description"]

        # Geminiで詳細抽出
        events = ai_extract_events(client, text, today)

        for event in events:
            fp = normalize_string(event["name"])
            if fp in existing_fps:
                continue
            existing_fps.add(fp)

            event["release_date"] = normalize_date(item["date"])
            event["source_url"] = item["link"]
            append_to_csv(event, OUTPUT_CSV)
            new_count += 1

        time.sleep(3)  # アクセス間隔

    print(f"完了！新規追加: {new_count}件 → {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
