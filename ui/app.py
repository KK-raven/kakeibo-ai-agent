# ui/app.py
"""
Streamlit チャット UI

固定のデモユーザーIDで動作する開発・確認用のWebインターフェース。
本番のユーザー向けインターフェースはLINE Bot + LIFF。
"""

from datetime import datetime

import requests
import pandas as pd
import plotly.express as px
import streamlit as st

API_BASE_URL = "http://api:8000"

DEMO_USER_ID = 1

TRANSACTION_COLUMNS = {
    "date": "日付",
    "type": "種別",
    "amount": "金額",
    "category": "カテゴリ",
    "store_name": "店名",
}


def fetch_transactions(limit: int = 20) -> list[dict]:
    """取引履歴をAPIから取得する。

    Args:
        limit: 取得件数の上限。

    Returns:
        取引履歴のリスト。取得失敗時は空リストを返す。
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/transactions",
            params={"limit": limit, "user_id": DEMO_USER_ID},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()["transactions"]
    except requests.exceptions.RequestException:
        return []


def download_export(filename: str) -> bytes | None:
    """エクスポートファイルをAPIから取得する。

    Args:
        filename: ダウンロードするファイル名。

    Returns:
        ファイルの内容。取得失敗時はNoneを返す。
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/exports/{filename}",
            timeout=10,
        )
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException:
        return None


def fetch_category_summary(
    year_month: str,
    type: str = "expense",
    person: str = "自分",
) -> list[dict] | None:
    """カテゴリ別集計をAPIから取得する。

    Args:
        year_month: "YYYY-MM" 形式。
        type: "expense" or "income"。
        person: 誰の集計か。

    Returns:
        カテゴリ別集計のリスト。通信エラー時はNone。
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/category_summary",
            params={
                "year_month": year_month,
                "type": type,
                "person": person,
                "user_id": DEMO_USER_ID,
            },
            timeout=5,
        )
        response.raise_for_status()
        return response.json()["summary"]
    except requests.exceptions.RequestException:
        return None


def check_api_health() -> bool:
    """APIのヘルスチェックを行う。"""
    try:
        response = requests.get(f"{API_BASE_URL}/health", timeout=3)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def send_message(
    message: str,
    conversation_history: list,
) -> tuple[str, list]:
    """チャットメッセージをAPIに送信する。

    Args:
        message: ユーザーの入力テキスト。
        conversation_history: これまでの会話履歴。

    Returns:
        (応答テキスト, tool_resultsリスト)。
        通信失敗時はエラーメッセージと空リストを返す。
    """
    try:
        response = requests.post(
            f"{API_BASE_URL}/chat",
            json={
                "user_id": DEMO_USER_ID,
                "message": message,
                "conversation_history": conversation_history,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return data["response"], data.get("tool_results", [])
    except requests.exceptions.Timeout:
        return "エラー：APIの応答がタイムアウトしました", []
    except requests.exceptions.RequestException as e:
        return f"エラー：APIとの通信に失敗しました（{e}）", []


def main():
    """Streamlitアプリのエントリーポイント。"""
    st.title("家計簿AIエージェント")

    with st.sidebar:
        st.header("システム状態")
        if check_api_health():
            st.success("API: 接続中")
        else:
            st.error("API: 未接続")

        st.divider()
        st.header("取引履歴")

        limit = st.slider(
            "表示件数", min_value=5, max_value=50, value=20, step=5,
        )

        transactions = fetch_transactions(limit=limit)
        if transactions:
            df = pd.DataFrame(transactions)[
                list(TRANSACTION_COLUMNS.keys())
            ]
            df.columns = list(TRANSACTION_COLUMNS.values())
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("取引履歴がありません")

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "conversation_history" not in st.session_state:
        st.session_state.conversation_history = []

    with st.expander("カテゴリ別支出グラフ", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            selected_date = st.date_input(
                "対象月", value=datetime.now(),
            )
            year_month = selected_date.strftime("%Y-%m")
        with col2:
            type_label = st.selectbox("種別", ["支出", "収入"])
        with col3:
            person = st.selectbox("対象者", ["自分", "妻", "共通"])
        type_value = "expense" if type_label == "支出" else "income"

        summary = fetch_category_summary(
            year_month=year_month,
            type=type_value,
            person=person,
        )
        if summary is None:
            st.error(
                "データの取得に失敗しました。"
                "APIの接続を確認してください。"
            )
        elif len(summary) == 0:
            st.caption("該当するデータがありません")
        else:
            df_summary = pd.DataFrame(summary)
            df_summary.columns = ["カテゴリ", "合計金額", "件数"]
            fig = px.bar(
                df_summary,
                x="カテゴリ",
                y="合計金額",
                text="合計金額",
            )
            fig.update_layout(
                xaxis_tickangle=-45,
                margin=dict(b=80),
            )
            fig.update_traces(
                texttemplate="¥%{text:,}",
                textposition="outside",
            )
            st.plotly_chart(fig, use_container_width=True)

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if "last_export_filename" in st.session_state:
        filename = st.session_state.last_export_filename
        file_content = download_export(filename)
        if file_content:
            st.download_button(
                label=f"📥 {filename} をダウンロード",
                data=file_content,
                file_name=filename,
                mime=(
                    "text/csv"
                    if filename.endswith(".csv")
                    else "text/plain"
                ),
            )

    st.caption("Enter で送信　／　Shift + Enter で改行")
    if prompt := st.chat_input("メッセージを入力してください"):

        with st.chat_message("user"):
            st.markdown(prompt)
        st.session_state.messages.append(
            {"role": "user", "content": prompt},
        )

        with st.chat_message("assistant"):
            with st.spinner("考え中..."):
                response, tool_results = send_message(
                    prompt,
                    st.session_state.conversation_history,
                )
            st.markdown(response)

        st.session_state.messages.append(
            {"role": "assistant", "content": response},
        )

        st.session_state.conversation_history.append(
            {"role": "user", "content": prompt},
        )
        st.session_state.conversation_history.append(
            {"role": "assistant", "content": response},
        )

        executed_tools = {
            tr["tool"] for tr in tool_results if "tool" in tr
        }

        import re
        match = re.search(
            r"[\w\-]+_\d{8}_\d{6}\.(csv|txt)", response,
        )
        if match:
            st.session_state.last_export_filename = match.group()

        should_rerun = any(
            not tool.startswith(("get_", "check_"))
            for tool in executed_tools
        )
        if should_rerun or match:
            st.rerun()


if __name__ == "__main__":
    main()
    