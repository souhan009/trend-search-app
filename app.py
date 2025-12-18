import streamlit as st
import datetime
import os
import json
import time
import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Set, Any

import pandas as pd
import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from google import genai
from google.genai import types

# ============================================================
# Streamlit config
# ============================================================
st.set_page_config(page_title="イベント情報「全件網羅」抽出アプリ（決定版）", page_icon="📖", layout="wide")
st.title("📖 イベント情報「全件網羅」抽出アプリ（決定版）")
st.markdown("""
**AI × スマートクローリング（決定版）** 一覧ページから記事URLを厳密に抽出 → 本文をAI解析 → 重複除外して一覧化。  
**新機能:** 1. **自動リトライ**: API制限(429)がかかっても自動で待機して再開します。  
2. **モデル診断**: サイドバー下部で、あなたの環境で使える正確なモデル名を確認できます。
""")

# ============================================================
# Site rules
# ============================================================
@dataclass(frozen=True)
class SiteRule:
    name: str
    match_netloc: str
    article_path_allow: re.Pattern
    listing_next_hint_tokens: Tuple[str, ...] = ("次へ", "次の", "もっと見る", "Next", "NEXT", "More", "MORE")
    deny_path_prefixes: Tuple[str, ...] = ("/ranking", "/tag", "/tags", "/category", "/categories", "/login", "/signup", "/account")
    content_selectors: Tuple[str, ...] = ("article", "main", "div.article", "div#main", "div.content")

SITE_RULES: List[SiteRule] = [
    SiteRule(
        name="PRTIMES",
        match_netloc="prtimes.jp",
        article_path_allow=re.compile(r"^/main/html/rd/p/"),
        deny_path_prefixes=(
            "/ranking", "/company", "/categories", "/category", "/tag", "/tags",
            "/gourmet", "/entertainment", "/fashion", "/beauty", "/sports", "/technology", "/topics",
        ),
        content_selectors=("article", "main", "div.main-contents", "div#main", "div.body", "div.content")
    ),
    SiteRule(
        name="AtPress",
        match_netloc="atpress.ne.jp",
        article_path_allow=re.compile(r"^/news/\d+"),
        deny_path_prefixes=("/ranking", "/tag", "/tags", "/category", "/categories", "/login", "/signup", "/account"),
        content_selectors=("article", "main", "div#main", "div.newsDetail", "div.content")
    ),
]

def get_site_rule(url: str) -> Optional[SiteRule]:
    netloc = urllib.parse.urlparse(url).netloc.lower()
    for rule in SITE_RULES:
        if rule.match_netloc in netloc:
            return rule
    return None

# ============================================================
# Utils
# ============================================================
def normalize_date(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    # ISO format check
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        if "/" in t:
            return f"{y}/{mo}/{d}"
        return f"{y}年{mo}月{d}日"

    def rep_ymd(m2):
        return f"{m2.group(1)}年{m2.group(2).zfill(2)}月{m2.group(3).zfill(2)}日"

    t = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", rep_ymd, t)
    t = re.sub(r"(\d{4})/(\d{1,2})/(\d{1,2})", lambda m2: f"{m2.group(1)}/{m2.group(2).zfill(2)}/{m2.group(3).zfill(2)}", t)
    return t.strip()

def normalize_string(text) -> str:
    if not isinstance(text, str):
        return ""
    t = text.replace(" ", "").replace("　", "")
    t = t.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return t.lower().strip()

def safe_json_parse(json_str: str) -> List[Dict]:
    if not json_str or not isinstance(json_str, str):
        return []
    s = json_str.replace("```json", "").replace("```", "").strip()

    l = s.find("[")
    r = s.rfind("]")
    if l != -1 and r != -1 and r > l:
        cand = s[l:r+1]
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, list) else []
        except Exception:
            pass

    l = s.find("{")
    r = s.rfind("}")
    if l != -1 and r != -1 and r > l:
        cand = s[l:r+1]
        try:
            obj = json.loads(cand)
            return [obj] if isinstance(obj, dict) else []
        except Exception:
            pass

    return []

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
    except Exception:
        return False

def fetch_html(session: requests.Session, url: str, timeout=(5, 20), max_retries=2) -> Optional[str]:
    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200 and r.text:
                return r.text
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

def clean_soup(soup: BeautifulSoup) -> None:
    for t in soup.find_all(["script", "style", "nav", "footer", "iframe", "header", "noscript", "svg"]):
        try:
            t.decompose()
        except Exception:
            pass
    exclude_tokens = ["sidebar", "ranking", "recommend", "widget", "ad", "bread", "breadcrumb", "banner"]
    for t in soup.find_all(True):
        if not isinstance(t, Tag):
            continue
        attrs = getattr(t, "attrs", None)
        if not isinstance(attrs, dict):
            continue
        cls_list = attrs.get("class") or []
        if not isinstance(cls_list, (list, tuple)):
            cls_list = [str(cls_list)]
        cls = " ".join(map(str, cls_list)).lower()
        if any(tok in cls for tok in exclude_tokens):
            try:
                t.decompose()
            except Exception:
                pass

def extract_main_text(soup: BeautifulSoup, rule: Optional[SiteRule]) -> str:
    if rule:
        for sel in rule.content_selectors:
            try:
                node = soup.select_one(sel)
                if node:
                    return node.get_text("\n", strip=True)
            except Exception:
                continue
    return soup.get_text("\n", strip=True)

def split_text_into_chunks(text: str, chunk_size=8000, overlap=400):
    if not text:
        return
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        yield text[start:end]
        start = max(end - overlap, end)

def find_next_page_url(soup: BeautifulSoup, current_url: str, rule: Optional[SiteRule]) -> Optional[str]:
    link_next = soup.find("link", rel="next")
    if link_next and link_next.get("href") and is_valid_href(link_next["href"]):
        joined = urllib.parse.urljoin(current_url, link_next["href"])
        if same_domain(joined, current_url):
            return joined
    a_next = soup.find("a", rel=lambda v: v and "next" in str(v).lower(), href=True)
    if a_next and is_valid_href(a_next["href"]):
        joined = urllib.parse.urljoin(current_url, a_next["href"])
        if same_domain(joined, current_url):
            return joined
    tokens = rule.listing_next_hint_tokens if rule else ("次へ", "次の", "もっと見る", "Next", "More")
    for a in soup.find_all("a", href=True):
        try:
            txt = a.get_text(strip=True)
        except Exception:
            continue
        if any(t in txt for t in tokens):
            href = a.get("href")
            if href and is_valid_href(href):
                joined = urllib.parse.urljoin(current_url, href)
                if same_domain(joined, current_url):
                    return joined
    return None

def is_article_url(url: str, rule: Optional[SiteRule]) -> bool:
    if not rule:
        return True
    pu = urllib.parse.urlparse(url)
    path = pu.path or ""
    low = path.lower()
    if any(low.startswith(p) for p in rule.deny_path_prefixes):
        return False
    return bool(rule.article_path_allow.search(path))

def extract_article_links_from_listing(
    soup: BeautifulSoup,
    current_url: str,
    rule: Optional[SiteRule],
    link_limit: int = 80
) -> List[str]:
    base = urllib.parse.urlparse(current_url)
    out: List[str] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not is_valid_href(href):
            continue
        url = urllib.parse.urljoin(current_url, href)
        pu = urllib.parse.urlparse(url)
        if pu.netloc != base.netloc:
            continue
        if not is_article_url(url, rule):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= link_limit:
            break
    return out

# ------------------------------------------------------------
# Release date & JSON-LD location extraction
# ------------------------------------------------------------
def extract_release_date(soup: BeautifulSoup) -> str:
    meta_selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "dc.date"}),
        ("meta", {"name": "DC.date"}),
        ("meta", {"itemprop": "datePublished"}),
    ]
    for tag_name, attrs in meta_selectors:
        m = soup.find(tag_name, attrs=attrs)
        if m and m.get("content"):
            return normalize_date(str(m["content"]))
    t = soup.find("time")
    if t:
        dt = t.get("datetime")
        if dt:
            return normalize_date(str(dt))
        txt = t.get_text(strip=True)
        if txt:
            return normalize_date(txt)
    return ""

def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]

def extract_location_from_jsonld(soup: BeautifulSoup) -> Dict[str, str]:
    out = {"address": "", "latitude": "", "longitude": ""}
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = sc.string or sc.get_text(strip=True)
            if not raw:
                continue
            obj = json.loads(raw)
        except Exception:
            continue
        nodes: List[Any] = []
        for node in _as_list(obj):
            if isinstance(node, dict) and "@graph" in node:
                nodes.extend(_as_list(node.get("@graph")))
            else:
                nodes.append(node)
        for n in nodes:
            if not isinstance(n, dict):
                continue
            loc = n.get("location") or n.get("Place") or n.get("place")
            for loc_node in _as_list(loc):
                if not isinstance(loc_node, dict):
                    continue
                addr = loc_node.get("address")
                if isinstance(addr, dict):
                    parts = [
                        addr.get("addressRegion"),
                        addr.get("addressLocality"),
                        addr.get("streetAddress"),
                        addr.get("postalCode"),
                        addr.get("addressCountry"),
                    ]
                    addr_text = "".join([p for p in parts if isinstance(p, str) and p.strip()])
                    if addr_text and not out["address"]:
                        out["address"] = addr_text
                elif isinstance(addr, str) and addr.strip() and not out["address"]:
                    out["address"] = addr.strip()
                geo = loc_node.get("geo")
                if isinstance(geo, dict):
                    lat = geo.get("latitude")
                    lon = geo.get("longitude")
                    if lat is not None and not out["latitude"]:
                        out["latitude"] = str(lat).strip()
                    if lon is not None and not out["longitude"]:
                        out["longitude"] = str(lon).strip()
            addr2 = n.get("address")
            if isinstance(addr2, dict) and not out["address"]:
                parts = [
                    addr2.get("addressRegion"),
                    addr2.get("addressLocality"),
                    addr2.get("streetAddress"),
                    addr2.get("postalCode"),
                ]
                addr_text = "".join([p for p in parts if isinstance(p, str) and p.strip()])
                if addr_text:
                    out["address"] = addr_text
            geo2 = n.get("geo")
            if isinstance(geo2, dict):
                lat = geo2.get("latitude")
                lon = geo2.get("longitude")
                if lat is not None and not out["latitude"]:
                    out["latitude"] = str(lat).strip()
                if lon is not None and not out["longitude"]:
                    out["longitude"] = str(lon).strip()
            if out["address"] or out["latitude"] or out["longitude"]:
                return out
    return out

# ------------------------------------------------------------
# Gemini extraction (RETRY LOGIC ADDED)
# ------------------------------------------------------------
def ai_extract_events_from_text(
    client: genai.Client,
    model_name: str,
    temperature: float,
    text: str,
    today: datetime.date,
    debug_mode: bool,
    gemini_error_counter: Dict[str, int],
    min_chunk_len: int = 120,
) -> List[Dict]:
    all_items: List[Dict] = []
    
    # チャンクごとに処理
    for chunk in split_text_into_chunks(text, chunk_size=8000, overlap=400):
        if not chunk or len(chunk) < min_chunk_len:
            continue

        prompt = f"""
以下のWebページ本文から、イベント・ニュース情報をJSON配列で漏れなく抽出してください。
【現在日付: {today}】

[抽出ルール]
- 本文に含まれるイベント（展示、催事、キャンペーン、募集、発表会、セミナー等）や、日時・期間・場所が書かれている情報を可能な限り抽出。
- 省略厳禁。ただし「企業フッタ・問い合わせ先テンプレ」などの非イベント定型文は無理に拾わない。
- date_info は本文の表記のままでも良いが、可能なら YYYY年MM月DD日 / YYYY/MM/DD / 期間表現（例: 2025年01月01日〜2025年02月01日）。
- address / latitude / longitude は本文から推定できる範囲でよい（不明なら空文字）。
- 出力は必ずJSONのみ（説明文は禁止）。

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
{chunk}
"""
        # ========================================================
        # リトライロジック (Max 3回)
        # ========================================================
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                res = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=float(temperature)
                    )
                )

                if debug_mode:
                    st.write("🧪 Gemini raw (head 400):", (res.text or "")[:400])

                extracted = safe_json_parse(res.text)
                if isinstance(extracted, list):
                    for item in extracted:
                        if not item or not isinstance(item, dict):
                            continue
                        name = str(item.get("name") or "").strip()
                        if not name:
                            continue
                        out = {
                            "name": name,
                            "place": str(item.get("place") or "").strip(),
                            "address": str(item.get("address") or "").strip(),
                            "latitude": str(item.get("latitude") or "").strip(),
                            "longitude": str(item.get("longitude") or "").strip(),
                            "date_info": normalize_date(str(item.get("date_info") or "").strip()),
                            "description": str(item.get("description") or "").strip(),
                        }
                        all_items.append(out)
                
                # 成功したらループを抜ける
                break 

            except Exception as e:
                # 429エラー (Resource Exhausted) の場合のみ待機して再開
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    if attempt < max_retries:
                        wait_time = 10 * (attempt + 1) # 10s, 20s, 30s
                        if debug_mode:
                            st.warning(f"⚠️ 429 Detected. Retrying in {wait_time}s... ({attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                    else:
                        gemini_error_counter["count"] = gemini_error_counter.get("count", 0) + 1
                        if debug_mode:
                            st.error(f"❌ Retry Limit Reached: {e}")
                else:
                    # その他のエラーは即時記録して次へ
                    gemini_error_counter["count"] = gemini_error_counter.get("count", 0) + 1
                    if debug_mode:
                        st.error(f"❌ Gemini Error: {e}")
                    else:
                        # 404等の場合、ユーザーに気づかせる
                        st.warning(f"❌ Gemini Error: {e} (Check Model Name!)")
                    break

    return all_items

# ============================================================
# Sidebar UI
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

    st.markdown("### 🔗 カスタムURL（同一ドメインの一覧URL推奨）")
    custom_urls_text = st.text_area("URL（1行に1つ）", height=110)

    st.divider()
    st.header("2. 探索設定")
    max_pages = st.slider("一覧の最大ページ数（ページ送り回数）", 1, 30, 6)
    link_limit_per_page = st.slider("1ページあたり収集する記事URL上限", 10, 300, 80, step=10)
    max_articles_total = st.slider("総記事数の上限（安全策）", 20, 2000, 400, step=20)
    # ▼▼▼ 上限を30秒に変更し、ステップを1秒刻みにしました ▼▼▼
    sleep_sec = st.slider("アクセス間隔（秒）", 0.0, 30.0, 10.0, step=1.0)

    st.divider()
    st.header("3. Gemini設定")
    # デフォルトを安定版に戻しつつ、ユーザーが変更可能にする
    model_name = st.text_input("モデル名", value="gemini-1.5-flash")
    temperature = st.slider("temperature（0推奨）", 0.0, 1.0, 0.0, step=0.1)

    st.divider()
    st.header("🐞 デバッグ")
    debug_mode = st.checkbox("デバッグモード（詳細ログ表示）", value=False)
    debug_show_articles = st.slider("デバッグ表示する記事数", 1, 10, 3)

    st.divider()
    st.header("4. 既存CSVによる重複除外")
    uploaded_file = st.file_uploader("過去CSV（重複除外用）", type="csv")
    
    # --------------------------------------------------------
    # NEW: モデル名診断ツール (修正版：シンプルモード)
    # --------------------------------------------------------
    st.divider()
    st.header("🔍 モデル名診断")
    if st.button("利用可能なモデル一覧を表示"):
        api_key_check = None
        try:
            api_key_check = st.secrets["GOOGLE_API_KEY"]
        except:
            api_key_check = os.environ.get("GOOGLE_API_KEY")
            
        if not api_key_check:
            st.error("APIキーが見つかりません。")
        else:
            try:
                tmp_client = genai.Client(api_key=api_key_check)
                # リスト取得
                models_iter = tmp_client.models.list()
                
                valid_models = []
                for m in models_iter:
                    # 属性チェックを厳密にせず、nameがあれば取得する
                    # 新SDKではオブジェクト構造が異なるため getattr で安全策をとる
                    raw_name = getattr(m, "name", str(m))
                    
                    # "models/" を削除して見やすくする
                    clean_name = raw_name.replace("models/", "")
                    
                    # Gemini系かつVision/Content生成できそうなものだけ残す簡易フィルタ
                    if "gemini" in clean_name.lower():
                        valid_models.append(clean_name)
                
                if valid_models:
                    st.success("✅ 取得成功！以下の名前を試してください")
                    st.code("\n".join(sorted(valid_models)), language="text")
                else:
                    st.warning("モデルが見つかりませんでした。")
            except Exception as e:
                st.error(f"一覧取得エラー: {e}")

# ============================================================
# Load existing fingerprints
# ============================================================
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
            st.sidebar.success(f"📚 {len(existing_fingerprints)}件の既存データをロード")
        else:
            st.sidebar.warning("CSVにイベント名列が見つかりませんでした（重複除外なしで続行）。")
    except Exception as e:
        st.sidebar.error(f"CSV読込エラー: {e}")

# ============================================================
# Session state
# ============================================================
if "extracted_data" not in st.session_state:
    st.session_state.extracted_data = None
if "last_update" not in st.session_state:
    st.session_state.last_update = None

# ============================================================
# Main
# ============================================================
if st.button("一括読み込み開始", type="primary"):
    api_key = None
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except Exception:
        api_key = os.environ.get("GOOGLE_API_KEY")

    if not api_key:
        st.error("⚠️ GOOGLE_API_KEY が設定されていません（st.secrets または環境変数）。")
        st.stop()

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

    status = st.empty()
    progress = st.progress(0.0)

    # --------------------------------------------------------
    # 1) Collect article URLs from listings
    # --------------------------------------------------------
    collected: List[Tuple[str, str]] = []
    collected_seen: Set[str] = set()
    visited_listing: Set[str] = set()

    total_units = max(len(targets) * max_pages, 1)
    done_units = 0

    for target in targets:
        base_url = target["url"]
        label = target["label"]
        current_url = base_url
        rule = get_site_rule(current_url)

        for page_num in range(1, max_pages + 1):
            done_units += 1
            progress.progress(min(done_units / total_units, 1.0))

            if current_url in visited_listing:
                status.warning(f"🔁 一覧URL再訪のため停止: {current_url}")
                break
            visited_listing.add(current_url)

            status.info(f"📄 一覧取得: {label} | {page_num}/{max_pages}\n{current_url}")

            html = fetch_html(session, current_url)
            if not html:
                status.warning(f"アクセス不可: {current_url}")
                break

            soup = BeautifulSoup(html, "html.parser")
            next_url = find_next_page_url(soup, current_url, rule)
            links = extract_article_links_from_listing(soup, current_url, rule, link_limit=link_limit_per_page)

            add_count = 0
            for u in links:
                if u not in collected_seen:
                    collected_seen.add(u)
                    collected.append((u, label))
                    add_count += 1

            status.info(f"🔗 記事URL収集: +{add_count}件（累計 {len(collected)}件）")

            if len(collected) >= max_articles_total:
                break
            if not next_url:
                break

            current_url = next_url
            time.sleep(sleep_sec)

        if len(collected) >= max_articles_total:
            break

    collected = collected[:max_articles_total]

    if not collected:
        progress.empty()
        status.error("一覧ページから記事URLを取得できませんでした。")
        st.session_state.extracted_data = None
        st.stop()

    # --------------------------------------------------------
    # 2) Extract events from article pages
    # --------------------------------------------------------
    status.info(f"🧠 記事ページ解析開始（総 {len(collected)} 件）")
    extracted_all: List[Dict] = []

    run_fingerprints: Set[Tuple[str, str]] = set()

    skipped_duplicate_csv = 0
    skipped_duplicate_run = 0
    failed_articles = 0
    non_article_skipped = 0
    short_text_skipped = 0
    gemini_error_counter = {"count": 0}

    for i, (article_url, label) in enumerate(collected, start=1):
        progress.progress(min(i / max(len(collected), 1), 1.0))
        status.info(f"🧠 記事解析 {i}/{len(collected)}: {article_url}")

        rule = get_site_rule(article_url)

        if not is_article_url(article_url, rule):
            non_article_skipped += 1
            continue

        html = fetch_html(session, article_url)
        if not html:
            failed_articles += 1
            continue

        soup = BeautifulSoup(html, "html.parser")

        release_date = extract_release_date(soup)
        loc = extract_location_from_jsonld(soup)

        clean_soup(soup)
        text = extract_main_text(soup, rule)

        if not text or len(text) < 200:
            short_text_skipped += 1
            if debug_mode and i <= debug_show_articles:
                st.warning(f"本文が短すぎてスキップ: len={len(text) if text else 0} url={article_url}")
                st.write((text or "")[:500])
            continue

        if debug_mode and i <= debug_show_articles:
            st.write(f"🧪 [debug] text length={len(text)} release_date={release_date}")
            st.write("🧪 [debug] text head:", text[:500])
            if loc.get("address") or loc.get("latitude") or loc.get("longitude"):
                st.write("🧪 [debug] jsonld loc:", loc)

        items = ai_extract_events_from_text(
            client=client,
            model_name=model_name,
            temperature=temperature,
            text=text,
            today=today,
            debug_mode=debug_mode and (i <= debug_show_articles),
            gemini_error_counter=gemini_error_counter,
        )

        for item in items:
            item["release_date"] = release_date

            if loc.get("address") and not item.get("address"):
                item["address"] = loc["address"]
            if loc.get("latitude") and not item.get("latitude"):
                item["latitude"] = loc["latitude"]
            if loc.get("longitude") and not item.get("longitude"):
                item["longitude"] = loc["longitude"]

            n = normalize_string(item.get("name", ""))
            p = normalize_string(item.get("place", ""))

            if not n:
                continue

            fp = (n, p)

            if fp in existing_fingerprints:
                skipped_duplicate_csv += 1
                continue

            if fp in run_fingerprints:
                skipped_duplicate_run += 1
                continue

            run_fingerprints.add(fp)

            item["source_label"] = label
            item["source_url"] = article_url
            extracted_all.append(item)

        time.sleep(sleep_sec)

    progress.empty()

    if gemini_error_counter.get("count", 0) > 0:
        st.warning(
            f"⚠️ Gemini呼び出しでエラーが {gemini_error_counter['count']} 回発生しました。"
            f"（モデル名/クォータ/APIキー権限の可能性。サイドバーの診断ツールも活用してください）"
        )

    if not extracted_all:
        status.warning(
            f"抽出結果が0件でした。\n\n"
            f"- 記事失敗: {failed_articles}件\n"
            f"- 非記事URLスキップ: {non_article_skipped}件\n"
            f"- 本文短すぎスキップ: {short_text_skipped}件\n"
            f"- CSV除外: {skipped_duplicate_csv}件\n"
            f"- Geminiエラー: {gemini_error_counter.get('count', 0)}件"
        )
        st.session_state.extracted_data = None
        st.stop()

    st.session_state.extracted_data = extracted_all
    st.session_state.last_update = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    status.success(
        f"🎉 完了！新規 {len(extracted_all)} 件\n"
        f"- CSV除外: {skipped_duplicate_csv}件\n"
        f"- 今回重複除外: {skipped_duplicate_run}件\n"
        f"- 非記事URLスキップ: {non_article_skipped}件\n"
        f"- 本文短すぎスキップ: {short_text_skipped}件\n"
        f"- 記事失敗: {failed_articles}件\n"
        f"- Geminiエラー: {gemini_error_counter.get('count', 0)}件"
    )
# ============================================================
# Result rendering
# ============================================================
if st.session_state.extracted_data:
    df = pd.DataFrame(st.session_state.extracted_data)

    st.markdown(f"**取得件数: {len(df)}**（更新: {st.session_state.last_update}）")

    display_df = df.rename(columns={
        "release_date": "リリース日",
        "date_info": "期間",
        "name": "イベント名",
        "place": "場所",
        "address": "住所",
        "latitude": "緯度",
        "longitude": "経度",
        "description": "概要",
        "source_label": "情報源",
        "source_url": "URL"
    })

    desired_cols = ["リリース日", "期間", "イベント名", "場所", "住所", "緯度", "経度", "概要", "情報源", "URL"]
    cols = [c for c in desired_cols if c in display_df.columns]
    display_df = display_df[cols]

    # ▼▼▼ 追加機能：ゴミ掃除（「空文字」や「不明」を本当の空欄にする） ▼▼▼
    # AIが文字通り「空文字」と書いてしまったり、「不明」と入れたりするのを消去
    rubbish_words = ["空文字", "不明", "None", "null", "N/A", "未定"]
    for col in display_df.columns:
        # 文字列型の列に対して置換を実行
        display_df[col] = display_df[col].replace(rubbish_words, "")
    # ▲▲▲ 追加終了 ▲▲▲

    if "期間" in display_df.columns:
        try:
            display_df = display_df.sort_values("期間", na_position="last")
        except Exception:
            pass

    st.dataframe(
        display_df,
        use_container_width=True,
        column_config={
            "URL": st.column_config.LinkColumn("元記事", display_text="🔗 Link"),
            "概要": st.column_config.TextColumn("概要", width="large"),
        },
        hide_index=True
    )

    csv_bytes = display_df.to_csv(index=False).encode("utf-8_sig")
    st.download_button("📥 CSVダウンロード", csv_bytes, "events_full.csv", "text/csv")
