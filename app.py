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
import glob
from datetime import datetime

# ============================================================
# Streamlit config
# ============================================================
st.set_page_config(page_title="イベント情報ビューア", page_icon="🍽️", layout="wide")
st.title("🍽️ イベント情報ビューア")

RESULTS_DIR = "results"
AUTO_CSV = os.path.join(RESULTS_DIR, "auto_results.csv")

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

def get_diff_files() -> list:
    """diff_results_*.csvを新しい順に返す"""
    files = glob.glob(os.path.join(RESULTS_DIR, "diff_results_*.csv"))
    files.sort(reverse=True)
    return files

def format_diff_label(filepath: str) -> str:
    """ファイル名からタイムスタンプを読みやすい形式に変換（UTC→JST変換）"""
    from datetime import timezone, timedelta
    basename = os.path.basename(filepath)
    try:
        ts = basename.replace("diff_results_", "").replace(".csv", "")
        dt_utc = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        dt_jst = dt_utc.astimezone(timezone(timedelta(hours=9)))
        return dt_jst.strftime("%Y-%m-%d %H:%M:%S JST")
    except Exception:
        return basename

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

    # 表示データ選択
    st.subheader("📂 表示データ選択")
    view_mode = st.radio(
        "表示するデータ",
        ["全データ（auto_results）", "差分データ（diff_results）"],
        index=0
    )

    if view_mode == "差分データ（diff_results）":
        diff_files = get_diff_files()
        if diff_files:
            labels = [format_diff_label(f) for f in diff_files]
            selected_label = st.selectbox("実行日時を選択", labels)
            selected_diff = diff_files[labels.index(selected_label)]
        else:
            st.info("差分ファイルがまだありません")
            selected_diff = None
    else:
        selected_diff = None

# ============================================================
# メインエリア：テーブル表示
# ============================================================
if view_mode == "全データ（auto_results）":
    st.subheader("📋 全データ一覧")
    df = load_csv(AUTO_CSV)
    if not df.empty:
        st.caption(f"総件数: {len(df)}件")
        show_table(df)
        st.download_button(
            label="📥 CSVダウンロード",
            data=df.to_csv(index=False, encoding="utf-8-sig"),
            file_name="auto_results.csv",
            mime="text/csv",
        )
    else:
        st.info("auto_results.csv がまだありません。バッチを実行してください。")

else:
    st.subheader("📋 差分データ一覧")
    diff_files = get_diff_files()
    if diff_files and selected_diff:
        df = load_csv(selected_diff)
        if not df.empty:
            st.caption(f"実行日時: {format_diff_label(selected_diff)}　件数: {len(df)}件")
            show_table(df)
            st.download_button(
                label="📥 CSVダウンロード",
                data=df.to_csv(index=False, encoding="utf-8-sig"),
                file_name=os.path.basename(selected_diff),
                mime="text/csv",
            )
        else:
            st.info("データがありません。")
    else:
        st.info("差分ファイルがまだありません。バッチを実行してください。")
