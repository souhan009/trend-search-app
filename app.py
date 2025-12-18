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
st.set_page_config(page_title="イベント情報抽出（バッチ爆速版）", page_icon="⚡", layout="wide")
st.title("⚡ イベント情報抽出アプリ（バッチ爆速版）")
st.markdown("""
**AI × バッチ処理（最速）** 複数の記事をまとめてAIに送ることで、待機時間を大幅に短縮しました。  
**設定:** サイドバーの「バッチサイズ」で、一度に処理する記事数を変更できます（推奨: 5〜10）。
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
# Batch Gemini Extraction (CORE UPDATE)
# ------------------------------------------------------------
def ai_extract_events_batch(
    client: genai.Client,
    model_name: str,
    temperature: float,
    batch_items: List[Dict], # [{text, url, release_date, loc, label}, ...]
    today: datetime.date,
    debug_mode: bool,
    gemini_error_counter: Dict[str, int],
) -> List[Dict]:
    
    # テキスト結合（プロンプト作成）
    combined_text = ""
    for i, item in enumerate(batch_items):
        combined_text += f"\n--- [Article ID: {i}] (Source: {item['url']}) ---\n"
        combined_text += item['text'][:8000] # 長すぎるとトークン死するのでカット
        combined_text += "\n"

    prompt = f"""
以下の {len(batch_items)} 件の記事（Article ID: 0 〜 {len(batch_items)-1}）から、イベント情報を抽出してください。
【現在日付: {today}】

[ルール]
- 各記事についてイベント（展示、催事、キャンペーン等）があれば抽出。なければスキップ。
- **重要:** 出力JSONには必ず `article_id` (数値) を含めること。これで元記事と紐付けます。
- date_info: YYYY年MM月DD日 / 期間。
- address/latitude/longitude: 本文から推測（不明なら空文字）。
- 出力はJSON配列のみ。

[JSON形式]
[
  {{
    "article_id": 0,
    "name": "タイトル",
    "place": "場所",
    "address": "住所",
    "latitude": "緯度",
    "longitude": "経度",
    "date_info": "日時",
    "description": "概要"
  }},
  ...
]

対象記事データ:
{combined_text}
"""

    extracted_results = []
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
                st.write(f"🧪 Batch Raw Response ({len(batch_items)} articles):", (res.text or "")[:200])

            parsed = safe_json_parse(res.text)
            if isinstance(parsed, list):
                extracted_results = parsed
                break # 成功
            else:
                break
                
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt < max_retries:
                    wait_time = 20 * (attempt + 1)
                    if debug_mode:
                        st.warning(f"⚠️ 429 Detected in Batch. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    gemini_error_counter["count"] = gemini_error_counter.get("count", 0) + 1
                    if debug_mode: st.error(f"❌ Batch Retry Limit: {e}")
            else:
                gemini_error_counter["count"] = gemini_error_counter.get("count", 0) + 1
                if debug_mode: st.error(f"❌ Batch Error: {e}")
                break

    # 結果をマージ
    final_items = []
    for item in extracted_results:
        if not isinstance(item, dict): continue
        
        # ID紐付け
        aid = item.get("article_id")
        if aid is None or not isinstance(aid, int) or aid < 0 or aid >= len(batch_items):
            continue
            
        original = batch_items[aid]
        
        # メタデータの優先順位: 構造化データ > AI推測
        final_lat = item.get("latitude", "")
        if original["loc"].get("latitude"): final_lat = original["loc"]["latitude"]
        
        final_lon = item.get("longitude", "")
        if original["loc"].get("longitude"): final_lon = original["loc"]["longitude"]
        
        final_addr = item.get("address", "")
        if original["loc"].get("address"): final_addr = original["loc"]["address"]

        out = {
            "name": str(item.get("name") or "").strip(),
            "place": str(item.get("place") or "").strip(),
            "address": str(final_addr).strip(),
            "latitude": str(final_lat).strip(),
            "longitude": str(final_lon).strip(),
            "date_info": normalize_date(str(item.get("date_info") or "").strip()),
            "description": str(item.get("description") or "").strip(),
            "release_date": original["release_date"],
            "source_label": original["label"],
            "source_url": original["url"]
        }
        if out["name"]:
            final_items.append(out)

    return final_items

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
    custom_urls_text = st.text_area("URL（1行に1つ）", height=80)

    st.divider()
    st.header("2. 探索設定")
    batch_size = st.slider("バッチサイズ（1回に送る記事数）", 1, 20, 5)
    max_pages = st.slider("一覧の最大ページ数", 1, 30, 6)
    link_limit_per_page = st.slider("1ページあたり収集URL上限", 10, 300, 80)
    max_articles_total = st.slider("総記事数の上限", 20, 2000, 400, step=20)
    sleep_sec = st.slider("AIリクエスト間の待機(秒)", 0.0, 60.0, 10.0, step=1.0)
    
    st.divider()
    st.header("3. Gemini設定")
    model_name = st.text_input("モデル名", value="gemini-2.0-flash-lite")
    temperature = st.slider("temperature", 0.0, 1.0, 0.0)

    st.divider()
    st.header("🐞 デバッグ")
    debug_mode = st.checkbox("デバッグモード", value=False)
    
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

if st.button("🚀 一括読み込み開始 (バッチ処理)", type="primary"):
    # ▼▼▼ 修正: ここに today の定義を追加しました ▼▼▼
    today = datetime.date.today()
    # ▲▲▲ 修正完了 ▲▲▲

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
    
    # 2. Batch Process
    extracted_all = []
    run_fingerprints = set()
    gemini_error_counter = {"count": 0}
    
    batch_buffer = [] 
    
    status.info(f"🧠 記事解析開始: {len(collected)}件 (バッチサイズ: {batch_size})")
    
    for i, (url, label) in enumerate(collected):
        progress.progress((i+1) / len(collected))
        
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
        
        batch_buffer.append({
            "text": text,
            "url": url,
            "label": label,
            "release_date": r_date,
            "loc": loc
        })
        
        if len(batch_buffer) >= batch_size or i == len(collected) - 1:
            status.info(f"🤖 AI解析中... ({len(batch_buffer)}件まとめて送信) Total: {len(extracted_all)}")
            
            batch_results = ai_extract_events_batch(
                client, model_name, temperature, 
                batch_buffer, today, debug_mode, gemini_error_counter
            )
            
            for item in batch_results:
                fp = normalize_string(item["name"])
                if fp in existing_fingerprints or fp in run_fingerprints: continue
                run_fingerprints.add(fp)
                extracted_all.append(item)
            
            batch_buffer = []
            time.sleep(sleep_sec)
            
    st.session_state.extracted_data = extracted_all
    st.session_state.last_update = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status.success(f"完了! {len(extracted_all)}件抽出")

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
    st.download_button("CSV DL", display_df.to_csv(index=False).encode("utf-8_sig"), "events_batch.csv")
