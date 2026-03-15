"""
batch.py v5
マルチサイト・マルチジャンル対応版 + Gemini情報抽出
GitHub Actionsから2時間ごとに定期実行される
"""

import os
import re
import csv
import json
import time
import datetime
import requests
from bs4 import BeautifulSoup
from typing import List, Dict
from google import genai
from google.genai import types

# ============================================================
# 設定
# ============================================================
OUTPUT_CSV = "results/all_results.csv"
ACCESS_INTERVAL = 2  # 秒
MODEL_NAME = "gemini-2.0-flash"
TEST_MODE = True   # ← Trueで10件取得後に終了、本番運用時はFalseに
TEST_LIMIT = 10

TARGETS = [
    {
        "site":    "https://prtimes.jp/",
        "genre":   "グルメ",
        "listing": "https://prtimes.jp/gourmet/",
        "article_pattern": re.compile(r"^/main/html/rd/p/"),
        "base_url": "https://prtimes.jp",
        "parser":  "prtimes",
    },
    {
        "site":    "https://prtimes.jp/",
        "genre":   "エンタメ",
        "listing": "https://prtimes.jp/entertainment/",
        "article_pattern": re.compile(r"^/main/html/rd/p/"),
        "base_url": "https://prtimes.jp",
        "parser":  "prtimes",
    },
    {
        "site":    "https://www.atpress.ne.jp/",
        "genre":   "旅行",
        "listing": "https://www.atpress.ne.jp/news/travel",
        "article_pattern": re.compile(r"^/news/\d+$"),
        "base_url": "https://www.atpress.ne.jp",
        "parser":  "atpress",
    },
    {
        "site":    "https://www.atpress.ne.jp/",
        "genre":   "グルメ",
        "listing": "https://www.atpress.ne.jp/news/food",
        "article_pattern": re.compile(r"^/news/\d+$"),
        "base_url": "https://www.atpress.ne.jp",
        "parser":  "atpress",
    },
]

# ============================================================
# ユーティリティ
# ============================================================
def normalize_datetime(text: str) -> str:
    if not text:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", text)
    if m:
        return f"{m.group(1)}年{m.group(2)}月{m.group(3)}日 {m.group(4)}:{m.group(5)}"
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{2}):(\d{2})", text)
    if m:
        return f"{m.group(1)}年{m.group(2).zfill(2)}月{m.group(3).zfill(2)}日 {m.group(4)}:{m.group(5)}"
    return text

def get_jst_now() -> datetime.datetime:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst)

def load_existing_urls(filename: str) -> set:
    urls = set()
    if not os.path.isfile(filename):
        return urls
    try:
        with open(filename, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                u = row.get("url", "").strip()
                if u:
                    urls.add(u)
    except Exception as e:
        print(f"CSV読み込みエラー: {e}")
    return urls

def get_next_id(filename: str) -> int:
    if not os.path.isfile(filename):
        return 1
    try:
        with open(filename, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            ids = [int(row["ID"]) for row in reader if row.get("ID", "").isdigit()]
            return max(ids) + 1 if ids else 1
    except Exception:
        return 1

def append_to_csv(data: Dict, filename: str):
    fieldnames = [
        "ID", "site", "genre", "url", "datetime",
        "h1", "h2",
        "event_date", "venue", "address", "fee", "note",
        "info", "official",
        "crawled_at"
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
# 記事URL一覧取得
# ============================================================
def fetch_article_urls(target: Dict) -> List[str]:
    try:
        r = requests.get(target["listing"], headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        urls = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if target["article_pattern"].match(href):
                full_url = target["base_url"] + href if href.startswith("/") else href
                if full_url not in seen:
                    seen.add(full_url)
                    urls.append(full_url)
        print(f"  [{target['genre']}] 記事URL: {len(urls)}件")
        return urls
    except Exception as e:
        print(f"  [{target['genre']}] 一覧取得エラー: {e}")
        return []

# ============================================================
# 記事詳細取得
# ============================================================
def fetch_detail_prtimes(soup: BeautifulSoup) -> Dict:
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""
    h2 = soup.find("h2")
    h2_text = h2.get_text(strip=True) if h2 else ""
    time_tag = soup.find("time", attrs={"datetime": True})
    datetime_text = time_tag["datetime"] if time_tag else ""
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    body_text = soup.get_text("\n", strip=True)[:6000]
    return {"h1": h1_text, "h2": h2_text, "datetime": datetime_text, "body": body_text}

def fetch_detail_atpress(soup: BeautifulSoup) -> Dict:
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""
    h2_text = ""
    if h1:
        next_h2 = h1.find_next("h2")
        if next_h2:
            h2_text = next_h2.get_text(strip=True)
    span = soup.find("span", id="published-at")
    datetime_text = span.get_text(strip=True) if span else ""
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    body_text = soup.get_text("\n", strip=True)[:6000]
    return {"h1": h1_text, "h2": h2_text, "datetime": datetime_text, "body": body_text}

def fetch_article_detail(url: str, parser: str) -> Dict:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        if parser == "atpress":
            return fetch_detail_atpress(soup)
        else:
            return fetch_detail_prtimes(soup)
    except Exception as e:
        print(f"  記事取得エラー {url}: {e}")
        return {"h1": "", "h2": "", "datetime": "", "body": ""}

# ============================================================
# Gemini: 記事本文から情報抽出
# ============================================================
def ai_extract_info(client, body_text: str) -> Dict:
    empty = {
        "event_date": "", "venue": "", "address": "",
        "fee": "", "note": "", "info": "", "official": ""
    }
    if not body_text:
        return empty

    prompt = f"""以下のプレスリリース本文から情報を抽出してください。
情報がない項目は空文字にしてください。

抽出項目:
- event_date: 開催日時（複数ある場合は改行区切りで全て）
- venue: 場所・会場名
- address: 住所（都道府県から番地まで）
- fee: 参加費・料金・入場料
- note: 予約方法・その他特記事項
- info: 店舗情報・会場情報・イベント概要などのまとまった基本情報ブロック（店名・所在地・営業時間・席数・設備等などが含まれる箇所）をそのまま抜き出す
- official: 公式サイトURL・SNSアカウント・予約サイトなどのリンク情報をそのまま抜き出す

必ず以下のJSON形式のみで返してください:
{{"event_date":"","venue":"","address":"","fee":"","note":"","info":"","official":""}}

本文:
{body_text}
"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            res = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            text = res.text.strip()
            # ```json ... ``` を除去
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            result = json.loads(text)
            return {
                "event_date": result.get("event_date", ""),
                "venue":      result.get("venue", ""),
                "address":    result.get("address", ""),
                "fee":        result.get("fee", ""),
                "note":       result.get("note", ""),
                "info":       result.get("info", ""),
                "official":   result.get("official", ""),
            }
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                wait = 15 * (attempt + 1)
                print(f"  429エラー、{wait}秒後にリトライ ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                print(f"  Gemini抽出エラー: {e}")
                print(f"  レスポンス: {res.text[:200] if 'res' in dir() else 'N/A'}")
                break
    return empty

# ============================================================
# メイン処理
# ============================================================
def main():
    print(f"=== batch.py v5 ({'テストモード: ' + str(TEST_LIMIT) + '件' if TEST_MODE else '本番モード'}) ===")

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY が設定されていません")
        return
    client = genai.Client(api_key=api_key)

    existing_urls = load_existing_urls(OUTPUT_CSV)
    print(f"既存データ: {len(existing_urls)}件（重複除外用）")

    next_id = get_next_id(OUTPUT_CSV)
    crawled_at = get_jst_now().strftime("%Y年%m月%d日 %H:%M")
    total_new = 0

    for target in TARGETS:
        if TEST_MODE and total_new >= TEST_LIMIT:
            break
        print(f"\n--- {target['site']} [{target['genre']}] ---")
        article_urls = fetch_article_urls(target)
        new_urls = [u for u in article_urls if u not in existing_urls]
        print(f"  新規: {len(new_urls)}件")

        for i, url in enumerate(new_urls):
            if TEST_MODE and total_new >= TEST_LIMIT:
                print(f"  テストモード: {TEST_LIMIT}件到達、終了します")
                break
            print(f"  取得中 ({i+1}/{len(new_urls)}): {url}")
            detail = fetch_article_detail(url, target["parser"])
            extracted = ai_extract_info(client, detail["body"])

            data = {
                "ID":         next_id,
                "site":       target["site"],
                "genre":      target["genre"],
                "url":        url,
                "datetime":   normalize_datetime(detail["datetime"]),
                "h1":         detail["h1"],
                "h2":         detail["h2"],
                "event_date": extracted["event_date"],
                "venue":      extracted["venue"],
                "address":    extracted["address"],
                "fee":        extracted["fee"],
                "note":       extracted["note"],
                "info":       extracted["info"],
                "official":   extracted["official"],
                "crawled_at": crawled_at,
            }
            append_to_csv(data, OUTPUT_CSV)
            existing_urls.add(url)
            next_id += 1
            total_new += 1
            time.sleep(ACCESS_INTERVAL)

    print(f"\n完了！新規追加: {total_new}件 → {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
