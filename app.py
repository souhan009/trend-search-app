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

st.title("📖 イベント情報「直読」抽出アプリ")
st.markdown("指定したWebページの中身を直接AIが読み込み、正確なイベントリストを作成します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("読み込み対象")
    
    # プリセットURL（東京・渋谷周辺のイベント一覧）
    # ※Walkerplusなどの一覧ページを指定
    PRESET_URLS = {
        "Walkerplus (今日のイベント/東京)": "https://www.walkerplus.com/event_list/today/ar0300/",
        "Walkerplus (今週末のイベント/東京)": "https://www.walkerplus.com/event_list/weekend/ar0300/",
        "Walkerplus (来週のイベント/東京)": "https://www.walkerplus.com/event_list/next_week/ar0300/",
        "Let's Enjoy Tokyo (現在開催中のイベント/渋谷)": "https://www.enjoytokyo.jp/event/list/chi03/?date_type=current",
        "Fashion Press (最新ニュース)": "https://www.fashion-press.net/news/",
        "【自由入力】": "custom"
    }
    
    selected_preset = st.selectbox("対象サイトを選択", list(PRESET_URLS.keys()))
    
    target_url = ""
    if selected_preset == "【自由入力】":
        target_url = st.text_input("URLを貼り付けてください", placeholder="https://...")
    else:
        target_url = PRESET_URLS[selected_preset]
        st.caption(f"URL: {target_url}")

    st.info("💡 検索ではなく、このページの文章をそのままAIに読ませてリスト化します。")

# --- メインエリア ---

if st.button("読み込み開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    if not target_url:
        st.error("⚠️ URLを指定してください。")
        st.stop()

    # 進捗表示
    progress_bar = st.progress(0)
    status_text = st.empty()

    # --- STEP 1: Webページのテキスト取得 (Scraping) ---
    status_text.info(f"📥 ページの内容を取得中...: {target_url}")
    progress_bar.progress(20)

    try:
        # ブラウザのふりをする（ブロック回避）
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(target_url, headers=headers, timeout=10)
        response.encoding = response.apparent_encoding # 文字化け防止
        
        if response.status_code != 200:
            st.error(f"ページの取得に失敗しました (Status Code: {response.status_code})")
            st.stop()

        # HTMLからテキストのみ抽出
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 不要なタグ（スクリプトやスタイル）を削除
        for script in soup(["script", "style", "nav", "footer", "iframe"]):
            script.decompose()
            
        # 本文テキストを取得 (余計な空白削除)
        page_text = soup.get_text(separator="\n", strip=True)
        
        # テキストが長すぎる場合はカット（Geminiの入力制限対策・コスト削減）
        # Gemini 2.0はコンテキストウィンドウが広いですが、念のため先頭5万文字に制限
        page_text = page_text[:50000]

    except Exception as e:
        st.error(f"ページの読み込みエラー: {e}")
        st.stop()

    # --- STEP 2: AIによる解析 (Gemini) ---
    status_text.info("🤖 AIがページを解読してリスト化しています...")
    progress_bar.progress(50)

    client = genai.Client(api_key=api_key)
    today = datetime.date.today()

    prompt = f"""
    あなたは優秀なデータ抽出アシスタントです。
    以下の「Webページのテキストデータ」から、イベント情報を抽出し、JSON形式で整理してください。

    【Webページのテキスト】
    {page_text}

    【抽出ルール】
    1. テキスト内に書かれているイベント名、開催期間、場所、概要を抜き出してください。
    2. **テキストに書かれていない情報は絶対に創作しないでください。**
    3. URLについては、このページ自体のURL（{target_url}）を「ソース」として扱います。
    4. 場所の緯度経度（lat, lon）は、場所名からあなたが推測して埋めてください（地図表示用）。

    【出力形式（JSONのみ）】
    [
        {{
            "name": "イベント名",
            "place": "開催場所",
            "date_info": "期間(テキスト通りに)",
            "description": "概要(簡潔に)",
            "lat": 緯度(数値),
            "lon": 経度(数値)
        }}
    ]
    """

    try:
        # ★ここを変更: 確実に動く gemini-2.0-flash-exp を使用
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0 # 忠実に抽出させる
            )
        )
        
        # --- JSONデータの抽出 ---
        text_resp = response.text.replace("```json", "").replace("```", "").strip()
        data = []
        try:
            data = json.loads(text_resp)
        except:
            pass

        progress_bar.progress(100)
        time.sleep(0.5)
        progress_bar.empty()

        if not data:
            st.warning("ページからイベント情報を抽出できませんでした。リスト形式のページではない可能性があります。")
            st.stop()
        else:
            status_text.success(f"{len(data)}件のイベントを抽出しました！")

        # データフレーム変換
        df = pd.DataFrame(data)

        # --- 1. マップ表示 ---
        st.subheader("📍 イベントマップ")
        
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
                        "html": "<b>{name}</b><br/>{place}",
                        "style": {"backgroundColor": "steelblue", "color": "white"}
                    }
                ))
        
        # --- 2. リスト表示 ---
        st.markdown("---")
        st.subheader("📋 抽出されたイベントリスト")
        st.caption(f"データソース: {target_url}")
        
        # CSVダウンロード用
        # CSVにはソースURL列を追加
        df['source_url'] = target_url
        csv = df.to_csv(index=False).encode('utf-8_sig')
        st.download_button(
            label="📥 CSVをダウンロード",
            data=csv,
            file_name="events_extracted.csv",
            mime='text/csv'
        )

        for item in data:
            st.markdown(f"""
            - **期間**: {item.get('date_info')}
            - **イベント名**: {item.get('name')}
            - **場所**: {item.get('place')}
            - **概要**: {item.get('description')}
            - [🔗 情報元ページへ]({target_url})
            """)

    except Exception as e:
        st.error(f"エラーが発生しました: {e}")
