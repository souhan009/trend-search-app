"""
batch.py v4
マルチサイト・マルチジャンル対応版
対象:
  - PRTIMES グルメ: https://prtimes.jp/gourmet/
  - PRTIMES エンタメ: https://prtimes.jp/entertainment/
  - アットプレス 旅行: https://www.atpress.ne.jp/news/travel
  - アットプレス グルメ: https://www.atpress.ne.jp/news/food
GitHub Actionsから2時間ごとに定期実行される
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
OUTPUT_CSV = "results/all_results.csv"
ACCESS_INTERVAL = 2  # 秒

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
    fieldnames = ["ID", "site", "genre", "url", "datetime", "h1", "h2", "crawled_at"]
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
# 記事詳細取得（PRTIMESとアットプレスで処理を分ける）
# ============================================================
def fetch_detail_prtimes(soup: BeautifulSoup) -> Dict:
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""

    h2 = soup.find("h2")
    h2_text = h2.get_text(strip=True) if h2 else ""

    time_tag = soup.find("time", attrs={"datetime": True})
    datetime_text = time_tag["datetime"] if time_tag else ""

    return {"h1": h1_text, "h2": h2_text, "datetime": datetime_text}

def fetch_detail_atpress(soup: BeautifulSoup) -> Dict:
    # h1タグ最初のもの
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""

    # h1直後のh2
    h2_text = ""
    if h1:
        next_h2 = h1.find_next("h2")
        if next_h2:
            h2_text = next_h2.get_text(strip=True)

    # span id="published-at"
    span = soup.find("span", id="published-at")
    datetime_text = span.get_text(strip=True) if span else ""

    return {"h1": h1_text, "h2": h2_text, "datetime": datetime_text}

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
        return {"h1": "", "h2": "", "datetime": ""}

# ============================================================
# メイン処理
# ============================================================
def main():
    print("=== batch.py v4 (マルチサイト版) ===")

    existing_urls = load_existing_urls(OUTPUT_CSV)
    print(f"既存データ: {len(existing_urls)}件（重複除外用）")

    next_id = get_next_id(OUTPUT_CSV)
    crawled_at = get_jst_now().strftime("%Y年%m月%d日 %H:%M")
    total_new = 0

    for target in TARGETS:
        print(f"\n--- {target['site']} [{target['genre']}] ---")
        article_urls = fetch_article_urls(target)
        new_urls = [u for u in article_urls if u not in existing_urls]
        print(f"  新規: {len(new_urls)}件")

        for i, url in enumerate(new_urls):
            print(f"  取得中 ({i+1}/{len(new_urls)}): {url}")
            detail = fetch_article_detail(url, target["parser"])

            data = {
                "ID":         next_id,
                "site":       target["site"],
                "genre":      target["genre"],
                "url":        url,
                "datetime":   detail["datetime"],
                "h1":         detail["h1"],
                "h2":         detail["h2"],
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
