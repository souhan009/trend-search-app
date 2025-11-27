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
import time

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="🗺️")

st.title("🗺️ トレンド・イベントMap検索")
st.markdown("信頼できる情報サイト（Walkerplus, Go Tokyo等）の記事を検索し、イベント情報を抽出します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="東京都渋谷区", help="具体的な地名を入力してください。")
    
    st.markdown("---")
    st.markdown("### 🌐 検索対象サイト")
    
    SITE_DOMAINS = {
        "Walkerplus": "walkerplus.com",
        "GO TOKYO": "gotokyo.org",
        "Let's Enjoy Tokyo": "enjoytokyo.jp",
        "Fashion Press": "fashion-press.net",
        "TimeOut Tokyo": "timeout.jp",
        "Jorudan": "jorudan.co.jp",
        "PR TIMES": "prtimes.jp"
    }
    
    selected_sites = st.multiselect(
        "情報を取得するサイト（複数可）",
        options=list(SITE_DOMAINS.keys()),
        default=["Walkerplus", "Let's Enjoy Tokyo", "Fashion Press"]
    )
    
    st.info("💡 選択したサイト内を検索し、個別のイベント記事を探します。")

# --- メインエリア ---

if st.button("検索開始", type="primary"):
    # 事前チェック
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    if not selected_sites:
        st.error("⚠️ 検索対象サイトを少なくとも1つ選択してください。")
        st.stop()

    # ★ここからメインの処理（tryブロック）を開始
    try:
        # 検索処理準備
        client = genai.Client(api_key=api_key)
        status_text = st.empty()
        status_text.info(f"🔍 {region}の情報を、指定サイト内から収集中... (目標: 10件以上)")

        # 選択されたドメインをリスト化
        target_domains = [SITE_DOMAINS[name] for name in selected_sites]
        site_query = " OR ".join([f"site:{d}" for d in target_domains])
        
        today = datetime.date.today()
        
        # プロンプト
        prompt = f"""
        あなたは「イベント情報の収集ロボット」です。
        以下の検索クエリを使い、Google検索結果に表示される**個別のイベント記事**から情報を抽出してください。

        【検索クエリ】
        「{region} イベント 開催中 {site_query}」
        「{region} 新規オープン {site_query}」
        「{region} 期間限定 {site_query}」

        【基準日】
        本日は {today} です。過去に終了したイベントは除外してください。

        【厳守ルール】
        1. **実在する記事のみ**: 検索結果に出てきた記事（Webページ）を1件のイベントとして扱ってください。
        2. **URL**: 検索結果の**記事URL**をそのまま使用してください。自分でURLを作ったり、トップページ (`walkerplus.com` のみ等) を入れたりしないでください。
        3. **件数**: 検索結果から可能な限り多く（最大20件）抽出してください。

        【出力形式（JSONのみ）】
        [
            {{
                "name": "イベント名",
                "place": "開催場所",
                "date_info": "期間(例: 開催中〜12/25)",
                "description": "概要(短くてOK)",
                "source_name": "サイト名",
                "url": "記事のURL",
                "lat": 緯度(数値・不明ならnull),
                "lon": 経度(数値・不明ならnull)
            }}
        ]
        """

        # 検索実行関数（リトライ機能付き）
        def execute_search(model_name):
            return client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )

        response = None
        
        # 1. まずは安定版の 1.5-flash-002 を試す
        try:
            response = execute_search("gemini-1.5-flash-002")
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "NOT_FOUND" in error_msg:
                status_text.warning("⚠️ モデル切り替え中...")
                try:
                    time.sleep(2)
                    response = execute_search("gemini-2.0-flash-exp")
                except Exception as e2:
                    st.error(f"エラー: {e2}")
                    st.stop()
            else:
                st.error(f"エラーが発生しました: {e}")
                st.stop()

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
            
            # 名前チェック
            if not name or name.lower() in ['unknown', 'イベント']:
                continue
            
            # URLチェック（許可したドメインが含まれているか）
            is_valid_source = False
            if url and url.startswith("http"):
                for domain in target_domains:
                    if domain in url:
                        is_valid_source = True
                        break
            
            if not is_valid_source:
                # 怪しいURLはGoogle検索へ置換
                search_query = f"{item['name']} {item['place']} イベント"
                item['url'] = f"https://www.google.com/search?q={urllib.parse.quote(search_query)}"
                item['source_name'] = "Google検索"
            
            cleaned_data.append(item)
            
        data = cleaned_data

        if not data:
            st.warning(f"⚠️ 指定されたサイトからは、条件に合う記事が見つかりませんでした。")
            st.info("サイトの選択を増やすか、エリア名を変更して（例：渋谷区→東京）試してみてください。")
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

    # メイン処理の終了（エラーハンドリング）
    except Exception as e:
        status_text.empty()
        st.error(f"予期せぬエラーが発生しました: {e}")
