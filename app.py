import streamlit as st
import datetime
from google import genai
from google.genai import types
import os

# ページの設定
st.set_page_config(page_title="トレンド・イベント検索", page_icon="🔍")

st.title("🔍 トレンド・イベント検索アプリ")
st.markdown("指定した期間・地域の「新メニュー」「新規オープン」「イベント」情報をAIが検索します。")

# --- サイドバー: 設定エリア ---
with st.sidebar:
    st.header("検索条件")
    
    # 地域の設定 (ここを追加！)
    st.markdown("### 📍 地域・場所")
    region = st.text_input("検索したい場所", value="全国", help="例: 東京都、大阪市、渋谷区、吉祥寺 など")

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
        st.error("⚠️ APIキーが設定されていません。管理者に連絡してください。")
        st.stop()

    if start_date > end_date:
        st.error("⚠️ 終了日は開始日より後の日付にしてください。")
    else:
        # 検索処理
        client = genai.Client(api_key=api_key)
        
        status_text = st.empty()
        status_text.info(f"🔍 {region}周辺の情報を収集中... (20〜30秒ほどかかります)")

        # プロンプト (地域情報を埋め込み)
        prompt = f"""
        あなたはトレンドリサーチャーです。
        【{region}】における、【{start_date}】から【{end_date}】までの期間の以下の情報を、Google検索を使って調べてください。

        【調査対象】
        1. 有名チェーン店や人気飲食店の「新メニュー」「期間限定メニュー」の発売情報
        2. 注目の「新規店舗オープン」情報（商業施設や話題の店）
        3. 期間限定のイベント情報

        【条件】
        - 検索地域は必ず【{region}】に関連する情報に絞ってください。
        - 情報源は信頼できるニュースサイトやプレスリリースなどを優先してください。
        - **厳選して5〜10件** 抽出してください。
        - 過去のイベントではなく、指定期間に含まれるものに限ります。
        - 出力はMarkdown形式で、読みやすい箇条書きにしてください。
        """

        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )

            # 結果表示
            status_text.empty()
            st.success(f"検索完了！ ({region})")
            st.markdown(response.text)

            # 参照元リンク
            with st.expander("📚 参考にしたWebページ"):
                if response.candidates[0].grounding_metadata.grounding_chunks:
                    for chunk in response.candidates[0].grounding_metadata.grounding_chunks:
                        if chunk.web:
                            st.markdown(f"- [{chunk.web.title}]({chunk.web.uri})")

        except Exception as e:
            status_text.empty()
            st.error(f"エラーが発生しました: {e}")
