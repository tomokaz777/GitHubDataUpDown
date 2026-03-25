import base64
import mimetypes
from io import BytesIO
from urllib.parse import quote

import requests
import streamlit as st


# =========================
# 基本設定
# =========================
st.set_page_config(page_title="GitHub File Manager", layout="wide")
st.title("GitHub Private File Manager")

# secrets 読み込み
try:
    GITHUB_TOKEN = st.secrets["GITHUB_TOKEN"].strip()
    GITHUB_OWNER = st.secrets["GITHUB_OWNER"].strip()
    GITHUB_REPO = st.secrets["GITHUB_REPO"].strip()
    GITHUB_BRANCH = st.secrets.get("GITHUB_BRANCH", "main").strip()
except Exception:
    st.error("secrets.toml に GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO / GITHUB_BRANCH を設定してください。")
    st.stop()

API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}


# =========================
# GitHub API 共通
# =========================
def github_get(path: str = ""):
    """ファイル/フォルダ一覧取得"""
    url = f"{API_BASE}/{quote(path)}" if path else API_BASE
    params = {"ref": GITHUB_BRANCH}
    res = requests.get(url, headers=HEADERS, params=params, timeout=30)
    return res


def github_repo_check():
    """
    設定値（owner/repo/branch/token）の疎通確認
    """
    user_url = "https://api.github.com/user"
    user_res = requests.get(user_url, headers=HEADERS, timeout=30)
    if user_res.status_code != 200:
        return False, f"Token が無効/期限切れの可能性があります: {user_res.status_code}", user_res

    user_login = user_res.json().get("login", "(unknown)")
    repo_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    repo_res = requests.get(repo_url, headers=HEADERS, timeout=30)
    if repo_res.status_code != 200:
        return (
            False,
            f"Repository にアクセスできません: {repo_res.status_code} "
            f"(token user: {user_login}, target: {GITHUB_OWNER}/{GITHUB_REPO})",
            repo_res,
        )

    repo_data = repo_res.json()
    branch_url = f"{repo_url}/branches/{quote(GITHUB_BRANCH)}"
    branch_res = requests.get(branch_url, headers=HEADERS, timeout=30)
    if branch_res.status_code != 200:
        # 空リポジトリ（コミットなし）の場合、branch API は 404 になりうる
        if repo_data.get("size", 0) == 0:
            return True, f"接続OK (token user: {user_login}, 空リポジトリ)", branch_res
        return False, f"Branch が見つかりません: {GITHUB_BRANCH} ({branch_res.status_code})", branch_res

    return True, f"接続OK (token user: {user_login})", branch_res


def is_empty_repo_response(res: requests.Response) -> bool:
    if res.status_code != 404:
        return False
    try:
        message = (res.json().get("message") or "").lower()
        return "repository is empty" in message
    except Exception:
        return False


def github_download_file(path: str):
    """ファイル内容取得（raw bytes）"""
    url = f"{API_BASE}/{quote(path)}"
    params = {"ref": GITHUB_BRANCH}
    res = requests.get(url, headers=HEADERS, params=params, timeout=30)
    res.raise_for_status()
    data = res.json()

    if data.get("type") != "file":
        raise ValueError("指定パスはファイルではありません。")

    encoded = data.get("content", "").replace("\n", "")
    file_bytes = base64.b64decode(encoded)
    return file_bytes, data.get("sha")


def github_upload_file(path: str, file_bytes: bytes, message: str):
    """新規作成 or 上書き"""
    url = f"{API_BASE}/{quote(path)}"

    existing_sha = None
    check_res = requests.get(url, headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=30)
    if check_res.status_code == 200:
        existing_sha = check_res.json().get("sha")

    payload = {
        "message": message,
        "content": base64.b64encode(file_bytes).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    res = requests.put(url, headers=HEADERS, json=payload, timeout=30)
    return res


def github_delete_file(path: str, message: str):
    """削除"""
    url = f"{API_BASE}/{quote(path)}"

    get_res = requests.get(url, headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=30)
    if get_res.status_code != 200:
        return get_res

    sha = get_res.json().get("sha")
    payload = {
        "message": message,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }
    res = requests.delete(url, headers=HEADERS, json=payload, timeout=30)
    return res


def list_files_recursive(path: str = ""):
    """
    指定フォルダ以下のファイルを再帰取得
    """
    results = []
    res = github_get(path)
    if res.status_code != 200:
        return results, res

    data = res.json()

    # 単一ファイルの場合
    if isinstance(data, dict) and data.get("type") == "file":
        results.append({
            "name": data["name"],
            "path": data["path"],
            "size": data.get("size", 0),
            "download_url": data.get("download_url"),
        })
        return results, res

    # フォルダの場合
    if isinstance(data, list):
        for item in data:
            if item["type"] == "file":
                results.append({
                    "name": item["name"],
                    "path": item["path"],
                    "size": item.get("size", 0),
                    "download_url": item.get("download_url"),
                })
            elif item["type"] == "dir":
                sub_results, _ = list_files_recursive(item["path"])
                results.extend(sub_results)

    return results, res


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    s = float(size)
    for unit in units:
        if s < 1024 or unit == units[-1]:
            return f"{s:.1f} {unit}"
        s /= 1024


# =========================
# UI
# =========================
with st.sidebar:
    st.header("設定")
    target_folder = st.text_input("GitHub上の保存先フォルダ", value="")
    st.caption(f"Repository: {GITHUB_OWNER}/{GITHUB_REPO}")
    st.caption(f"Branch: {GITHUB_BRANCH}")

    with st.expander("GitHub接続チェック"):
        ok, msg, check_res = github_repo_check()
        if ok:
            st.success(msg)
        else:
            st.error(msg)
            try:
                st.code(check_res.text)
            except Exception:
                pass


tab1, tab2, tab3 = st.tabs(["ファイル一覧", "アップロード", "削除"])


# =========================
# タブ1: 一覧・ダウンロード
# =========================
with tab1:
    st.subheader("GitHub上のファイル一覧")

    if st.button("一覧を更新"):
        st.session_state["refresh_files"] = True

    normalized_folder = target_folder.strip("/")
    files, res = list_files_recursive(normalized_folder)

    if is_empty_repo_response(res):
        st.info("リポジトリは空です。先にアップロードタブから最初のファイルを追加してください。")
    elif res.status_code == 404 and normalized_folder:
        st.info(
            f"フォルダ `{normalized_folder}` はまだ存在しません。"
            " 先にアップロードすると自動で作成されます。"
        )
    elif res.status_code == 404 and not normalized_folder:
        st.error("Repository / Branch / Token のいずれかが不正、または権限不足です。")
        st.info("左メニューの「GitHub接続チェック」を確認してください。")
        try:
            st.code(res.text)
        except Exception:
            pass
    elif res.status_code != 200:
        st.error(f"ファイル一覧取得失敗: {res.status_code}")
        try:
            st.code(res.text)
        except Exception:
            pass
    else:
        if not files:
            st.info("ファイルがありません。")
        else:
            st.write(f"件数: {len(files)}")

            for i, file_info in enumerate(files):
                col1, col2, col3 = st.columns([5, 2, 2])

                with col1:
                    st.write(f"**{file_info['path']}**")
                    st.caption(f"サイズ: {human_size(file_info['size'])}")

                with col2:
                    try:
                        file_bytes, _ = github_download_file(file_info["path"])
                        mime_type = mimetypes.guess_type(file_info["name"])[0] or "application/octet-stream"

                        st.download_button(
                            label="ダウンロード",
                            data=BytesIO(file_bytes),
                            file_name=file_info["name"],
                            mime=mime_type,
                            key=f"download_{i}"
                        )
                    except Exception as e:
                        st.error(f"DL失敗: {e}")

                with col3:
                    st.code(file_info["name"])


# =========================
# タブ2: アップロード
# =========================
with tab2:
    st.subheader("ファイルアップロード")

    uploaded_file = st.file_uploader("アップロードするファイルを選択", accept_multiple_files=False)

    custom_name = st.text_input("GitHub上の保存ファイル名（空なら元の名前）", value="")

    if uploaded_file is not None:
        st.write(f"選択中: {uploaded_file.name}")
        if st.button("GitHubへアップロード"):
            try:
                file_name = custom_name.strip() if custom_name.strip() else uploaded_file.name
                normalized_folder = target_folder.strip("/")
                target_path = f"{normalized_folder}/{file_name}" if normalized_folder else file_name

                res = github_upload_file(
                    path=target_path,
                    file_bytes=uploaded_file.getvalue(),
                    message=f"Upload {target_path} via Streamlit"
                )

                if res.status_code in (200, 201):
                    st.success(f"アップロード成功: {target_path}")
                else:
                    st.error(f"アップロード失敗: {res.status_code}")
                    st.code(res.text)
            except Exception as e:
                st.error(f"アップロード中にエラー: {e}")


# =========================
# タブ3: 削除
# =========================
with tab3:
    st.subheader("ファイル削除")

    normalized_folder = target_folder.strip("/")
    files_for_delete, res_del = list_files_recursive(normalized_folder)

    if is_empty_repo_response(res_del):
        st.info("リポジトリは空です。削除対象ファイルはありません。")
    elif res_del.status_code == 404 and normalized_folder:
        st.info(f"フォルダ `{normalized_folder}` はまだ存在しないため、削除対象がありません。")
    elif res_del.status_code != 200:
        st.error(f"削除対象一覧取得失敗: {res_del.status_code}")
        try:
            st.code(res_del.text)
        except Exception:
            pass
    else:
        if not files_for_delete:
            st.info("削除対象ファイルがありません。")
        else:
            file_paths = [f["path"] for f in files_for_delete]
            selected_path = st.selectbox("削除するファイルを選択", file_paths)

            confirm = st.checkbox("本当に削除する")
            if st.button("削除実行", type="primary", disabled=not confirm):
                try:
                    res = github_delete_file(
                        path=selected_path,
                        message=f"Delete {selected_path} via Streamlit"
                    )
                    if res.status_code == 200:
                        st.success(f"削除成功: {selected_path}")
                    else:
                        st.error(f"削除失敗: {res.status_code}")
                        st.code(res.text)
                except Exception as e:
                    st.error(f"削除中にエラー: {e}")