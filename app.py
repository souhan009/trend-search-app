import streamlit as st
import datetime
import os
import json
import time
import re
import urllib.parse
from typing import List, Dict, Tuple, Optional, Set

import pandas as pd
import requests
from bs4 import BeautifulSoup

from google import genai
from google.genai import types

# ============================================================
# Streamlit config
# ============================================================
st.set_page_config(page_title="イベント情報「全件網羅」抽出アプリ（完成版）", page_icon="📖", layout="wide")
st.title("📖 イベント情報「全件網羅」抽出アプリ（完成版）")
st.markdown("""
**AI × スマートクローリング（完成版）**  
一覧ページのカードから**記事URLを収集** → 記事ページ本文を**AIで抽出**し、重複を除外して一覧化します。
""")

# ============================================================
# Utils
# ============================================================

def normalize_date(text: str) -> str:
    """日付をゼロ埋めでなるべく揃える（文字列のまま扱う）"""
    if not text or not isinstance(text, str):
        return ""

    # 2025年1月2日 -> 2025年01月02日
    def rep_ymd(m):
        return f"{m.group(1)}年{m.group(2).zfill(2)}月{m.group(3).zfill(2)}日"
    text = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", rep_ymd, text)

    # 2025/1/2 -> 2025/01/02
    text = re.sub(
        r"(\d{4})/(\d{1,2})/(\d{1,2})",
        lambda m: f"{m.group(1)}/{m.group(2).zfill(2)}/{m.group(3).zfill(2)}",
        text
    )

    return text.strip()

def normalize_string(text) -> str:
    if not isinstance(text, str):
        return ""
    t = text.replace(" ", "").replace("　", "")
    t = t.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return t.lower().strip()

def safe_json_parse(json_str: str) -> List[Dict]:
    """Gemini出力の揺れに耐えるJSON救出。リスト抽出→辞書抽出の順。"""
    if not json_str or not isinstance(json_str, str):
        return []
    s = json_str.replace("```json", "").replace("```", "").strip()

    # list candidate
    l = s.find("[")
    r = s.rfind("]")
    if l != -1 and r != -1 and r > l:
        cand = s[l:r+1]
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, list) else []
        except:
            pass

    # dict candidate
    l = s.find("{")
    r = s.rfind("}")
    if l != -1 and r != -1 and r > l:
        cand = s[l:r+1]
        try:
            obj = json.loads(cand)
            return [obj] if isinstance(obj, dict) else []
        except:
            pass

    return []

def clean_soup(soup: BeautifulSoup) -> None:
    """不要要素を削除してテキスト抽出を安定させる"""
    for tag in soup.find_all(["script", "style", "nav", "footer", "iframe", "header", "noscript", "svg"]):
        tag.decompose()

    exclude_tokens = ["sidebar", "ranking", "recommend", "widget", "ad", "bread", "breadcrumb", "banner"]
    for tag in soup.find_all(attrs={"class": True}):
        cls_list = tag.get("class") or []
        cls = " ".join(cls_list).lower()
        if any(tok in cls for tok in exclude_tokens):
            tag.decompose()

def split_text_into_chunks(text: str, chunk_size=8000, overlap=400):
    if not text:
        return
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        yield text[start:end]
        start = max(end - overlap, end)

def is_valid_href(href: str) -> bool:
    if not href:
        return False
    h = href.strip()
    if h.startswith("#"):
        return False
    if h.lower().startswith("javascript:"):
        return False
    return True

def same_domain(url_a: str, url_b: str) -> bool:
    try:
        return urllib.parse.urlparse(url_a).netloc == urllib.parse.urlparse(url_b).netloc
    except:
        return False

def find_next_page_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """「次へ」「もっと見る」リンクを探す。誤爆を減らすため優先順をつける。"""
    next_url = None

    # 1) rel=next
    link_next = soup.find("link", rel="next")
    if link_next and link_next.get("href") and is_valid_href(link_next["href"]):
        next_url = link_next["href"]

    # 2) 明示ボタン（よくある）
    if not next_url:
        selectors = [
            "a[rel='next']",
            "a.next",
            "a.pagination__next",
            "a.pager-next",
            "a:contains('次へ')",
        ]
        # BeautifulSoupは:containsが効かないので、テキスト含みで拾う
        for a in soup.find_all("a", href=True):
            txt = a.get_text(strip=True)
            cls = " ".join(a.get("class") or []).lower()
            if any(k in txt for k in ["次へ", "次の", "Next", "NEXT"]) or any(k in cls for k in ["next", "more"]):
                href = a.get("href")
                if href and is_valid_href(href):
                    next_url = href
                    break

    if next_url:
        joined = urllib.parse.urljoin(current_url, next_url)
        # 同一ドメイン優先（違うなら無効扱い）
        if same_domain(joined, current_url):
            return joined
    return None

def extract_article_links_from_listing(
    soup: BeautifulSoup,
    current_url: str,
    link_limit: int = 50
) -> List[str]:
    """
    一覧ページから記事URL候補を抽出。
    - ドメイン内のみ
    - 明らかに一覧やタグ、ログイン等は除外
    """
    base = urllib.parse.urlparse(current_url)
    candidates: List[str] = []

    # まず "記事カードっぽい" aタグから多めに拾う（汎用）
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not is_valid_href(href):
            continue
        url = urllib.parse.urljoin(current_url, href)
        pu = urllib.parse.urlparse(url)

        # 同一ドメインに限定
        if pu.netloc != base.netloc:
            continue

        # ありがちな除外
        path = (pu.path or "").lower()
        if any(x in path for x in ["/tag/", "/tags/", "/category/", "/categories/", "/login", "/signup", "/account"]):
            continue

        # URLが短すぎる/トップっぽいのは除外
        if len(path.strip("/")) < 3:
            continue

        candidates.append(url)

    # 重複排除（順序保持）
    seen = set()
    uniq = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    return uniq[:link_limit]

def fetch_html(session: requests.Session, url: str, timeout=(5, 20), max_retries=2) -> Optional[str]:
    """軽いリトライ付きHTML取得"""
    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200 and r.text:
                return r.text
            # 429/503だけ少し待って再試行
            if r.status_code in (429, 503) and attempt < max_retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None

def ai_extract_events_from_text(
    client: genai.Client,
    model_name: str,
    text: str,
    today: datetime.date,
) -> List[Dict]:
    """記事本文（テキスト）からイベント情報を抽出"""
    all_items: List[Dict] = []

    for chunk in split_text_into_chunks(text, chunk_size=8000, overlap=400):
        if not chunk or len(chunk) < 80:
            continue

        prompt = f"""
以下のWebページ本文から、イベント・ニュース情報をJSON配列で漏れなく抽出してください。
【現在日付: {today}】

[抽出ルール]
- 本文に含まれるイベント（展示、催事、キャンペーン、募集、発表会、セミナー等）や、日時・期間・場所が書かれている情報を可能な限り抽出。
- 省略厳禁（ただし「明らかにイベントではない定型フッタ」などは無理に拾わない）。
- date_info は本文の表記のままでも良いが、可能なら YYYY年MM月DD日 / YYYY/MM/DD / 期間表現（例: 2025年01月01日〜2025年02月01日）のように分かる形で入れる。
- 出力は必ず JSON のみ。

[JSON形式]
[
  {{
    "name": "タイトル",
    "place": "場所（不明なら空文字）",
    "date_info": "日付や期間（不明なら空文字）",
    "description": "概要（短めに）"
  }}
]

本文:
{chunk}
"""
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            extracted = safe_json_parse(res.text)
            if isinstance(extracted, list):
                for item in extracted:
                    if not item or not isinstance(item, dict):
                        continue
                    name = item.get("name") or ""
                    if not name.strip():
                        continue
                    item["name"] = str(name).strip()
                    item["place"] = str(item.get("place") or "").strip()
                    item["date_info"] = normalize_date(str(item.get("date_info") or "").strip())
                    item["description"] = str(item.get("description") or "").strip()
                    all_items.append(item)
        except Exception:
            continue

    return all_items

# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.header("1. 対象サイト")

    PRESET_URLS = {
        "PRTIMES (グルメ)": "https://prtimes.jp/gourmet/",
        "PRTIMES (エンタメ)": "https://prtimes.jp/entertainment/",
        "AtPress (グルメ)": "https://www.atpress.ne.jp/news/food",
        "AtPress (新着)": "https://www.atpress.ne.jp/news",
    }

    selected_presets = st.multiselect(
        "プリセットから選択",
        options=list(PRESET_URLS.keys()),
        default=["PRTIMES (グルメ)"],
    )

    st.markdown("### 🔗 カスタムURL")
    custom_urls_text = st.text_area("URL（1行に1つ）", height=110)

    st.divider()
    st.header("2. 探索設定")
    max_pages = st.slider("一覧の最大ページ数（ページ送り回数）", 1, 20, 5)
    link_limit_per_page = st.slider("1ページあたり収集する記事URL上限", 10, 200, 60, step=10)
    max_articles_total = st.slider("総記事数の上限（安全策）", 20, 1000, 250, step=10)
    sleep_sec = st.slider("アクセス間隔（秒）", 0.0, 2.0, 0.6, step=0.1)

    st.divider()
    st.header("3. Gemini設定")
    model_name = st.text_input("モデル名", value="gemini-2.0-flash")
    temperature = st.slider("temperature（通常0推奨）", 0.0, 1.0, 0.0, step=0.1)

    st.divider()
    st.header("4. 既存CSVによる重複除外")
    uploaded_file = st.file_uploader("過去CSV（重複除外用）", type="csv")

    existing_fingerprints: Set[Tuple[str, str]] = set()
    if uploaded_file is not None:
        try:
            existing_df = pd.read_csv(uploaded_file)
            name_col = next((c for c in existing_df.columns if "イベント名" in c or c.lower() in ["name", "title"]), None)
            place_col = next((c for c in existing_df.columns if "場所" in c or c.lower() in ["place", "location"]), None)

            if name_col:
                for _, row in existing_df.iterrows():
                    n = normalize_string(row.get(name_col, ""))
                    p = normalize_string(row.get(place_col, "")) if place_col else ""
                    if n:
                        existing_fingerprints.add((n, p))
                st.success(f"📚 {len(existing_fingerprints)}件の既存データをロード")
            else:
                st.warning("CSVにイベント名列が見つかりませんでした（重複除外なしで続行）。")
        except Exception as e:
            st.error(f"CSV読込エラー: {e}")

# ============================================================
# Session State
# ============================================================
if "extracted_data" not in st.session_state:
    st.session_state.extracted_data = None
if "last_update" not in st.session_state:
    st.session_state.last_update = None

# ============================================================
# Main logic
# ============================================================
if st.button("一括読み込み開始", type="primary"):
    # API key
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except Exception:
        api_key = os.environ.get("GOOGLE_API_KEY")

    if not api_key:
        st.error("⚠️ GOOGLE_API_KEY が設定されていません。st.secrets または環境変数に設定してください。")
        st.stop()

    # targets
    targets = []
    for label in selected_presets:
        targets.append({"url": PRESET_URLS[label], "label": label})

    if custom_urls_text:
        for u in custom_urls_text.splitlines():
            u = u.strip()
            if u.startswith("http"):
                domain = urllib.parse.urlparse(u).netloc
                targets.append({"url": u, "label": f"カスタム ({domain})"})

    unique_targets = {t["url"]: t for t in targets}
    targets = list(unique_targets.values())

    if not targets:
        st.error("URLを指定してください。")
        st.stop()

    today = datetime.date.today()

    client = genai.Client(api_key=api_key)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    })

    main_progress = st.progress(0.0)
    status_box = st.empty()

    # gathering article urls
    all_article_urls: List[Tuple[str, str]] = []  # (article_url, source_label)
    visited_listing: Set[str] = set()

    total_units = len(targets) * max_pages
    unit_done = 0

    for target in targets:
        base_url = target["url"]
        label = target["label"]
        current_url = base_url

        for page_num in range(1, max_pages + 1):
            unit_done += 1
            main_progress.progress(min(unit_done / max(total_units, 1), 1.0))

            if current_url in visited_listing:
                status_box.warning(f"🔁 既に訪問済みの一覧URLのため停止: {current_url}")
                break
            visited_listing.add(current_url)

            status_box.info(f"📄 一覧取得: {label} | {page_num}ページ目\n{current_url}")

            html = fetch_html(session, current_url)
            if not html:
                status_box.warning(f"アクセス不可: {current_url}")
                break

            soup = BeautifulSoup(html, "html.parser")
            next_url = find_next_page_url(soup, current_url)

            # 一覧から記事リンク収集
            links = extract_article_links_from_listing(
                soup, current_url, link_limit=link_limit_per_page
            )
            for u in links:
                all_article_urls.append((u, label))

            # 上限安全策
            if len(all_article_urls) >= max_articles_total:
                break

            if not next_url:
                break

            current_url = next_url
            time.sleep(sleep_sec)

        if len(all_article_urls) >= max_articles_total:
            break

    # article url de-dup
    dedup = []
    seen_url = set()
    for u, lab in all_article_urls:
        if u not in seen_url:
            seen_url.add(u)
            dedup.append((u, lab))
    all_article_urls = dedup[:max_articles_total]

    if not all_article_urls:
        main_progress.empty()
        status_box.error("一覧ページから記事URLを取得できませんでした。")
        st.session_state.extracted_data = None
        st.stop()

    # =========================================================
    # Extract events from articles
    # =========================================================
    status_box.info(f"🧠 記事ページ解析開始（総{len(all_article_urls)}件）")
    extracted_all: List[Dict] = []
    visited_article: Set[str] = set()

    skipped_duplicate_csv = 0
    skipped_duplicate_run = 0
    failed_articles = 0

    for i, (article_url, label) in enumerate(all_article_urls, start=1):
        main_progress.progress(min(i / max(len(all_article_urls), 1), 1.0))
        status_box.info(f"🧠 記事解析 {i}/{len(all_article_urls)}: {article_url}")

        if article_url in visited_article:
            continue
        visited_article.add(article_url)

        html = fetch_html(session, article_url)
        if not html:
            failed_articles += 1
            continue

        soup = BeautifulSoup(html, "html.parser")
        clean_soup(soup)
        text = soup.get_text("\n", strip=True)

        # AI抽出
        items = ai_extract_events_from_text(client, model_name, text, today)

        # 付帯情報＆重複除外
        for item in items:
            n = normalize_string(item.get("name", ""))
            p = normalize_string(item.get("place", ""))

            if not n:
                continue

            # CSV既知重複除外
            if (n, p) in existing_fingerprints:
                skipped_duplicate_csv += 1
                continue

            # 今回取得内の重複除外（ソース問わず）
            fp = (n, p, normalize_string(item.get("date_info", ""))[:20])
            # date_infoまで含めた軽い指紋（完全一致を避ける）
            # ただし name/place が同じなら基本同一イベントとして扱いたい場合は date_infoを外してもOK
            # ここでは「name+place」を最優先にする
            fp2 = (n, p)

            # すでに抽出済みか確認
            exists = False
            for d in extracted_all:
                if (normalize_string(d.get("name","")), normalize_string(d.get("place",""))) == fp2:
                    exists = True
                    break
            if exists:
                skipped_duplicate_run += 1
                continue

            item["source_label"] = label
            item["source_url"] = article_url
            extracted_all.append(item)

        time.sleep(sleep_sec)

    main_progress.empty()

    if not extracted_all:
        status_box.warning(
            f"抽出結果が0件でした。記事取得失敗: {failed_articles}件 / CSV除外: {skipped_duplicate_csv}件"
        )
        st.session_state.extracted_data = None
        st.stop()

    st.session_state.extracted_data = extracted_all
    st.session_state.last_update = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    status_box.success(
        f"🎉 完了！新規 {len(extracted_all)} 件 / CSV除外: {skipped_duplicate_csv}件 / 今回重複除外: {skipped_duplicate_run}件 / 記事失敗: {failed_articles}件"
    )

# ============================================================
# Result rendering
# ============================================================
if st.session_state.extracted_data:
    df = pd.DataFrame(st.session_state.extracted_data)

    st.markdown(f"**取得件数: {len(df)}**（更新: {st.session_state.last_update}）")

    # 表示用リネーム
    display_df = df.rename(columns={
        "date_info": "期間",
        "name": "イベント名",
        "place": "場所",
        "description": "概要",
        "source_label": "情報源",
        "source_url": "URL"
    })

    # 列の存在を保証しつつ並べる
    desired_cols = ["期間", "イベント名", "場所", "概要", "情報源", "URL"]
    cols = [c for c in desired_cols if c in display_df.columns]
    display_df = display_df[cols]

    # ソート（期間が空でも落ちないように）
    if "期間" in display_df.columns:
        try:
            display_df = display_df.sort_values("期間", na_position="last")
        except:
            pass

    st.dataframe(
        display_df,
        use_container_width=True,
        column_config={
            "URL": st.column_config.LinkColumn("元記事", display_text="🔗 Link"),
            "概要": st.column_config.TextColumn("概要", width="large")
        },
        hide_index=True
    )

    csv_bytes = display_df.to_csv(index=False).encode("utf-8_sig")
    st.download_button("📥 CSVダウンロード", csv_bytes, "events_full.csv", "text/csv")
