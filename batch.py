"""
batch.py v3
Webサイト直接取得版
https://prtimes.jp/gourmet/ から記事URL一覧を取得
→ 各記事のh1・h2を取得 → all_results.csvに追記
GitHub Actionsから定期実行される
"""

import os
import re
import csv
import time
import datetime
import requests
from bs4 import BeautifulSoup
from typing import List, Dict

# ============================================================
# 設定
# ============================================================
LISTING_URL = "https://prtimes.jp/gourmet/"
OUTPUT_CSV = "results/all_results.csv"
ARTICLE_PATH_PATTERN = re.compile(r"^/main/html/rd/p/")
ACCESS_INTERVAL = 2  # 秒

# ============================================================
# ユーティリティ
# ============================================================
def get_jst_now() -> datetime.datetime:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst)

def normalize_string(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.replace(" ", "").replace("　", "").lower().strip()

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
    fieldnames = ["ID", "url", "h1", "h2", "crawled_at"]
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
def fetch_article_urls(listing_url: str) -> List[str]:
    try:
        r = requests.get(listing_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        urls = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ARTICLE_PATH_PATTERN.match(href):
                full_url = f"https://prtimes.jp{href}" if href.startswith("/") else href
                if full_url not in seen:
                    seen.add(full_url)
                    urls.append(full_url)
        print(f"記事URL取得: {len(urls)}件")
        return urls
    except Exception as e:
        print(f"一覧取得エラー: {e}")
        return []

# ============================================================
# 記事詳細取得
# ============================================================
def fetch_article_detail(url: str) -> Dict:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        h1 = soup.find("h1")
        h1_text = h1.get_text(strip=True) if h1 else ""

        h2 = soup.find("h2")
        h2_text = h2.get_text(strip=True) if h2 else ""

        return {"h1": h1_text, "h2": h2_text}
    except Exception as e:
        print(f"記事取得エラー {url}: {e}")
        return {"h1": "", "h2": ""}

# ============================================================
# メイン処理
# ============================================================
def main():
    print("=== batch.py v3 (Web直接取得版) ===")

    existing_urls = load_existing_urls(OUTPUT_CSV)
    print(f"既存データ: {len(existing_urls)}件（重複除外用）")

    # 1. 記事URL一覧取得
    article_urls = fetch_article_urls(LISTING_URL)
    if not article_urls:
        print("記事URLが取得できませんでした")
        return

    # 2. 新規URLのみ処理
    new_urls = [u for u in article_urls if u not in existing_urls]
    print(f"新規記事: {len(new_urls)}件")
    if not new_urls:
        print("新規記事はありませんでした")
        return

    # 3. 各記事の詳細取得
    next_id = get_next_id(OUTPUT_CSV)
    new_count = 0
    crawled_at = get_jst_now().strftime("%Y年%m月%d日 %H:%M")

    for i, url in enumerate(new_urls):
        print(f"取得中 ({i+1}/{len(new_urls)}): {url}")
        detail = fetch_article_detail(url)

        data = {
            "ID": next_id,
            "url": url,
            "h1": detail["h1"],
            "h2": detail["h2"],
            "crawled_at": crawled_at,
        }
        append_to_csv(data, OUTPUT_CSV)
        next_id += 1
        new_count += 1
        time.sleep(ACCESS_INTERVAL)

    print(f"完了！新規追加: {new_count}件 → {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
