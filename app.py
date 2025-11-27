import streamlit as st
import datetime
from google import genai
from google.genai import types
import os
import json
import pandas as pd
import re
import pydeck as pdk
import urllib.parse

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="🗺️")

st.title("🗺️ トレンド・イベントMap検索")
st.markdown("指定した「イベントまとめサイト」のリストから、情報を一括抽出します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="東京都渋谷区", help="具体的な地名を入力してください。")
    
    st.markdown("---")
    st.markdown("### 🔗 検索対象ページ指定")
    
    # 検索対象ページ
    SPECIFIC_PAGES = {
        "Let's Enjoy Tokyo (関東エリア一覧)": "https://www.enjoytokyo.jp/event/list/regn01/",
        "GO TOKYO (東京公式・イベントカレンダー)": "https://www.gotokyo.org/jp/event-calendar/",
        "Walkerplus (東京イベント一覧)": "https://www.walkerplus.com/event_list/ar0313/",
        "Fashion Press (ニュース一覧)": "https://www.fashion-press.net/news/",
        "TimeOut Tokyo (東京のイベント)": "https://www.timeout.jp/tokyo/ja/things-to-do/",
        "Jorudan (イベント情報)": "https://www.jorudan.co.jp/sp/event/"
    }
    
    selected_pages = st.multiselect(
        "検索範囲とするページ（複数可）",
        options=list(SPECIFIC_PAGES.keys()),
        default=["Let's Enjoy Tokyo (関東エリア一覧)", "GO TOKYO (東京公式・イベントカレンダー)"]
    )
    
    st.info("💡 指定されたURL階層の下にある情報のみを検索します。")

# --- メインエリア ---

if st.button("検索開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    if not selected_pages:
        st.error("⚠️ 検索対象ページを少なくとも1つ選択してください。")
        st.stop()

    # 検索処理
    client = genai.Client(api_key=api_key)
    status_text = st.empty()
    status_text.info(f"🔍 {region}の情報を、指定されたページ内から厳密に検索中...")

    target_urls = [SPECIFIC_PAGES[name] for name in selected_pages]
    site_query = " OR ".join([f"site:{url}" for url in target_urls])
    
    today = datetime.date.today()
    
    # プロンプト
    prompt = f"""
    あなたは「指定されたWebページからイベントリストを読み取るロボット」です。
    以下の検索クエリを使い、**指定されたURLパスの配下にあるページ**から、イベント情報を抽出してください。

    【検索クエリ】
    「{region} イベント 開催中 {site_query}」
    「{region} 新規オープン {site_query}」

    【基準日】
    本日は {today} です。過去のイベントは除外してください。

    【厳守ルール】
    1. **指定されたサイト以外からの情報は絶対に拾わないでください。**
    2. **捏造禁止**: 検索結果のスニペットに書かれているイベント名と日付のみを使用してください。
    3. **URL**: 記事の個別URLがあればそれを、なければ「検索結果のURL（一覧ページのURL）」を使用してください。架空のURLは禁止です。

    【出力形式（JSONのみ）】
    [
        {{
            "name": "イベント名",
            "place": "開催場所",
            "date_info": "期間(例: 開催中〜12/25)",
            "description": "概要(短くてOK)",
            "source_name": "サイト名",
            "url": "URL",
            "lat": 緯度(数値・不明ならnull),
            "lon": 経度(数値・不明ならnull)
        }}
    ]
    """

    try:
        # AIにリクエスト
        response = client.models.generate_content(
            # ★ここを変更: 2.0-flash -> 1.5-flash (制限に引っかかりにくいモデル)
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="application/json",
                temperature=0.0
            )
        )

        status_text.empty()
        
        # --- JSONデータの抽出 ---
        text = response.text.replace("```json", "").replace("```", "").strip()
        data = []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            try:
                if e.msg.startswith("Extra data"):
                    data = json.loads(text[:e.pos])
                else:
                    match = re.search(r'\[.*\]', text, re.DOTALL)
                    if match:
                        data = json.loads(match.group(0))
            except:
                pass
        
        # --- クリーニング & URLチェック ---
        cleaned_data = []
        for item in data:
            name = item.get('name', '')
            url = item.get('url', '')
            
            if not name or name.lower() in ['unknown', 'イベント']:
                continue
            
            # URLチェック
            is_valid_source = False
            if url:
                for target in target_urls:
                    domain = urllib.parse.urlparse(target).netloc
                    if domain in url:
                        is_valid_source = True
                        break
            
            if not is_valid_source:
                search_query = f"{item['name']} {item['place']} イベント"
                item['url'] = f"https://www.google.com/search?q={urllib.parse.quote(search_query)}"
                item['source_name'] = "Google検索"
            
            cleaned_data.append(item)
            
        data = cleaned_data

        if not data:
            st.warning(f"⚠️ 指定されたページ内からは、{region} の情報が見つかりませんでした。")
            st.info("別のサイトを選択するか、エリア名を変更して（例：渋谷区→東京）試してみてください。")
            st.stop()

        # データフレーム変換
        df = pd.DataFrame(data)

        # --- 1. 高機能地図 (Voyager) ---
        st.subheader(f"📍 {region}周辺のイベントマップ")
        st.caption(f"抽出件数: {len(data)}件")
        
        if not df.empty and 'lat' in df.columns and 'lon' in df.columns:
            map_df = df.dropna(subset=['lat', 'lon'])
            
            if not map_df.empty:
                view_state = pdk.ViewState(
                    latitude=map_df['lat'].mean(),
                    longitude=map_df['lon'].mean(),
                    zoom=13,
                    pitch=0,
                )

                layer = pdk.Layer(
                    "ScatterplotLayer",
                    map_df,
                    get_position='[lon, lat]',
                    get_color='[255, 75, 75, 160]',
                    get_radius=200,
                    pickable=True,
                )

                st.pydeck_chart(pdk.Deck(
                    map_style='https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
                    initial_view_state=view_state,
                    layers=[layer],
                    tooltip={
                        "html": "<b>{name}</b><br/>{place}<br/><i>{description}</i>",
                        "style": {"backgroundColor": "steelblue", "color": "white"}
                    }
                ))
                st.caption("※地図上の赤い丸にマウスを乗せると詳細が表示されます。")
                
                # CSV作成
                export_data = []
                for _, row in map_df.iterrows():
                    gaiyou = f"【期間】{row.get('date_info')}\n{row.get('description')}"
                    export_data.append({
                        "Name": row.get('name'),
                        "住所": row.get('place'),
                        "概要": gaiyou,
                        "公式サイト": row.get('url', '')
                    })
                
                export_df = pd.DataFrame(export_data)
                csv = export_df.to_csv(index=False).encode('utf-8_sig')

                st.download_button(
                    label="📥 Googleマイマップ用CSVをダウンロード",
                    data=csv,
                    file_name=f"event_map_{region}.csv",
                    mime='text/csv',
                    help="このファイルをGoogleマイマップにインポートし、「住所」列を目印の場所に指定してください。"
                )
            else:
                 st.info("※位置情報が特定できなかったため、地図には表示されませんが、以下のリストには表示されています。")
        else:
            st.warning("地図データが取得できませんでした。")

        # --- 2. 速報テキストリスト ---
        st.markdown("---")
        st.subheader("📋 イベント情報一覧")
        
        for item in data:
            url_text = "なし"
            source_label = item.get('source_name', '掲載サイト')
            
            link_label = f"{source_label} で見る"
            if source_label == "Google検索":
                link_label = "🔍 Googleで再検索"

            if item.get('url'):
                url_text = f"[🔗 {link_label}]({item.get('url')})"

            st.markdown(f"""
            - **期間**: {item.get('date_info')}
            - **イベント名**: {item.get('name')}
            - **場所**: {item.get('place')}
            - **概要**: {item.get('description')}
            - **ソース**: {url_text}
            """)

    except Exception as e:
        status_text.empty()
        st.error(f"予期せぬエラーが発生しました: {e}")
