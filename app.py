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
st.markdown("指定した期間・地域の情報をAIが検索し、高機能マップとリストで表示します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="東京都渋谷区", help="具体的な地名を入力してください。")

    st.markdown("---")
    
    st.markdown("### 📅 期間指定")
    today = datetime.date.today()
    next_month = today + datetime.timedelta(days=30)
    
    start_date = st.date_input("開始日", today)
    end_date = st.date_input("終了日", next_month)

# --- メインエリア ---

if st.button("検索開始", type="primary"):
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except:
        st.error("⚠️ APIキーが設定されていません。")
        st.stop()

    if start_date > end_date:
        st.error("⚠️ 終了日は開始日より後の日付にしてください。")
    else:
        # 検索処理
        client = genai.Client(api_key=api_key)
        status_text = st.empty()
        status_text.info(f"🔍 {region}周辺の情報を厳密に収集中... (情報の精査に少し時間がかかります)")

        # プロンプト (嘘を防ぐための厳格な指示を追加)
        prompt = f"""
        あなたは「ファクトチェッカー」を兼ねたトレンドリサーチャーです。
        【{region}】における、【{start_date}】から【{end_date}】までの期間の以下の情報を、Google検索を使って調べてください。

        【調査対象】
        1. 有名チェーン店や人気飲食店の「新メニュー」「期間限定メニュー」の発売情報
        2. 注目の「新規店舗オープン」情報
        3. 期間限定のイベント情報

        【出力形式（JSONのみ）】
        Markdown装飾は不要。以下のキーを持つJSONリストを出力してください。
        [
            {{
                "type": "種別",
                "name": "店名またはイベント名",
                "place": "具体的な場所",
                "start_date": "YYYY-MM-DD",
                "end_date": "YYYY-MM-DD",
                "description": "概要",
                "url": "公式情報やニュース記事のURL(必須)",
                "lat": 緯度(数値),
                "lon": 経度(数値)
            }},
            ...
        ]

        【重要：ハルシネーション（嘘）対策】
        - **検索結果の記事に「{start_date.year}年」または「{end_date.year}年」の表記があるか必ず確認してください。**
        - 日付が不明確なもの、昨年の記事（2023年など）は**絶対に**含めないでください。
        - 該当する情報がない場合は、無理に件数を埋めず、確実なものだけを出力してください。
        - 各情報の `url` には、その情報の根拠となったWebページのURLを必ず入れてください。
        """

        try:
            # AIにリクエスト
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    response_mime_type="application/json"
                )
            )

            status_text.empty()
            
            # --- JSONデータの抽出・修復ロジック ---
            text = response.text.replace("```json", "").replace("```", "").strip()
            data = []
            
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                # エラーリカバリー
                try:
                    if e.msg.startswith("Extra data"):
                        data = json.loads(text[:e.pos])
                    else:
                        match = re.search(r'\[.*\]', text, re.DOTALL)
                        if match:
                            candidate = match.group(0)
                            try:
                                data = json.loads(candidate)
                            except json.JSONDecodeError as e2:
                                if e2.msg.startswith("Extra data"):
                                    data = json.loads(candidate[:e2.pos])
                                else:
                                    raise e2
                        else:
                            raise e
                except Exception:
                    st.error("データの読み込みに失敗しました。")
                    st.stop()

            # --- 期間表示用の整形処理 ---
            for item in data:
                s_date = item.get('start_date')
                e_date = item.get('end_date')
                if s_date and e_date:
                    if s_date == e_date:
                        item['display_date'] = s_date
                    else:
                        item['display_date'] = f"{s_date} 〜 {e_date}"
                else:
                    item['display_date'] = s_date or "日付不明"

            # データフレーム変換
            df = pd.DataFrame(data)

            # --- 1. 高機能地図の表示 (Voyagerスタイル) ---
            st.subheader(f"📍 {region}周辺のイベントマップ")
            
            if not df.empty and 'lat' in df.columns and 'lon' in df.columns:
                map_df = df.dropna(subset=['lat', 'lon'])
                
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
                    gaiyou = f"【期間】{row.get('display_date')}\n{row.get('description')}"
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
                st.warning("地図データが取得できませんでした。検索結果が少なかった可能性があります。")

            # --- 2. 速報テキストリスト（検証リンク付き） ---
            st.markdown("---")
            st.subheader("📋 イベント情報一覧（要確認）")
            st.caption("※AIの検索結果には誤りが含まれる可能性があります。必ず「ソース」のリンクから真偽を確認してください。")
            
            for item in data:
                url_text = "なし"
                if item.get('url'):
                    # リンクをクリックしやすく目立たせる
                    url_text = f"[🔗 検索元ソースを確認する]({item.get('url')})"

                st.markdown(f"""
                - **期間**: {item.get('display_date')}
                - **種別**: {item.get('type')}
                - **店名/イベント名**: {item.get('name')}
                - **場所**: {item.get('place')}
                - **概要**: {item.get('description')}
                - **ソース**: {url_text}
                """)
            
            # Groundingソース（念のため残す）
            with st.expander("📚 AIが参照したWebページ一覧"):
                if response.candidates[0].grounding_metadata.grounding_chunks:
                    for chunk in response.candidates[0].grounding_metadata.grounding_chunks:
                        if chunk.web:
                            st.markdown(f"- [{chunk.web.title}]({chunk.web.uri})")

        except Exception as e:
            status_text.empty()
            st.error(f"予期せぬエラーが発生しました: {e}")
