import streamlit as st
import datetime
import os
import json
import time
import re
import urllib.parse
import csv
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
st.set_page_config(page_title="イベント情報抽出（自動保存版）", page_icon="💾", layout="wide")
st.title("💾 イベント情報抽出アプリ（自動保存版）")
st.markdown("""
**AI × スマートクローリング（途中保存対応）** 1件抽出するごとに、自動的に `progressive_results.csv` に保存します。  
途中でエラー停止しても、そこまでのデータは確保されます。
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
# Metadata Extraction
# ------------------------------------------------------------
def extract_release_date(soup: BeautifulSoup) -> str:
    meta_selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "date"}),
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
    if x is None: return []
    return x if isinstance(x, list) else [x]

def extract_location_from_jsonld(soup: BeautifulSoup) -> Dict[str, str]:
    out = {"address": "", "latitude": "", "longitude": ""}
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = sc.string or sc.get_text(strip=True)
            if not raw: continue
            obj = json.loads(raw)
        except: continue
        nodes: List[Any] = []
        for node in _as_list(obj):
            if isinstance(node, dict) and "@graph" in node:
                nodes.extend(_as_list(node.get("@graph")))
            else:
                nodes.append(node)
        for n in nodes:
            if not isinstance(n, dict): continue
            loc = n.get("location") or n.get("Place") or n.get("place")
            for loc_node in _as_list(loc):
                if not isinstance(loc_node, dict): continue
                addr = loc_node.get("address")
                if isinstance(addr, dict):
                    parts = [addr.get("addressRegion"), addr.get("addressLocality"), addr.get("streetAddress")]
                    addr_text = "".join([p for p in parts if isinstance(p, str) and p.strip()])
                    if addr_text and not out["address"]: out["address"] = addr_text
                elif isinstance(addr, str) and addr.strip() and not out["address"]:
                    out["address"] = addr.strip()
                geo = loc_node.get("geo")
                if isinstance(geo, dict):
                    lat, lon = geo.get("latitude"), geo.get("longitude")
                    if lat is not None and not out["latitude"]: out["latitude"] = str(lat).strip()
                    if lon is not None and not out["longitude"]: out["longitude"] = str(lon).strip()
            if out["address"] or out["latitude"]: return out
    return out

# ------------------------------------------------------------
# Gemini Extraction (Single Article)
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
    
    # ユーザーがUIで指定したモデル名がそのまま使われます
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
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                res = client.models.generate_content(
                    model=model_name, # ここでUIの入力値が使われます
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
                break 

            except Exception as e:
                err_str = str(e)
                # 429エラーハンドリング
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    if attempt < max_retries:
                        wait_time = 15 * (attempt + 1)
                        if debug_mode:
                            st.warning(f"⚠️ 429 Detected. Retrying in {wait_time}s... ({attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                    else:
                        gemini_error_counter["count"] = gemini_error_counter.get("count", 0) + 1
                        if debug_mode: st.error(f"❌ Retry Limit Reached: {e}")
                else:
                    gemini_error_counter["count"] = gemini_error_counter.get("count", 0) + 1
                    if debug_mode: st.error(f"❌ Gemini Error: {e}")
                    else: st.warning(f"❌ Gemini Error: {e}")
                    break

    return all_items

# ------------------------------------------------------------
# Append to CSV (Incremental Save)
# ------------------------------------------------------------
def append_to_csv(data: Dict, filename: str):
    fieldnames = [
        "release_date", "date_info", "name", "place", "address", 
        "latitude", "longitude", "description", "source_label", "source_url"
    ]
    file_exists = os.path.isfile(filename)
    try:
        with open(filename, mode='a', encoding='utf-8_sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)
    except Exception as e:
        print(f"CSV Write Error: {e}")

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
    selected_presets = st.multiselect("プリセット", list(PRESET_URLS.keys()), default=["PRTIMES (グルメ)"])
    st.markdown("### 🔗 カスタムURL")
    custom_urls_text = st.text_area("URL（1行に1つ）", height=110)

    st.divider()
    st.header("2. 探索設定")
    max_pages = st.slider("一覧の最大ページ数", 1, 30, 6)
    link_limit_per_page = st.slider("1ページあたり収集URL上限", 10, 300, 80)
    max_articles_total = st.slider("総記事数の上限", 20, 2000, 400, step=20)
    sleep_sec = st.slider("アクセス間隔（秒）", 0.0, 30.0, 5.0, step=1.0)
    
    st.divider()
    st.header("3. Gemini設定")
    # UI入力欄 (初期値は gemini-2.0-flash ですが、画面上で変更すればそれが使われます)
    model_name = st.text_input("モデル名", value="gemini-2.0-flash") 
    temperature = st.slider("temperature", 0.0, 1.0, 0.0)

    st.divider()
    st.header("🐞 デバッグ")
    debug_mode = st.checkbox("デバッグモード", value=False)
    debug_show_articles = st.slider("デバッグ表示する記事数", 1, 10, 3)

    st.divider()
    st.header("4. 重複除外")
    uploaded_file = st.file_uploader("過去CSV", type="csv")
    
    st.divider()
    if st.button("モデル名診断"):
        api_key_check = os.environ.get("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY")
        if not api_key_check: st.error("No API Key")
        else:
            try:
                cl = genai.Client(api_key=api_key_check)
                ms = [getattr(m, "name", str(m)).replace("models/", "") for m in cl.models.list() if "gemini" in str(m).lower()]
                st.code("\n".join(sorted(ms)))
            except Exception as e: st.error(e)

# ============================================================
# Main Logic
# ============================================================
existing_fingerprints = set()
if uploaded_file:
    try:
        edf = pd.read_csv(uploaded_file)
        for _, r in edf.iterrows():
            n = normalize_string(r.get("イベント名", "") or r.get("name", ""))
            if n: existing_fingerprints.add(n)
        st.sidebar.success(f"📚 {len(existing_fingerprints)}件ロード済")
    except: pass

if "extracted_data" not in st.session_state: st.session_state.extracted_data = None

PROGRESSIVE_CSV = "progressive_results.csv"

if st.button("一括読み込み開始", type="primary"):
    # 既存の一時ファイルを削除（リセット）
    if os.path.exists(PROGRESSIVE_CSV):
        try:
            os.remove(PROGRESSIVE_CSV)
        except: pass

    today = datetime.date.today()
    api_key = os.environ.get("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY")
    if not api_key:
        st.error("API Key未設定")
        st.stop()
        
    targets = [{"url": PRESET_URLS[k], "label": k} for k in selected_presets]
    if custom_urls_text:
        for u in custom_urls_text.splitlines():
            if u.strip().startswith("http"): targets.append({"url": u.strip(), "label": "Custom"})
    
    if not targets: st.error("URLなし"); st.stop()
    
    client = genai.Client(api_key=api_key)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    
    status = st.empty()
    progress = st.progress(0.0)
    
    # 1. Collect URLs
    collected = []
    visited = set()
    seen_urls = set()
    
    for t in targets:
        curr = t["url"]
        rule = get_site_rule(curr)
        for p in range(max_pages):
            if curr in visited: break
            visited.add(curr)
            status.info(f"📄 一覧取得: {t['label']} ({p+1}/{max_pages})")
            
            html = fetch_html(session, curr)
            if not html: break
            soup = BeautifulSoup(html, "html.parser")
            
            links = extract_article_links_from_listing(soup, curr, rule, link_limit_per_page)
            for u in links:
                if u not in seen_urls:
                    seen_urls.add(u)
                    collected.append((u, t["label"]))
            
            if len(collected) >= max_articles_total: break
            curr = find_next_page_url(soup, curr, rule)
            if not curr: break
            time.sleep(1.0)
        if len(collected) >= max_articles_total: break
            
    collected = collected[:max_articles_total]
    if not collected: st.error("記事URLが見つかりませんでした"); st.stop()
    
    # 2. Extract Events
    extracted_all = []
    run_fingerprints = set()
    gemini_error_counter = {"count": 0}
    
    status.info(f"🧠 記事解析開始: {len(collected)}件 -> 結果は {PROGRESSIVE_CSV} に自動保存されます")
    
    for i, (url, label) in enumerate(collected):
        progress.progress((i+1) / len(collected))
        status.info(f"🧠 解析中 ({i+1}/{len(collected)}) モデル: {model_name} | URL: {url}")
        
        rule = get_site_rule(url)
        if not is_article_url(url, rule): continue
        html = fetch_html(session, url)
        if not html: continue
        
        soup = BeautifulSoup(html, "html.parser")
        r_date = extract_release_date(soup)
        loc = extract_location_from_jsonld(soup)
        clean_soup(soup)
        text = extract_main_text(soup, rule)
        
        if not text or len(text) < 200: continue
        
        if debug_mode and i < debug_show_articles:
            st.write(f"🧪 [debug] len={len(text)} date={r_date} loc={loc}")

        items = ai_extract_events_from_text(
            client, model_name, temperature, 
            text, today, debug_mode and (i < debug_show_articles), gemini_error_counter
        )
            
        for item in items:
            item["release_date"] = r_date
            if loc.get("address") and not item.get("address"): item["address"] = loc["address"]
            if loc.get("latitude") and not item.get("latitude"): item["latitude"] = loc["latitude"]
            if loc.get("longitude") and not item.get("longitude"): item["longitude"] = loc["longitude"]

            fp = normalize_string(item["name"])
            if fp in existing_fingerprints or fp in run_fingerprints: continue
            run_fingerprints.add(fp)
            
            item["source_label"] = label
            item["source_url"] = url
            
            extracted_all.append(item)
            append_to_csv(item, PROGRESSIVE_CSV)
        
        time.sleep(sleep_sec)
            
    st.session_state.extracted_data = extracted_all
    st.session_state.last_update = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status.success(f"完了! {len(extracted_all)}件抽出。ファイル: {PROGRESSIVE_CSV}")

if st.session_state.extracted_data:
    df = pd.DataFrame(st.session_state.extracted_data)
    
    rubbish = ["空文字", "不明", "None", "null", "N/A", "未定"]
    for c in df.columns:
        df[c] = df[c].replace(rubbish, "")

    display_df = df.rename(columns={
        "release_date": "リリース日", "date_info": "期間", "name": "イベント名",
        "place": "場所", "address": "住所", "latitude": "緯度", "longitude": "経度",
        "description": "概要", "source_label": "情報源", "source_url": "URL"
    })
    
    cols = ["リリース日", "期間", "イベント名", "場所", "住所", "緯度", "経度", "概要", "情報源", "URL"]
    display_df = display_df[[c for c in cols if c in display_df.columns]]
    
    st.dataframe(display_df, use_container_width=True, hide_index=True,
                 column_config={"URL": st.column_config.LinkColumn("Link")})
    
    st.download_button("結果CSVをDL", display_df.to_csv(index=False).encode("utf-8_sig"), "events_final.csv")
    
    if os.path.exists(PROGRESSIVE_CSV):
        with open(PROGRESSIVE_CSV, "rb") as f:
            st.download_button("途中経過CSVをDL", f, file_name="events_progressive.csv")
