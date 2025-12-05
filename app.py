import streamlit as st
import datetime
from google import genai
from google.genai import types
import os
import json
import pandas as pd
import requests
from bs4 import BeautifulSoup
import time
import urllib.parse
import re

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索（多ページ対応）", page_icon="📖", layout="wide")

st.title("📖 イベント情報「全件網羅」抽出アプリ")
st.markdown("""
**AI × スマートクローリング**
Webページを読み込み、**「もっと見る」や「次へ」のリンクを自動で辿って**、奥にある記事まで抽出します。
""")

# --- ユーティリティ関数 ---

def normalize_date(text):
    """日付をゼロ埋めYYYY年MM月DD日形式に統一"""
    if not text: return text
    def replace_func(match):
        return f"{match.group(1)}年{match.group(2).zfill(2)}月{match.group(3).zfill(2)}日"
    text = re.sub(r'(\d{4})年(\d{1,2})月(\d{1,2})日', replace_func, text)
    text = re.sub(r'(\d{4})/(\d{1,2})/(\d{1,2})', lambda m: f"{m.group(1)}/{m.group(2).zfill(2)}/{m.group(3).zfill(2)}", text)
    return text

def normalize_string(text):
    """文字列比較用の正規化関数"""
    if not isinstance(text, str):
        return ""
    text = text.replace(" ", "").replace("　", "")
    text = text.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return text.lower()

def safe_json_parse(json_str):
    """不完全なJSON文字列から、有効なオブジェクトのみを救出する"""
    if not json_str: return []
    json_str = json_str.replace("```json", "").replace("```", "").strip()
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            last_brace_index = json_str.rfind("}")
            if last_brace_index == -1: return [] 
            repaired_json = json_str[:last_brace_index+1] + "]"
            return json.loads(repaired_json)
        except:
            return []

def split_text_into_chunks(text, chunk_size=8000, overlap=500):
    """テキスト分割ジェネレータ"""
    if not text: return
    start = 0
    text_len = len(text)
    while start < text_len:
        end = start + chunk_size
        yield text[start:end]
        start = end - overlap

def find_next_page_url(soup, current_url):
    """
    HTML内から「次へ」や「もっと見る」のURLを探し出す関数
    """
    next_url = None
    
    # パターン1: ユーザー指定の特定クラス（優先）
    target_btn = soup.select_one("a.js-list-article-more-button")
    if target_btn and target_btn.get('href'):
        next_url = target_btn['href']
        
    # パターン2: rel="next"
    if not next_url:
        link_next = soup.find("link", rel="next")
        if link_next and link_next.get('href'):
            next_url = link_next['href']

    # パターン3: 一般的なページネーションクラス
    if not next_url:
        # "次へ", "Next", "More" を含むaタグ、または page-link などのクラス
        candidates = soup.find_all("a", href=True)
        for a in candidates:
            text = a.get_text(strip=True)
            cls = " ".join(a.get("class", []))
            
            # テキストやクラス名で判定
            if "次へ" in text or "Next" in text or "more" in cls.lower() or "next" in cls.lower():
                # 明らかにトップに戻るようなリンクは除外
                if len(a['href']) > 2: 
                    next_url = a['href']
                    break
    
    if next_url:
        # 相対パスを絶対パスに変換
        return urllib.parse.urljoin(current_url, next_url)
    
    return None

# --- Session State ---
if 'extracted_data' not in st.session_state:
    st.session_state.extracted_data = None
if 'last_update' not in st.session_state:
    st.session_state.last_update = None

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("1. 読み込み対象")
    
    PRESET_URLS = {
        "PRTIMES (グルメ)": "https://prtimes.jp/gourmet/",
        "PRTIMES (エンタメ)": "https://prtimes.jp/entertainment/",
        "AtPress (グルメ)": "https://www.atpress.ne.jp/news/food",
        "AtPress (新着)": "https://www.atpress.ne.jp/news",
    }
    
    selected_presets = st.multiselect(
        "サイトを選択",
        options=list(PRESET_URLS.keys()),
        default=["PRTIMES (グルメ)"]
    )

    st.markdown("### 🔗 カスタムURL")
    custom_urls_text = st.text_area("URLを入力 (1行に1つ)", height=100)
    
    st.markdown("---")
    st.header("2. 探索深度")
    max_pages = st.slider("読み込む最大ページ数", 1, 10, 3, help="「もっと見る」を何回辿るか指定します。多いと時間がかかります。")
    
    st.markdown("---")
    st.markdown("### 3. 既存データ除外")
    uploaded_file = st.file_uploader("過去CSV (重複除外用)", type="csv")
    
    existing_fingerprints = set()
    if uploaded_file is not None:
        try:
            existing_df = pd.read_csv(uploaded_file)
            count = 0
            name_col = next((col for col in existing_df.columns if 'イベント名' in col or 'Name' in col), None)
            place_col = next((col for col in existing_df.columns if '場所' in col or 'Place' in col), None)

            if name_col:
                for _, row in existing_df.iterrows():
                    n = normalize_string(row[name_col])
                    p = normalize_string(row[place_col]) if place_col else ""
                    existing_fingerprints.add((n, p))
                    count += 1
                st.success(f"📚 {count}件の既存データをロード")
        except Exception as e:
            st.error(f"CSV読込エラー: {e}")

# --- メインエリア ---

if st.button("一括読み込み開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    # ターゲットリスト作成
    targets = []
    for label in selected_presets:
        targets.append({"url": PRESET_URLS[label], "label": label})
    
    if custom_urls_text:
        for url in custom_urls_text.split('\n'):
            url = url.strip()
            if url and url.startswith("http"):
                domain = urllib.parse.urlparse(url).netloc
                targets.append({"url": url, "label": f"カスタム ({domain})"})
    
    # 重複URL削除
    unique_targets = {t['url']: t for t in targets}
    targets = list(unique_targets.values())

    if not targets:
        st.error("URLを指定してください。")
        st.stop()

    all_data = []
    client = genai.Client(api_key=api_key)
    today = datetime.date.today()
    
    main_progress = st.progress(0)
    status_text = st.empty()
    skipped_count_duplicate_csv = 0
    
    # --- サイトごとのループ ---
    for idx, target in enumerate(targets):
        base_url = target['url']
        label = target['label']
        
        current_url = base_url
        
        # --- ページごとのループ (指定回数まで) ---
        for page_num in range(1, max_pages + 1):
            
            progress_percent = (idx / len(targets)) + ((page_num / max_pages) / len(targets))
            main_progress.progress(min(progress_percent, 1.0))
            status_text.info(f"🔎 {label} | {page_num}ページ目を解析中...\nURL: {current_url}")
            
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124"}
                response = requests.get(current_url, headers=headers, timeout=15)
                response.encoding = response.apparent_encoding
                
                if response.status_code != 200:
                    st.warning(f"アクセス不可: {current_url}")
                    break

                soup = BeautifulSoup(response.text, "html.parser")
                
                # 次のページのURLを探しておく
                next_page_url = find_next_page_url(soup, current_url)
                
                # --- クリーニング ---
                for tag in soup.find_all(["script", "style", "nav", "footer", "iframe", "header", "noscript", "svg"]):
                    tag.decompose()
                
                # メインコンテンツ以外を削除（ノイズ除去）
                exclude = ['sidebar', 'ranking', 'recommend', 'widget', 'ad', 'bread']
                for tag in soup.find_all(attrs={"class": True}):
                    if not tag: continue
                    c_str = str(tag.get("class")).lower()
                    if any(x in c_str for x in exclude):
                        tag.decompose()
                
                full_text = soup.get_text(separator="\n", strip=True)
                chunks = list(split_text_into_chunks(full_text))
                
                # --- AI抽出 (チャンクごと) ---
                for chunk in chunks:
                    if not chunk: continue
                    
                    prompt = f"""
                    以下のWebテキストから、イベント・ニュース情報をJSONリストで抽出せよ。
                    【現在: {today}】
                    
                    [出力ルール]
                    - テキストにある情報は全て抽出すること。省略厳禁。
                    - 古いイベントも抽出してよい。
                    
                    Text:
                    {chunk[:10000]}
                    
                    JSON Output Example:
                    [
                        {{
                            "name": "タイトル",
                            "place": "場所",
                            "date_info": "YYYY年MM月DD日",
                            "description": "概要"
                        }}
                    ]
                    """
                    
                    try:
                        ai_res = client.models.generate_content(
                            model="gemini-2.0-flash-exp",
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json", 
                                temperature=0.0
                            )
                        )
                        extracted = safe_json_parse(ai_res.text)
                        
                        if isinstance(extracted, list):
                            for item in extracted:
                                if not item.get('name'): continue
                                
                                # 重複チェック
                                n = normalize_string(item['name'])
                                p = normalize_string(item.get('place', ''))
                                
                                # CSVとの重複確認
                                if (n, p) in existing_fingerprints:
                                    skipped_count_duplicate_csv += 1
                                    continue
                                
                                item['source_label'] = label
                                item['source_url'] = current_url # ページURLを保存
                                item['date_info'] = normalize_date(item.get('date_info', ''))
                                all_data.append(item)
                                
                    except Exception as e:
                        print(f"AI Error: {e}")
                        time.sleep(1)
            
                # 次のページがなければ終了、あればURL更新してループ継続
                if not next_page_url:
                    break
                current_url = next_page_url
                time.sleep(1) # サーバー負荷軽減
                
            except Exception as e:
                st.warning(f"エラー発生: {e}")
                break

    main_progress.empty()

    # --- 結果集計 ---
    if not all_data:
        if skipped_count_duplicate_csv > 0:
            st.warning(f"取得データは全てCSV内の既知情報でした。（除外: {skipped_count_duplicate_csv}件）")
        else:
            st.error("情報が見つかりませんでした。")
        st.session_state.extracted_data = None
    else:
        # 重複排除 (ページまたぎ等)
        unique_data = []
        seen = set()
        for d in all_data:
            key = (normalize_string(d['name']), normalize_string(d.get('place','')))
            if key not in seen:
                seen.add(key)
                unique_data.append(d)
        
        st.session_state.extracted_data = unique_data
        st.session_state.last_update = datetime.datetime.now().strftime("%H:%M:%S")
        status_text.success(f"🎉 完了！ 新規 {len(unique_data)} 件 (CSV除外: {skipped_count_duplicate_csv}件)")

# --- 結果表示 ---
if st.session_state.extracted_data:
    df = pd.DataFrame(st.session_state.extracted_data)
    
    st.markdown(f"**取得件数: {len(df)}**")
    
    # 表示用加工
    display_df = df.rename(columns={
        'date_info': '期間', 'name': 'イベント名', 
        'place': '場所', 'description': '概要', 
        'source_label': '情報源', 'source_url': 'URL'
    })
    
    # 期間でソート
    try:
        display_df = display_df.sort_values('期間')
    except: pass

    st.dataframe(
        display_df,
        use_container_width=True,
        column_config={
            "URL": st.column_config.LinkColumn("元記事", display_text="🔗 Link"),
            "概要": st.column_config.TextColumn("概要", width="large")
        },
        hide_index=True
    )
    
    csv = display_df.to_csv(index=False).encode('utf-8_sig')
    st.download_button("📥 CSVダウンロード", csv, "events_full.csv", "text/csv")
