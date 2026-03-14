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
        "ID": "ID",
        "site": "サイト",
        "genre": "ジャンル",
        "url": "URL",
        "datetime": "リリース日時",
        "h1": "H1",
        "h2": "H2",
        "crawled_at": "取得日時",
    }
    df_display = df.rename(columns=col_map)
    st.dataframe(
        df_display,
        use_container_width=True,
        height=500,
        column_config={
            "URL": st.column_config.LinkColumn("URL", display_text="開く")
        }
    )

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


