import streamlit as st
import datetime
from google import genai
from google.genai import types
import os
import json
import pandas as pd
import re

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="🗺️")

st.title("🗺️ トレンド・イベントMap検索")
st.markdown("指定した期間・地域の情報をAIが検索し、地図とテキストで表示します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="東京都渋谷区", help="地図を表示するため、なるべく具体的な地名（例：梅田、吉祥寺、横浜みなとみらい）がおすすめです。")

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
        status_text.info(f"🔍 {region}周辺の情報を収集中... 地図とリストを作成しています...")

        # プロンプト (取得項目に「type」と「place」を追加)
        prompt = f"""
        あなたはトレンドリサーチャーです。
        【{region}】における、【{start_date}】から【{end_date}】までの期間の以下の情報を、Google検索を使って調べてください。

        【調査対象】
        1. 有名チェーン店や人気飲食店の「新メニュー」「期間限定メニュー」の発売情報
        2. 注目の「新規店舗オープン」情報（商業施設や話題の店）
        3. 期間限定のイベント情報

        【出力形式（超重要）】
        結果は**必ず以下のJSON形式のリストのみ**を出力してください。
        Markdownの装飾（```json）や前置きは不要です。
        各アイテムには、以下の情報を必ず含めてください。

        [
            {{
                "type": "種別(新メニュー/オープン/イベント)",
                "name": "店名またはイベント名",
                "place": "具体的な場所・施設名",
                "date": "開催日または発売日(YYYY-MM-DD)",
                "description": "概要（特徴を簡潔に）",
                "url": "関連する公式URLなど（あれば）",
                "lat": 緯度(数値),
                "lon": 経度(数値)
            }},
            ...
        ]

        【条件】
        - 検索地域は【{region}】に関連するものに限定してください。
        - **厳選して5〜8件** 抽出してください。
        - 緯度経度が不明な場合は、その地域の代表的な座標を入れてください。
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
                # エラーリカバリー（前回と同じ頑丈なロジック）
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

            # データフレーム変換
            df = pd.DataFrame(data)

            # --- 1. 地図の表示 ---
            st.subheader(f"📍 {region}周辺のイベントマップ")
            if not df.empty and 'lat' in df.columns and 'lon' in df.columns:
                map_df = df.dropna(subset=['lat', 'lon'])
                st.map(map_df, size=20, color='#FF4B4B')
            else:
                st.warning("地図データが取得できませんでした。")

            # --- 2. 速報リスト（昨日の形式）を追加！ ---
            st.markdown("---")
            st.subheader("📋 速報テキストリスト")
            
            for item in data:
                # 昨日のような箇条書きスタイルで出力
                st.markdown(f"""
                - **種別**: {item.get('type', '情報')}
                - **店名/イベント名**: {item.get('name')}
                - **場所**: {item.get('place', region)}
                - **概要**: {item.get('description')}
                - **日付**: {item.get('date')}
                """)

            # --- 3. 詳細リスト（既存の折りたたみ） ---
            st.markdown("---")
            st.subheader("📝 詳細・リンク")
            for item in data:
                with st.expander(f"{item.get('date', '')} : {item.get('name', '名称不明')}"):
                    st.write(f"**種別**: {item.get('type', '')}")
                    st.write(f"**場所**: {item.get('place', '')}")
                    st.write(f"**概要**: {item.get('description', '')}")
                    if item.get('url'):
                        st.markdown(f"[🔗 公式情報・関連リンク]({item.get('url')})")
                        
            # 参照元リンク
            with st.expander("📚 参考にしたWebページ"):
                if response.candidates[0].grounding_metadata.grounding_chunks:
                    for chunk in response.candidates[0].grounding_metadata.grounding_chunks:
                        if chunk.web:
                            st.markdown(f"- [{chunk.web.title}]({chunk.web.uri})")

        except Exception as e:
            status_text.empty()
            st.error(f"予期せぬエラーが発生しました: {e}")
