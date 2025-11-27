import streamlit as st
import datetime
from google import genai
from google.genai import types
import os
import json
import pandas as pd
import re # 追加：正規表現を使うためのライブラリ

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="🗺️")

st.title("🗺️ トレンド・イベントMap検索")
st.markdown("指定した期間・地域の情報をAIが検索し、地図とリストで表示します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    
    # 地域の設定
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="東京都渋谷区", help="地図を表示するため、なるべく具体的な地名（例：梅田、吉祥寺、横浜みなとみらい）がおすすめです。")

    st.markdown("---")
    
    # 期間の設定
    st.markdown("### 📅 期間指定")
    today = datetime.date.today()
    next_month = today + datetime.timedelta(days=30)
    
    start_date = st.date_input("開始日", today)
    end_date = st.date_input("終了日", next_month)

# --- メインエリア ---

if st.button("検索開始", type="primary"):
    # SecretsからAPIキーを読み込む
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
        status_text.info(f"🔍 {region}周辺の情報を収集中... 地図データも作成しています...")

        # プロンプト (JSON出力を強制し、緯度経度を要求)
        prompt = f"""
        あなたはトレンドリサーチャーです。
        【{region}】における、【{start_date}】から【{end_date}】までの期間の以下の情報を、Google検索を使って調べてください。

        【調査対象】
        1. 有名チェーン店や人気飲食店の「新メニュー」「期間限定メニュー」の発売情報
        2. 注目の「新規店舗オープン」情報（商業施設や話題の店）
        3. 期間限定のイベント情報

        【出力形式（超重要）】
        結果は**必ず以下のJSON形式のリストのみ**を出力してください。
        Markdownの装飾や、「結果はこちらです」などの前置きは一切不要です。
        各アイテムには、その場所のおおよその緯度(lat)と経度(lon)を必ず含めてください。

        [
            {{
                "name": "店名またはイベント名",
                "date": "開催日または発売日",
                "description": "概要（50文字程度）",
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
