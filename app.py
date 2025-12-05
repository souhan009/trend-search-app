import streamlit as st
import datetime
from google import genai
from google.genai import types
import os
import json
import pandas as pd
import pydeck as pdk
import requests
from bs4 import BeautifulSoup
import time
import urllib.parse
import re

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="📖", layout="wide")

st.title("📖 イベント情報「完全救出」抽出アプリ")
st.markdown("Webページを読み込み、**手持ちのCSVにない新しい情報のみ**を抽出します。エラー回避強化版。")

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
    """
    不完全なJSON文字列から、有効なオブジェクトのみを救出してパースする関数。
    """
    json_str = json_str.replace("```json", "").replace("```", "").strip()
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            last_brace_index = json_str.rfind("}")
            if last_brace_index == -1:
                return [] 
            repaired_json = json_str[:last_brace_index+1] + "]"
            return json.loads(repaired_json)
        except:
            return []

def split_text_into_chunks(text, chunk_size=30000, overlap=1000):
    """テキストをオーバーラップ付きで分割するジェネレータ"""
    start = 0
    text_len = len(text)
    while start < text_len:
        end = start + chunk_size
        yield text[start:end]
        start = end - overlap

# --- Session State ---
if 'extracted_data' not in st.session_state:
    st.session_state.extracted_data = None
if 'last_update' not in st.session_state:
    st.session_state.last_update = None

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("1. 読み込み対象")
    
    PRESET_URLS = {
        # PR TIMES
        "PRTIMES (グルメ)": "https://prtimes.jp/gourmet/",
        "PRTIMES (ビジネス)": "https://prtimes.jp/business/",
        "PRTIMES (ライフスタイル)": "https://prtimes.jp/lifestyle/",
        "PRTIMES (ファッション)": "https://prtimes.jp/fashion/",
        "PRTIMES (ビューティ)": "https://prtimes.jp/beauty/",
        "PRTIMES (エンタメ)": "https://prtimes.jp/entertainment/",
        
        # AtPress
        "AtPress (新着ニュース)": "https://www.atpress.ne.jp/news",
        "AtPress (ランキング)": "https://www.atpress.ne.jp/service/release_ranking",
        "AtPress (エンタメ)": "https://www.atpress.ne.jp/news/entertainment",
        "AtPress (グルメ)": "https://www.atpress.ne.jp/news/food",
        "AtPress (旅行・観光)": "https://www.atpress.ne.jp/news/travel",
        "AtPress (ファッション)": "https://www.atpress.ne.jp/news/fashion"
    }
    
    selected_presets = st.multiselect(
        "サイトを選択",
        options=list(PRESET_URLS.keys()),
        default=["PRTIMES (グルメ)", "AtPress (グルメ)"]
    )

    st.markdown("### 🔗 カスタムURL")
    custom_urls_text = st.text_area("その他のURL (1行に1つ)", height=100)
    
    st.markdown("---")
    st.markdown("### 2. 既存データ除外 (オプション)")
    uploaded_file = st.file_uploader("過去CSVをアップロード (除外用)", type="csv")
    
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
                st.success(f"📚 既存データ {count}件 を読み込みました。")
            else:
                st.error("CSVに「イベント名」列が見つかりません。")
        except Exception as e:
            st.error(f"CSV読み込みエラー: {e}")

# --- メインエリア ---

if st.button("一括読み込み開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    targets = []
    for label in selected_presets:
        targets.append({"url": PRESET_URLS[label], "label": label})
    
    if custom_urls_text:
        for url in custom_urls_text.split('\n'):
            url = url.strip()
            if url and url.startswith("http"):
                domain = urllib.parse.urlparse(url).netloc
                targets.append({"url": url, "label": f"カスタム ({domain})"})
    
    unique_targets = {t['url']: t for t in targets}
    targets = list(unique_targets.values())

    if not targets:
        st.error("⚠️ URLが指定されていません。")
        st.stop()

    all_data = []
    client = genai.Client(api_key=api_key)
    today = datetime.date.today()
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total_urls = len(targets)
    skipped_count_duplicate_csv = 0
    
    # --- ループ処理 ---
    for i, target in enumerate(targets):
        url = target['url']
        label = target['label']
        
        status_text.info(f"⏳ ({i+1}/{total_urls}) 解析中...: {label}")
        progress_bar.progress(i / total_urls)
        
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            response = requests.get(url, headers=headers, timeout=15)
            response.encoding = response.apparent_encoding
            
            if response.status_code != 200:
                st.warning(f"⚠️ アクセス失敗: {url}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            
            # ノイズ除去
            for tag in soup(["script", "style", "nav", "footer", "iframe", "header", "noscript", "form", "svg"]):
                tag.decompose()
            
            exclude_keywords = ['sidebar', 'side-bar', 'ranking', 'recommend', 'widget', 'advertisement', 'pankuzu', 'breadcrumb']
            for tag in soup.find_all(attrs={"class": True}):
                classes = tag.get("class")
                # 【修正】tag.get("class")がNoneを返す可能性への対処（基本リストだが念の為）
                if not classes: continue
                if isinstance(classes, list): classes = " ".join(classes).lower()
                if any(k in classes for k in exclude_keywords): tag.decompose()
            
            full_text = soup.get_text(separator="\n", strip=True)
            
            # --- 分割処理 ---
            chunks = list(split_text_into_chunks(full_text, chunk_size=30000, overlap=1000))
            
            chunk_results = []
            chunk_progress = st.progress(0)
            
            for cid, chunk_text in enumerate(chunks):
                chunk_progress.progress((cid + 1) / len(chunks))
                
                prompt = f"""
                あなたはデータ抽出の専門家です。
                以下のテキスト（Webページの断片）から、含まれる「全ての」記事・イベント情報をJSONリストで抽出してください。
                **エラー防止のため、テキスト内で見つかった順に最大30件まで抽出してください。**

                【前提情報】
                ・本日の日付: {today.strftime('%Y年%m月%d日')}
                ・参照URL: {url}
                
                【テキスト内容】
                {chunk_text}

                【出力形式 (JSON List)】
                [
                    {{
                        "name": "イベント名または記事タイトル",
                        "place": "場所(なければ空欄)",
                        "date_info": "日付(YYYY年MM月DD日)",
                        "description": "概要(1行)",
                        "lat": 0.0,
                        "lon": 0.0
                    }}
                ]
                """

                try:
                    ai_response = client.models.generate_content(
                        model="gemini-2.0-flash-exp",
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json", 
                            temperature=0.0
                        )
                    )
                    
                    extracted = safe_json_parse(ai_response.text)
                    if isinstance(extracted, list):
                        chunk_results.extend(extracted)
                        
                except Exception as e:
                    print(f"Chunk error: {e}")
                    continue
                
                time.sleep(1)

            chunk_progress.empty()

            # --- 結果統合 ---
            seen_in_page = set()
            
            for item in chunk_results:
                # 【修正】itemがNoneでないこと、辞書型であることを確認
                if not item or not isinstance(item, dict):
                    continue

                n_key = normalize_string(item.get('name', ''))
                if not n_key or n_key in seen_in_page:
                    continue
                seen_in_page.add(n_key)

                p_key = normalize_string(item.get('place', ''))
                is_in_csv = False
                if (n_key, p_key) in existing_fingerprints:
                    is_in_csv = True
                elif p_key == "" and any(ef[0] == n_key for ef in existing_fingerprints):
                    is_in_csv = True
                
                if is_in_csv:
                    skipped_count_duplicate_csv += 1
                    continue

                item['source_label'] = label
                item['source_url'] = url
                if item.get('date_info'):
                    item['date_info'] = normalize_date(item['date_info'])
                all_data.append(item)

        except Exception as e:
            st.warning(f"読み込みエラー: {label} (エラー: {e})")
            continue

    progress_bar.progress(100)
    time.sleep(0.5)
    progress_bar.empty()

    if not all_data and skipped_count_duplicate_csv > 0:
        st.warning(f"データは取得できましたが、全てアップロードされたCSVに含まれる「既知の情報」でした。（除外数: {skipped_count_duplicate_csv}件）")
        st.session_state.extracted_data = None
    elif not all_data:
        st.error("情報が見つかりませんでした。")
        st.session_state.extracted_data = None
    else:
        unique_data = []
        seen_keys = set()
        for item in all_data:
            name_key = normalize_string(item.get('name', ''))
            place_key = normalize_string(item.get('place', ''))
            
            if (name_key, place_key) not in seen_keys:
                seen_keys.add((name_key, place_key))
                unique_data.append(item)
        
        st.session_state.extracted_data = unique_data
        st.session_state.last_update = datetime.datetime.now().strftime("%H:%M:%S")
        
        msg = f"🎉 読み込み完了！ 新規 {len(unique_data)} 件"
        if skipped_count_duplicate_csv > 0:
            msg += f" (CSV重複除外: {skipped_count_duplicate_csv} 件)"
        status_text.success(msg)

# --- 結果表示エリア ---

if st.session_state.extracted_data is not None:
    data = st.session_state.extracted_data
    df = pd.DataFrame(data)

    st.markdown(f"**最終更新: {st.session_state.last_update}** ({len(data)}件)")

    # 1. マップ表示
    st.subheader("📍 イベントマップ (新規のみ)")
    if not df.empty and 'lat' in df.columns and 'lon' in df.columns:
        map_df = df.dropna(subset=['lat', 'lon'])
        if not map_df.empty:
            view_state = pdk.ViewState(
                latitude=map_df['lat'].mean(),
                longitude=map_df['lon'].mean(),
                zoom=11,
                pitch=0,
            )
            layer = pdk.Layer(
                "ScatterplotLayer",
                map_df,
                get_position='[lon, lat]',
                get_color='[255, 75, 75, 160]',
                get_radius=300,
                pickable=True,
            )
            st.pydeck_chart(pdk.Deck(
                map_style='https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
                initial_view_state=view_state,
                layers=[layer],
                tooltip={"html": "<b>{name}</b><br/>{place}<br/><i>{date_info}</i>"}
            ))

    # 2. テーブル表示
    st.markdown("---")
    st.subheader("📋 新規イベント一覧")

    display_cols = ['date_info', 'name', 'place', 'description', 'source_label', 'source_url']
    available_cols = [c for c in display_cols if c in df.columns]
    display_df = df[available_cols].copy()
    
    rename_map = {
        'date_info': '期間', 'name': 'イベント名', 'place': '場所', 
        'description': '概要', 'source_label': '情報源', 'source_url': 'リンクURL'
    }
    display_df = display_df.rename(columns=rename_map)

    try:
        display_df = display_df.sort_values('期間')
    except:
        pass

    st.dataframe(
        display_df,
        use_container_width=True,
        column_config={
            "リンクURL": st.column_config.LinkColumn("元記事", display_text="🔗 リンクを開く"),
            "概要": st.column_config.TextColumn("概要", width="large")
        },
        hide_index=True
    )

    # 3. CSVダウンロード
    csv = display_df.to_csv(index=False).encode('utf-8_sig')
    st.download_button(
        label="📥 新規分CSVをダウンロード",
        data=csv,
        file_name="events_new_only.csv",
        mime='text/csv'
    )
