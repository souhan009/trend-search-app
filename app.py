import streamlit as st
import datetime
from google import genai
from google.genai import types
import os
import json
import pandas as pd
import re
import pydeck as pdk

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="🗺️")

st.title("🗺️ トレンド・イベントMap検索")
st.markdown("Web全体から「現在開催中」および「今後開催予定」のイベント・新店情報を広範囲に収集します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="東京都渋谷区", help="具体的な地名を入力してください。")
    
    st.info("💡 期間指定を撤廃しました。現在進行系〜未来の情報を可能な限り多く表示します。")

# --- メインエリア ---

if st.button("検索開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    # 検索処理
    client = genai.Client(api_key=api_key)
    status_text = st.empty()
    status_text.info(f"🔍 {region}の情報をWeb全体から収集中... (目標: 10〜20件)")

    # 今日の日付
    today = datetime.date.today()
    
    # プロンプト (制限を緩めて大量に取らせる)
    prompt = f"""
    あなたは「Web検索ロボット」です。
    以下の検索クエリでGoogle検索を行い、**現在開催中**または**今後開催/オープン予定**のイベント情報を抽出してください。
    
    【検索クエリ】
    「{region} イベント 開催中」
    「{region} イベント 開催予定」
    「{region} 新規オープン 予定」
    「{region} 限定メニュー」

    【基準日】
    本日は {today} です。これより過去に終了したものは除外してください。

    【抽出ルール（重要）】
    1. **件数優先**: 可能な限り多く（最大20件程度）抽出してください。
    2. **URLの捏造禁止**: `kanko.walkerplus.com` のような存在しないURLを創作しないでください。検索結果にある**正しい記事URL**をそのまま使用してください。わからない場合は、無理にURLを貼らず `null` にしてください。
    3. **実在確認**: 「unknown」や「情報なし」といった無意味なデータは含めないでください。

    【出力形式（JSONのみ）】
    [
        {{
            "name": "イベント名",
            "place": "開催場所",
            "date_info": "開催期間(例: 開催中〜12/25)",
            "description": "概要",
            "source_name": "サイト名",
            "url": "記事のURL",
            "lat": 緯度(数値),
            "lon": 経度(数値)
        }}
    ]
    """

    try:
        # AIにリクエスト
        response = client.models.generate_content(
            model="gemini-2.0-flash",
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
                        candidate = match.group(0)
                        data = json.loads(candidate)
            except:
                pass
        
        # クリーニング（名前がない、URLが壊れている、などを弾く）
        cleaned_data = []
        for item in data:
            name = item.get('name', '')
            url = item.get('url', '')
            
            # 名前チェック
            if not name or name.lower() in ['unknown', 'イベント', 'なし']:
                continue
            
            # URLチェック (httpから始まっていない、または変なドメインを弾く簡易フィルタ)
            if not url or not url.startswith('http'):
                continue
            if 'kanko.walkerplus' in url: # 例の幻覚ドメインを物理削除
                continue
                
            cleaned_data.append(item)
            
        data = cleaned_data

        if not data:
            st.warning(f"⚠️ 情報が見つかりませんでした。エリアを変えて試してみてください。")
            st.stop()

        # データフレーム変換
        df = pd.DataFrame(data)

        # --- 1. 高機能地図 (Voyager) ---
        st.subheader(f"📍 {region}周辺のイベントマップ")
        st.caption(f"取得件数: {len(data)}件")
        
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
                 st.warning("位置情報が取得できませんでした（リストのみ表示します）")
        else:
            st.warning("地図データが取得できませんでした。")

        # --- 2. 速報テキストリスト ---
        st.markdown("---")
        st.subheader("📋 イベント情報一覧")
        
        for item in data:
            url_text = "なし"
            source_label = item.get('source_name', '掲載サイト')
            
            if item.get('url'):
                url_text = f"[🔗 {source_label} で詳細を見る]({item.get('url')})"

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
