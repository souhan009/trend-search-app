"""
app.py v2
バッチ結果閲覧UI
- auto_results.csv（全データ）とdiff_results_*.csv（差分）をテーブル表示
- CSVダウンロード
- 手動バッチ実行
"""

import streamlit as st
import pandas as pd
import os
import subprocess
from datetime import datetime

# ============================================================
# Streamlit config
# ============================================================
st.set_page_config(page_title="イベント情報ビューア", page_icon="🍽️", layout="wide")
st.title("🍽️ イベント情報ビューア")

RESULTS_DIR = "results"
AUTO_CSV = os.path.join(RESULTS_DIR, "all_results.csv")

# ============================================================
# ユーティリティ
# ============================================================
def load_csv(filepath: str) -> pd.DataFrame:
    if not os.path.isfile(filepath):
        return pd.DataFrame()
    try:
        df = pd.read_csv(filepath, encoding="utf-16")
        return df
    except Exception as e:
        st.error(f"CSVの読み込みエラー: {e}")
        return pd.DataFrame()

def show_table(df: pd.DataFrame):
    if df.empty:
        st.info("データがありません。")
        return
    col_map = {
        "release_date": "リリース日",
        "date_info": "イベント日程",
        "name": "イベント名",
        "place": "場所",
        "address": "住所",
        "latitude": "緯度",
        "longitude": "経度",
        "description": "概要",
        "source_url": "ソースURL",
    }
    df_display = df.rename(columns=col_map)
    st.dataframe(df_display, use_container_width=True, height=500)

# ============================================================
# サイドバー：操作パネル
# ============================================================
with st.sidebar:
    st.header("⚙️ 操作パネル")

    # 手動バッチ実行
    st.subheader("🔄 バッチ実行")
    if st.button("今すぐ最新データを取得", type="primary", use_container_width=True):
        api_key = st.secrets.get("GOOGLE_API_KEY", "")
        if not api_key:
            st.error("GOOGLE_API_KEY が設定されていません")
        else:
            with st.spinner("バッチ処理を実行中..."):
                env = os.environ.copy()
                env["GOOGLE_API_KEY"] = api_key
                result = subprocess.run(
                    [os.sys.executable, "batch.py"],
                    capture_output=True, text=True, env=env
                )
            if result.returncode == 0:
                st.success("完了しました！")
                st.text(result.stdout[-1000:])
            else:
                st.error("エラーが発生しました")
                st.text(result.stderr[-500:])

    st.divider()

# ============================================================
# メインエリア：テーブル表示
# ============================================================
st.subheader("📋 全データ一覧")
df = load_csv(AUTO_CSV)
if not df.empty:
    st.caption(f"総件数: {len(df)}件")
    show_table(df)
    st.download_button(
        label="📥 CSVダウンロード",
        data=df.to_csv(index=False, encoding="utf-16").encode("utf-16"),
        file_name="all_results.csv",
        mime="text/csv",
    )
else:
    st.info("all_results.csv がまだありません。バッチを実行してください。")


