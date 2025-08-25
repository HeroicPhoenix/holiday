# -*- coding: utf-8 -*-
"""
FastAPI 假日查询服务（自动从 GitHub 拉取 JSON，带每日定时刷新 + 前端页面）
- 强化：多源回退（GitHub API -> raw.githubusercontent -> jsDelivr），重试、指数退避、按年份枚举兜底
- 静态站点挂到 /ui（根路径 / 重定向到 /ui/）
"""


import os
import re
import json
import time
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Iterable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel

# ===================== 环境变量配置 =====================
FOLDER_PATH = os.environ.get("HOLIDAY_JSON_PATH", "data/holidays")
GH_OWNER = os.environ.get("HOLIDAY_GH_OWNER", "NateScarlet")
GH_REPO = os.environ.get("HOLIDAY_GH_REPO", "holiday-cn")
GH_PATH = os.environ.get("HOLIDAY_GH_PATH", "").strip("/")  # 仓库根目录留空
GH_BRANCH = os.environ.get("HOLIDAY_GH_BRANCH", "master")
GH_TOKEN = os.environ.get("GITHUB_TOKEN")  # 建议配置（提升配额；不能解决网络被墙）

SCHED_TZ = os.environ.get("TZ", "Asia/Shanghai")         # 定时任务时区
SCHED_HOUR = int(os.environ.get("REFRESH_HOUR", "3"))    # 每日 03:00
SCHED_MIN = int(os.environ.get("REFRESH_MIN", "0"))

SHA_INDEX_FILE = os.path.join(FOLDER_PATH, ".sha_index.json")

# 请求超时（秒）
LIST_TIMEOUT = 8
GET_TIMEOUT = 15

# ===================== 全局状态 =====================
df: Optional[pd.DataFrame] = None
_df_lock = threading.Lock()
_session = requests.Session()
scheduler = BackgroundScheduler(timezone=SCHED_TZ)

# ===================== 小工具 =====================
def _gh_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "holiday-cn-fastapi-fetcher",
    }
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    return headers

def _load_sha_index() -> Dict[str, str]:
    if os.path.exists(SHA_INDEX_FILE):
        try:
            with open(SHA_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_sha_index(idx: Dict[str, str]) -> None:
    os.makedirs(FOLDER_PATH, exist_ok=True)
    with open(SHA_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)

def _is_year_json(name: str) -> bool:
    if name.startswith("._"):
        return False
    return re.fullmatch(r"\d{4}\.json", name) is not None

def _year_range_for_fallback() -> List[int]:
    # 仓库最早是 2007；为保险把“当前年+1”也尝试一下（有些年份提前发布）
    this_year = datetime.now().year
    return list(range(2007, this_year + 2))

def _sleep_backoff(attempt: int) -> None:
    # 1, 2, 4 秒退避（最多 4s）
    time.sleep(min(2 ** (attempt - 1), 4))

# ===================== 网络获取：带重试与回退 =====================
def _http_get(url: str, headers: Dict[str, str], timeout: int) -> Optional[requests.Response]:
    # 最多重试 3 次，指数退避
    for attempt in range(1, 4):
        try:
            r = _session.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            # 404 直接放弃，不再重试
            if r.status_code == 404:
                return None
            # 其他状态码重试
            print(f"⚠️ GET {url} -> {r.status_code}（第{attempt}次）")
        except Exception as e:
            print(f"⚠️ GET 异常（第{attempt}次）: {e}")
        _sleep_backoff(attempt)
    return None

def _download_to(dst_path: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "wb") as f:
        f.write(content)

# 1) 通过 GitHub Contents API 列目录（首选）
def _gh_list_contents() -> Optional[List[Dict[str, Any]]]:
    try:
        base = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents"
        url = f"{base}/{GH_PATH}?ref={GH_BRANCH}" if GH_PATH else f"{base}?ref={GH_BRANCH}"
        resp = _http_get(url, headers=_gh_headers(), timeout=LIST_TIMEOUT)
        if not resp:
            return None
        data = resp.json()
        return data if isinstance(data, list) else [data]
    except Exception as e:
        print(f"⚠️ GitHub 列目录失败：{e}")
        return None

# 2) 通过 Contents API 的 download_url 拉取
def _try_download_via_download_url(item: Dict[str, Any], sha_index: Dict[str, str], force: bool) -> bool:
    name = item.get("name")
    sha = item.get("sha")
    download_url = item.get("download_url")
    if not name or not _is_year_json(name) or not download_url:
        return False

    local_file = os.path.join(FOLDER_PATH, name)
    need = force or (sha_index.get(name) != sha) or (not os.path.exists(local_file))
    if not need:
        return False

    resp = _http_get(download_url, headers=_gh_headers(), timeout=GET_TIMEOUT)
    if not resp:
        return False

    _download_to(local_file, resp.content)
    sha_index[name] = sha or ""
    print(f"✅ 通过 API 下载完成：{name}")
    return True

# 3) 不依赖 API 的直链拉取（raw & jsDelivr），用于兜底
def _try_download_via_direct_urls(year: int) -> bool:
    # 组装仓库内路径
    inner = f"{GH_PATH}/{year}.json" if GH_PATH else f"{year}.json"
    urls = [
        f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/{GH_BRANCH}/{inner}",
        f"https://cdn.jsdelivr.net/gh/{GH_OWNER}/{GH_REPO}@{GH_BRANCH}/{inner}",
    ]
    for u in urls:
        resp = _http_get(u, headers=_gh_headers(), timeout=GET_TIMEOUT)
        if resp:
            local_file = os.path.join(FOLDER_PATH, f"{year}.json")
            _download_to(local_file, resp.content)
            print(f"✅ 直链下载完成：{year}.json ← {u}")
            return True
    print(f"❌ 直链下载失败：{year}.json")
    return False

# ===================== 拉取主流程 =====================
def fetch_all_year_jsons(force: bool = False) -> bool:
    """
    拉取所有 YYYY.json 到本地目录，返回是否有变更（新增/更新）
    - 优先：GitHub Contents API（更精准、快）
    - 失败：按年份枚举 + 直链多镜像（raw / jsDelivr）
    """
    changed = False
    sha_index = _load_sha_index()

    items = _gh_list_contents()
    if items:
        # API 可用：逐个按 item 下载（增量依据 sha）
        for it in items:
            # 跳过非年份 JSON
            name = it.get("name", "")
            if not _is_year_json(name):
                continue
            ok = _try_download_via_download_url(it, sha_index, force=force)
            changed = changed or ok
        _save_sha_index(sha_index)
        return changed

    # API 列目录失败：按年份枚举 + 直链下载（不使用 sha 增量，只要本地没有就下）
    print("ℹ️ 列目录不可用，切换到按年份直链下载模式…")
    for y in _year_range_for_fallback():
        local_file = os.path.join(FOLDER_PATH, f"{y}.json")
        if os.path.exists(local_file) and not force:
            continue
        if _try_download_via_direct_urls(y):
            changed = True

    # 直链模式没有 sha，用空表保存，防止旧索引残留
    _save_sha_index(sha_index)
    return changed

# ===================== DataFrame 构建 =====================
def build_dataframe() -> pd.DataFrame:
    holiday_map_local: Dict[str, Dict[str, Any]] = {}
    years_local: List[int] = []

    if not os.path.isdir(FOLDER_PATH):
        raise RuntimeError(f"本地目录不存在：{FOLDER_PATH}")

    for filename in os.listdir(FOLDER_PATH):
        if not filename.endswith(".json") or not filename[:4].isdigit() or filename.startswith("._"):
            continue
        filepath = os.path.join(FOLDER_PATH, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            print(f"⚠️ 读取失败：{filename}，原因：{e}")
            continue
        days = data.get("days", [])
        if not days:
            continue
        year = int(filename[:4])
        years_local.append(year)
        for day in days:
            date_str = day.get("date")
            if not date_str:
                continue
            holiday_map_local[date_str] = {
                "name": day.get("name", ""),
                "isOffDay": bool(day.get("isOffDay", False)),
            }

    results: List[Dict[str, Any]] = []
    for year in sorted(set(years_local)):
        current = datetime(year, 1, 1)
        end_date = datetime(year, 12, 31)
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            weekday = current.weekday()
            weekday_str = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]

            if date_str in holiday_map_local:
                info = holiday_map_local[date_str]
                is_off = info["isOffDay"]
                raw_name = info["name"]
                if is_off:
                    type_name = "法定节假日"
                    festival = raw_name
                else:
                    if weekday >= 5:
                        type_name = "调休补班日"
                        festival = raw_name
                    else:
                        type_name = "工作日"
                        festival = "无"
            else:
                if weekday >= 5:
                    type_name = "周末休息"
                    raw_name = "周末"
                    is_off = True
                    festival = "无"
                else:
                    type_name = "工作日"
                    raw_name = "工作日"
                    is_off = False
                    festival = "无"

            results.append({
                "date": date_str,
                "raw_name": raw_name,
                "is_off_day": bool(is_off),
                "weekday": weekday_str,
                "type": type_name,
                "festival": festival,
                "year": year
            })
            current += timedelta(days=1)

    df_local = pd.DataFrame(results)
    if not df_local.empty:
        df_local.set_index("date", inplace=True)
    else:
        # 返回空表但保持列结构，避免后续访问报错
        df_local = pd.DataFrame(columns=["raw_name","is_off_day","weekday","type","festival","year"])
        df_local.index.name = "date"
    return df_local

# ===================== 业务方法（后端可直接调用） =====================
def get_holiday_info(date_str: str) -> Dict[str, Any]:
    """
    后端内部直接调用：
        from app import get_holiday_info
        info = get_holiday_info("2025-10-01")
    """
    with _df_lock:
        if df is None or df.empty:
            raise RuntimeError("数据未初始化或为空")
        try:
            _ = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise ValueError("日期格式错误，应为 YYYY-MM-DD")
        if date_str not in df.index:
            raise KeyError(f"{date_str} 不在支持范围内")
        row = df.loc[date_str]
        return {
            "date": date_str,
            "weekday": str(row["weekday"]),
            "is_off_day": bool(row["is_off_day"]),
            "type": str(row["type"]),
            "festival": str(row["festival"]),
            "raw_name": str(row["raw_name"]),
            "year": int(row["year"]),
        }

# ===================== 初始化 & 定时刷新 =====================
def scheduled_refresh():
    try:
        print("⏳ 定时刷新开始...")
        changed = fetch_all_year_jsons(force=False)
        if changed:
            print("✅ 有更新，重建 DataFrame")
            df_local = build_dataframe()
            with _df_lock:
                global df
                df = df_local
        else:
            print("ℹ️ 无更新，保持现有数据")
    except Exception as e:
        print(f"❌ 定时刷新失败：{e}")

def _init_data() -> None:
    os.makedirs(FOLDER_PATH, exist_ok=True)
    try:
        changed = fetch_all_year_jsons(force=False)
        if changed:
            print("✅ JSON 已更新/新增。")
        else:
            print("ℹ️ 使用本地缓存或无变化。")
    except Exception as e:
        print(f"❌ 拉取 JSON 失败（将仅使用本地已有文件）：{e}")

    df_local = build_dataframe()
    with _df_lock:
        global df
        df = df_local

    if df_local.empty:
        print("⚠️ 未加载到任何年份的数据（目录为空或下载失败）。服务可用，但查询大概率 404。")
    else:
        print(f"✅ 数据就绪，覆盖年份：{df['year'].min()} ~ {df['year'].max()}")

# ===================== Lifespan（无 on_event 警告） =====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_data()
    scheduler.add_job(scheduled_refresh, "cron", hour=SCHED_HOUR, minute=SCHED_MIN)
    scheduler.start()
    print(f"🕒 已启动每日定时刷新：{SCHED_TZ} {SCHED_HOUR:02d}:{SCHED_MIN:02d}")
    try:
        yield
    finally:
        scheduler.shutdown()

# ===================== 创建应用 & 路由 =====================
app = FastAPI(lifespan=lifespan)

# 静态目录（含前端页面），挂到 /ui，根路径 / 重定向到 /ui/
if os.path.isdir("static"):
    app.mount("/ui", StaticFiles(directory="static", html=True), name="static")

    @app.get("/")
    def index():
        return RedirectResponse(url="/ui/")

class QueryBody(BaseModel):
    date: str

@app.get("/health")
def health():
    with _df_lock:
        ready = (df is not None) and (not df.empty)
    return {"ok": ready}

@app.get("/refresh")
def refresh(force: bool = Query(False, description="是否强制重新下载所有 JSON（忽略 sha）")):
    try:
        changed = fetch_all_year_jsons(force=force)
        df_local = build_dataframe()
        with _df_lock:
            global df
            df = df_local
        return {"ok": True, "download_changed": changed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/query")
def query_date(date: str = Query(..., description="日期格式为 YYYY-MM-DD")):
    try:
        return get_holiday_info(date)
    except ValueError as ve:
        return JSONResponse(status_code=400, content={"error": str(ve)})
    except KeyError as ke:
        return JSONResponse(status_code=404, content={"error": str(ke)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/query")
def query_date_post(body: QueryBody):
    try:
        return get_holiday_info(body.date)
    except ValueError as ve:
        return JSONResponse(status_code=400, content={"error": str(ve)})
    except KeyError as ke:
        return JSONResponse(status_code=404, content={"error": str(ke)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# 直接 python app.py 运行（开发用）
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=12081, reload=True)
