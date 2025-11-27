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

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="📖")

st.title("📖 イベント情報「一括直読」抽出アプリ")
st.markdown("複数のWebページを順番にAIが読み込み、情報を統合してリスト化します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("読み込み対象 (複数選択可)")
    
    # プリセットURLリスト
    PRESET_URLS = {
        "Walkerplus (今日のイベント/東京)": "https://www.walkerplus.com/event_list/today/ar0300/",
        "Walkerplus (今週末のイベント/東京)": "https://www.walkerplus.com/event_list/weekend/ar0300/",
        "Walkerplus (来週のイベント/東京)": "https://www.walkerplus.com/event_list/next_week/ar0300/",
        "Let's Enjoy Tokyo (現在開催中/渋谷)": "https://www.enjoytokyo.jp/event/list/chi03/?date_type=current",
        "Let's Enjoy Tokyo (今週末/渋谷)": "https://www.enjoytokyo.jp/event/list/chi03/?date_type=weekend",
        "Fashion Press (最新ニュース)": "https://www.fashion-press.net/news/",
        "TimeOut Tokyo (東京のイベント)": "https://www.timeout.jp/tokyo/ja/things-to-do"
    }
    
    # マルチセレクトに変更
    selected_presets = st.multiselect(
        "プリセットから選択",
        options=list(PRESET_URLS.keys()),
        default=["Walkerplus (今日のイベント/東京)"]
    )
    
    st.markdown("---")
    st.markdown("### 🔗 カスタムURL")
    custom_urls_text = st.text_area(
        "その他のURL（改行区切りで複数入力可）",
        placeholder="https://...\nhttps://...",
        height=100
    )

    st.info("💡 選択したすべてのページを順番に解析し、結果を1つのリストにまとめます。")

# --- メインエリア ---

if st.button("一括読み込み開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    # URLリストの作成
    target_urls = []
    
    # プリセットから追加
    for label in selected_presets:
        target_urls.append(PRESET_URLS[label])
    
    # カスタム入力から追加
    if custom_urls_text:
        for url in custom_urls_text.split('\n'):
            url = url.strip()
            if url and url.startswith("http"):
                target_urls.append(url)
    
    # 重複除去
    target_urls = list(set(target_urls))

    if not target_urls:
        st.error("⚠️ URLが指定されていません。")
        st.stop()

    # 処理開始
    all_data = []
    client = genai.Client(api_key=api_key)
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    total_urls = len(target_urls)
    
    # --- URLごとのループ処理 ---
    for i, url in enumerate(target_urls):
        current_progress = (i / total_urls)
        progress_bar.progress(current_progress)
        status_text.info(f"⏳ ({i+1}/{total_urls}) ページを解析中... \n{url}")
        
        try:
            # 1. スクレイピング
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            # タイムアウトを少し長めに設定
            response = requests.get(url, headers=headers, timeout=15)
            response.encoding = response.apparent_encoding
            
            if response.status_code != 200:
                st.warning(f"⚠️ アクセス失敗 (Status: {response.status_code}): {url}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            
            # 不要タグ削除
            for script in soup(["script", "style", "nav", "footer", "iframe", "header"]):
                script.decompose()
                
            page_text = soup.get_text(separator="\n", strip=True)
            page_text = page_text[:40000] # 文字数制限

            # 2. AI解析
            prompt = f"""
            あなたはデータ抽出アシスタントです。
            以下のWebページのテキストから「イベント情報」を抽出し、JSON形式でリスト化してください。

            【ページURL】
            {url}

            【テキスト内容】
            {page_text}

            【抽出ルール】
            1. イベント名、期間、場所、概要を抽出してください。
            2. テキストに書かれていない情報は創作せず、不明なら空欄にしてください。
            3. 場所の緯度経度（lat, lon）は、場所名から推測して埋めてください。
            4. `source_url` にはこのページのURL({url})を入れてください。

            【出力形式（JSONのみ）】
            [
                {{
                    "name": "イベント名",
                    "place": "開催場所",
                    "date_info": "期間",
                    "description": "概要(簡潔に)",
                    "source_url": "{url}",
                    "lat": 緯度(数値),
                    "lon": 経度(数値)
                }}
            ]
            """

            # 安定動作のため gemini-2.0-flash-exp を使用
            ai_response = client.models.generate_content(
                model="gemini-2.0-flash-exp",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            
            # JSON変換
            text_resp = ai_response.text.replace("```json", "").replace("```", "").strip()
            extracted_list = json.loads(text_resp)
            
            # 結果を統合リストに追加
            if isinstance(extracted_list, list):
                all_data.extend(extracted_list)
            
            # サーバー負荷軽減のため少し待機
            time.sleep(1)

        except Exception as e:
            st.warning(f"⚠️ エラーが発生したためスキップしました: {url}\nエラー内容: {e}")
            continue

    # --- 完了処理 ---
    progress_bar.progress(100)
    time.sleep(0.5)
    progress_bar.empty()

    if not all_data:
        st.error("イベント情報を抽出できませんでした。URLを確認してください。")
        st.stop()
    else:
        status_text.success(f"🎉 完了！ 合計 {len(all_data)} 件のイベント情報を抽出しました。")

    # データフレーム変換
    df = pd.DataFrame(all_data)

    # --- 1. マップ表示 (統合版) ---
    st.subheader("📍 イベントマップ (全件)")
    
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
                tooltip={
                    "html": "<b>{name}</b><br/>{place}<br/><i>{date_info}</i>",
                    "style": {"backgroundColor": "steelblue", "color": "white"}
                }
            ))
    
    # --- 2. リスト表示 ---
    st.markdown("---")
    st.subheader("📋 抽出されたイベントリスト")
    
    # CSVダウンロード
    csv = df.to_csv(index=False).encode('utf-8_sig')
    st.download_button(
        label="📥 全データをCSVでダウンロード",
        data=csv,
        file_name="events_all_extracted.csv",
        mime='text/csv'
    )

    # リスト表示
    for item in all_data:
        st.markdown(f"""
        - **期間**: {item.get('date_info')}
        - **イベント名**: {item.get('name')}
        - **場所**: {item.get('place')}
        - **概要**: {item.get('description')}
        - [🔗 情報元ページへ]({item.get('source_url')})
        """)
