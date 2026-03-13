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
        df = pd.read_csv(filepath, encoding="utf-8-sig")
        return df
    except Exception as e:
        st.error(f"CSVの読み込みエラー: {e}")
        return pd.DataFrame()

def show_table(df: pd.DataFrame):
    if df.empty:
        st.info("データがありません。")
        return
    col_map = {
        "No": "No",
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
        data=df.to_csv(index=False, sep="\t").encode("utf-16"),
        file_name="all_results.csv",
        mime="text/csv",
    )
else:
    st.info("all_results.csv がまだありません。バッチを実行してください。")


