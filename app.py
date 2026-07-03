"""评论区爬取工具 — FastAPI 后端"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    FileResponse,
    StreamingResponse,
)
import uvicorn

# 确保 crawlers 可导入
sys.path.insert(0, str(Path(__file__).parent))

from crawlers import (
    BilibiliCrawler,
    XiaohongshuCrawler,
    DouyinCrawler,
    Comment,
    WorkInfo,
)

app = FastAPI(title="评论区爬取工具", version="1.0")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = BASE_DIR / "templates"
LOGIN_LOG_DIR = OUTPUT_DIR / "_login_logs"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---- 爬虫实例 ----
CRAWLERS: Dict[str, object] = {
    "bilibili": BilibiliCrawler(),
    "xiaohongshu": XiaohongshuCrawler(),
    "douyin": DouyinCrawler(),
}

# ---- 任务管理 (内存) ----
tasks: Dict[str, dict] = {}
TASK_LOCK = asyncio.Lock()


def update_task(task_id: str, **kwargs):
    """更新任务状态（asyncio 中单线程安全）"""
    if task_id in tasks:
        tasks[task_id].update(kwargs)


def _parse_optional_limit(value):
    if value in (None, "", 0, "0", "all", "ALL", "All"):
        return None
    return int(value)


def _login_log_path(platform: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOGIN_LOG_DIR / f"{platform}_{stamp}.log"


async def _run_login_subprocess(platform: str) -> Path:
    log_path = _login_log_path(platform)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    with open(log_path, "ab") as log_fp:
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "app.py"), "--login-platform", platform],
            cwd=str(BASE_DIR),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    await asyncio.sleep(1)
    if proc.poll() is not None:
        error_text = ""
        try:
            error_text = log_path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            pass
        detail = error_text.splitlines()[-1] if error_text else "浏览器登录进程启动失败"
        raise HTTPException(500, detail)

    return log_path


async def _run_login_cli(platform: str):
    if platform not in CRAWLERS:
        raise ValueError(f"不支持的平台: {platform}")
    crawler = CRAWLERS[platform]
    if not hasattr(crawler, "login_interactive"):
        return
    await crawler.login_interactive()


# ============================================================
#  路由
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """主界面"""
    index_path = TEMPLATES_DIR / "index.html"
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return "<h1>前端文件缺失</h1>"


@app.post("/api/crawl")
async def start_crawl(request: Request):
    """启动爬取任务"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求格式错误")

    platform = body.get("platform", "")
    url = body.get("url", "").strip()
    raw_max_pages = body.get("max_pages")
    max_pages = _parse_optional_limit(raw_max_pages)

    if platform not in CRAWLERS:
        raise HTTPException(400, f"不支持的平台: {platform}")
    if not url:
        raise HTTPException(400, "请输入链接")

    task_id = uuid.uuid4().hex[:12]
    crawler = CRAWLERS[platform]

    async with TASK_LOCK:
        tasks[task_id] = {
            "id": task_id,
            "platform": platform,
            "platform_name": crawler.display_name,
            "url": url,
            "status": "running",
            "progress": 0,
            "total_comments": 0,
            "comments": [],
            "error": None,
            "report_path": "",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 后台执行爬取
    asyncio.create_task(_run_crawl(task_id, platform, url, max_pages))

    return {"task_id": task_id, "status": "started"}


async def _run_crawl(task_id: str, platform: str, url: str, max_pages: int | None):
    """后台执行爬取并生成报告"""
    crawler = CRAWLERS[platform]
    try:
        def progress_cb(pct: int):
            update_task(task_id, progress=min(pct, 99))

        comments = await crawler.get_comments(
            url, max_pages=max_pages, progress_callback=progress_cb
        )
        fetched_top_comments = len(comments)
        fetched_reply_comments = sum(len(c.replies) for c in comments)
        reply_total_estimate = sum(c.reply_count for c in comments)
        total_comments = int(getattr(crawler, "_last_total", 0) or 0)
        if total_comments <= 0:
            total_comments = fetched_top_comments + max(
                fetched_reply_comments, reply_total_estimate
            )

        # 保存原始 JSON
        task_dir = OUTPUT_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        json_path = task_dir / "comments.json"
        comments_data = []
        for c in comments:
            comments_data.append({
                "username": c.username,
                "content": c.content,
                "time": c.time,
                "likes": c.likes,
                "ip_location": c.ip_location,
                "images": [{"url": img.url} for img in c.images],
                "replies": [
                    {
                        "username": r.username,
                        "content": r.content,
                        "time": r.time,
                        "likes": r.likes,
                    }
                    for r in c.replies
                ],
                "reply_count": c.reply_count,
            })
        single_result = {
            "platform": crawler.display_name,
            "url": url,
            "total_comments": total_comments,
            "fetched_top_comments": fetched_top_comments,
            "fetched_reply_comments": fetched_reply_comments,
            "reply_total_estimate": reply_total_estimate,
            "comments": comments_data,
        }
        json_path.write_text(
            json.dumps(single_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 生成 HTML 报告
        report_html = _generate_report(
            platform=crawler.display_name,
            url=url,
            comments=comments,
            task_id=task_id,
        )
        report_path = task_dir / "report.html"
        report_path.write_text(report_html, encoding="utf-8")

        async with TASK_LOCK:
            update_task(
                task_id,
                status="completed",
                progress=100,
                total_comments=total_comments,
                report_path=str(report_path),
                json_path=str(json_path),
            )

    except Exception as e:
        async with TASK_LOCK:
            update_task(task_id, status="error", error=str(e), progress=0)


@app.post("/api/crawl-account")
async def start_account_crawl(request: Request):
    """启动账号级爬取任务"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求格式错误")

    platform = body.get("platform", "")
    account_url = body.get("account_url", "").strip()
    max_works = _parse_optional_limit(body.get("max_works", 20))
    max_pages = _parse_optional_limit(body.get("max_pages", 5))

    if platform not in CRAWLERS:
        raise HTTPException(400, f"不支持的平台: {platform}")
    if not account_url:
        raise HTTPException(400, "请输入账号主页链接")

    task_id = uuid.uuid4().hex[:12]
    crawler = CRAWLERS[platform]

    async with TASK_LOCK:
        tasks[task_id] = {
            "id": task_id,
            "platform": platform,
            "platform_name": crawler.display_name,
            "url": account_url,
            "status": "running",
            "progress": 0,
            "total_comments": 0,
            "current_work": "",
            "works_done": 0,
            "works_total": 0,
            "error": None,
            "report_path": "",
            "json_path": "",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    asyncio.create_task(_run_account_crawl(task_id, platform, account_url, max_works, max_pages))
    return {"task_id": task_id, "status": "started"}


async def _run_account_crawl(task_id: str, platform: str, account_url: str, max_works: int | None, max_pages: int | None):
    """后台执行账号级爬取：作品列表 → 逐作品评论 → 汇总报告"""
    crawler = CRAWLERS[platform]
    try:
        # 阶段1：获取作品列表 (进度 0-20%)
        update_task(task_id, current_work="正在获取作品列表...")
        works = await crawler.get_user_works(
            account_url,
            max_works=max_works,
            progress_callback=lambda p: update_task(task_id, progress=int(p * 0.2), current_work=f"获取作品列表... {int(p)}%"),
        )

        if not works:
            update_task(task_id, status="error", error="未找到任何作品，请检查账号链接是否正确")
            return

        update_task(task_id, progress=20, works_total=len(works), works_done=0,
                    current_work=f"找到 {len(works)} 个作品，开始爬取评论区...")

        # 阶段2：逐作品爬取评论 (进度 20-95%)
        all_data: list = []  # [(work_info, [comments]), ...]
        total_comments = 0

        for idx, work in enumerate(works):
            update_task(task_id,
                        current_work=f"正在爬取 ({idx+1}/{len(works)}): {work.title[:30]}",
                        works_done=idx)

            try:
                comments = await crawler.get_comments(work.url, max_pages=max_pages)
            except Exception as e:
                update_task(task_id, current_work=f"爬取失败 ({idx+1}/{len(works)}): {work.title[:30]}", last_error=str(e))
                print(f"[{crawler.display_name}] {work.url} 抓取失败: {e}")
                comments = []

            all_data.append((work, comments))
            total_comments += len(comments)

            # 进度: 20% + 75% 按作品比例分配
            pct = 20 + int(75 * (idx + 1) / len(works))
            update_task(task_id, progress=pct, total_comments=total_comments,
                        works_done=idx + 1)

            # 爬取间隔
            await asyncio.sleep(1)

        # 阶段3：保存数据 + 生成报告 (进度 95-100%)
        update_task(task_id, progress=95, current_work="正在生成报告...")

        task_dir = OUTPUT_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # 保存 JSON
        json_path = task_dir / "account_comments.json"
        json_data = {
            "account_url": account_url,
            "platform": crawler.display_name,
            "total_works": len(works),
            "total_comments": total_comments,
            "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "works": [],
        }
        for work, comments in all_data:
            json_data["works"].append({
                "work_id": work.work_id,
                "title": work.title,
                "url": work.url,
                "comment_count": len(comments),
                "comments": [
                    {
                        "username": c.username, "content": c.content,
                        "time": c.time, "likes": c.likes,
                        "ip_location": c.ip_location,
                        "images": [{"url": img.url} for img in c.images],
                        "replies": [{"username": r.username, "content": r.content, "time": r.time, "likes": r.likes} for r in c.replies],
                        "reply_count": c.reply_count,
                    }
                    for c in comments
                ],
            })
        json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 生成 HTML 汇总报告
        report_html = _generate_account_report(
            platform=crawler.display_name,
            account_url=account_url,
            all_data=all_data,
            task_id=task_id,
        )
        report_path = task_dir / "account_report.html"
        report_path.write_text(report_html, encoding="utf-8")

        update_task(task_id, status="completed", progress=100,
                    total_comments=total_comments,
                    report_path=str(report_path), json_path=str(json_path))

    except Exception as e:
        update_task(task_id, status="error", error=str(e))


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    """查询任务状态 (用于轮询)"""
    async with TASK_LOCK:
        if task_id not in tasks:
            raise HTTPException(404, "任务不存在")
        t = tasks[task_id].copy()
    # 返回摘要字段，去掉可能很大的列表
    for key in ("comments", "all_data"):
        t.pop(key, None)
    return t


@app.get("/api/task/{task_id}/stream")
async def task_stream(task_id: str, request: Request):
    """SSE 实时推送进度"""
    async def event_stream():
        last_progress = -1
        while True:
            if await request.is_disconnected():
                break
            async with TASK_LOCK:
                t = tasks.get(task_id)
            if t is None:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                break
            progress = t.get("progress", 0)
            status = t.get("status", "running")
            if progress != last_progress or status != "running":
                yield f"data: {json.dumps({'progress': progress, 'status': status, 'error': t.get('error'), 'total': t.get('total_comments', 0), 'current_work': t.get('current_work', ''), 'works_done': t.get('works_done', 0), 'works_total': t.get('works_total', 0)})}\n\n"
                last_progress = progress
            if status in ("completed", "error"):
                break
            await asyncio.sleep(0.8)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/download/{task_id}")
async def download_report(task_id: str, fmt: str = "html"):
    """下载报告 (html 或 json)"""
    async with TASK_LOCK:
        t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "completed":
        raise HTTPException(400, "任务尚未完成")

    if fmt == "json":
        json_path = t.get("json_path", "")
        if json_path and os.path.exists(json_path):
            return FileResponse(
                json_path,
                filename=f"comments_{task_id}.json",
                media_type="application/json",
            )
    else:
        report_path = t.get("report_path", "")
        if report_path and os.path.exists(report_path):
            return FileResponse(
                report_path,
                filename=f"report_{task_id}.html",
                media_type="text/html",
            )
    raise HTTPException(404, "报告文件不存在")


@app.post("/api/login/{platform}")
async def start_login(platform: str):
    """打开浏览器进行扫码登录"""
    if platform not in CRAWLERS:
        raise HTTPException(400, f"不支持的平台: {platform}")
    crawler = CRAWLERS[platform]
    if not hasattr(crawler, "login_interactive"):
        return {"message": f"{crawler.display_name} 不需要登录"}

    log_path = await _run_login_subprocess(platform)
    return {
        "message": (
            f"已尝试打开浏览器，请在浏览器中完成 {crawler.display_name} 登录，"
            "登录成功后直接关闭浏览器窗口即可保存。"
        ),
        "log_path": str(log_path),
    }


@app.get("/api/platforms")
async def list_platforms():
    """列出支持的平台"""
    return [
        {
            "id": c.platform_name,
            "name": c.display_name,
            "needs_login": hasattr(c, "login_interactive"),
            "api_based": c.platform_name == "bilibili",
        }
        for c in CRAWLERS.values()
    ]


# ============================================================
#  HTML 报告生成
# ============================================================

def _generate_report(
    platform: str, url: str, comments: list, task_id: str
) -> str:
    """生成独立的精美 HTML 报告"""
    total = len(comments)
    total_images = sum(len(c.images) for c in comments)
    total_replies = sum(c.reply_count for c in comments)
    total_likes = sum(c.likes for c in comments)

    # 评论卡片
    cards_html = ""
    for i, c in enumerate(comments):
        # 图片
        imgs_html = ""
        for img in c.images:
            imgs_html += (
                f'<div class="cmt-img-wrap">'
                f'<img src="{_escape(img.url)}" loading="lazy" '
                f'onerror="this.parentElement.remove()" />'
                f'</div>'
            )

        # 子回复
        replies_html = ""
        if c.replies:
            replies_html += '<div class="replies-section">'
            for r in c.replies:
                replies_html += (
                    f'<div class="reply-item">'
                    f'<span class="reply-user">{_escape(r.username)}</span>'
                    f'<span class="reply-text">{_escape(r.content)}</span>'
                    f'<span class="reply-meta">{r.time} · {r.likes} 赞</span>'
                    f'</div>'
                )
            remaining = c.reply_count - len(c.replies)
            if remaining > 0:
                replies_html += (
                    f'<div class="more-replies">… 还有 {remaining} 条回复</div>'
                )
            replies_html += "</div>"

        cards_html += f"""
        <div class="comment-card">
            <div class="card-header">
                <span class="username">{_escape(c.username)}</span>
                {f'<span class="ip-tag">{_escape(c.ip_location)}</span>' if c.ip_location else ''}
                <span class="time">{c.time}</span>
            </div>
            <div class="card-body">{_escape(c.content)}</div>
            {imgs_html}
            <div class="card-footer">
                <span class="stat">👍 {c.likes}</span>
                {f'<span class="stat">💬 {c.reply_count} 回复</span>' if c.reply_count else ''}
            </div>
            {replies_html}
        </div>
        """

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>评论区爬取报告 - {_escape(platform)}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #2c3e50; line-height:1.6; }}
.report {{ max-width:900px; margin:0 auto; padding:24px 16px; }}
.header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color:#fff; border-radius:16px; padding:32px; margin-bottom:24px; box-shadow: 0 4px 20px rgba(102,126,234,.3); }}
.header h1 {{ font-size:26px; margin-bottom:12px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:16px; font-size:14px; opacity:.9; }}
.meta span {{ background:rgba(255,255,255,.2); padding:4px 12px; border-radius:20px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:16px; margin-bottom:32px; }}
.stat-card {{ background:#fff; border-radius:12px; padding:20px; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.stat-num {{ font-size:32px; font-weight:700; color:#667eea; }}
.stat-label {{ font-size:13px; color:#888; margin-top:4px; }}
.section-title {{ font-size:20px; font-weight:600; margin-bottom:16px; color:#333; }}
.comment-card {{ background:#fff; border-radius:12px; padding:20px; margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,.05); transition:box-shadow .2s; }}
.comment-card:hover {{ box-shadow:0 4px 16px rgba(0,0,0,.1); }}
.card-header {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }}
.username {{ font-weight:600; color:#333; font-size:15px; }}
.ip-tag {{ font-size:11px; background:#f0f0f0; color:#999; padding:2px 8px; border-radius:10px; }}
.time {{ font-size:12px; color:#aaa; margin-left:auto; }}
.card-body {{ font-size:15px; line-height:1.8; white-space:pre-wrap; word-break:break-word; color:#444; }}
.cmt-img-wrap {{ display:inline-block; margin:8px 8px 0 0; }}
.cmt-img-wrap img {{ max-width:180px; max-height:180px; border-radius:8px; border:1px solid #eee; cursor:pointer; object-fit:cover; }}
.card-footer {{ margin-top:12px; display:flex; gap:16px; }}
.card-footer .stat {{ font-size:13px; color:#888; }}
.replies-section {{ margin-top:12px; padding-top:12px; border-top:1px solid #f0f0f0; }}
.reply-item {{ padding:6px 0; display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; font-size:13px; }}
.reply-user {{ font-weight:600; color:#667eea; white-space:nowrap; }}
.reply-text {{ color:#555; flex:1; }}
.reply-meta {{ font-size:11px; color:#bbb; white-space:nowrap; }}
.more-replies {{ font-size:12px; color:#aaa; padding:4px 0; }}
.footer {{ text-align:center; padding:24px; color:#bbb; font-size:12px; }}
.empty {{ text-align:center; padding:60px 20px; color:#bbb; font-size:16px; }}
@media print {{ body {{ background:#fff; }} .header {{ box-shadow:none; }} }}
</style>
</head>
<body>
<div class="report">
<div class="header">
    <h1>📊 评论区爬取报告</h1>
    <div class="meta">
        <span>平台: {_escape(platform)}</span>
        <span>来源: {_escape(url)}</span>
        <span>爬取时间: {now}</span>
    </div>
</div>

<div class="stats">
    <div class="stat-card"><div class="stat-num">{total}</div><div class="stat-label">总评论数</div></div>
    <div class="stat-card"><div class="stat-num">{total_images}</div><div class="stat-label">图片数</div></div>
    <div class="stat-card"><div class="stat-num">{total_replies}</div><div class="stat-label">总回复数</div></div>
    <div class="stat-card"><div class="stat-num">{total_likes}</div><div class="stat-label">总点赞数</div></div>
</div>

<h2 class="section-title">💬 评论列表 ({total})</h2>
{cards_html if cards_html else '<div class="empty">暂无评论数据</div>'}

<div class="footer">由评论区爬取工具自动生成 · {now}</div>
</div>
</body>
</html>"""


def _escape(text: str) -> str:
    """HTML 转义"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _generate_account_report(
    platform: str, account_url: str, all_data: list, task_id: str
) -> str:
    """生成账号级汇总 HTML 报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_works = len(all_data)
    total_comments = sum(len(comments) for _, comments in all_data)
    total_likes = sum(sum(c.likes for c in comments) for _, comments in all_data)

    # 作品章节
    sections_html = ""
    for idx, (work, comments) in enumerate(all_data):
        cards_html = ""
        for c in comments:
            imgs_html = ""
            for img in c.images:
                imgs_html += (
                    f'<div class="cmt-img-wrap">'
                    f'<img src="{_escape(img.url)}" loading="lazy" '
                    f'onerror="this.parentElement.remove()" />'
                    f'</div>'
                )
            replies_html = ""
            if c.replies:
                replies_html += '<div class="replies-section">'
                for r in c.replies:
                    replies_html += (
                        f'<div class="reply-item">'
                        f'<span class="reply-user">{_escape(r.username)}</span>'
                        f'<span class="reply-text">{_escape(r.content)}</span>'
                        f'<span class="reply-meta">{r.time} · {r.likes} 赞</span>'
                        f'</div>'
                    )
                replies_html += "</div>"

            cards_html += f"""
            <div class="comment-card">
                <div class="card-header">
                    <span class="username">{_escape(c.username)}</span>
                    {f'<span class="ip-tag">{_escape(c.ip_location)}</span>' if c.ip_location else ''}
                    <span class="time">{c.time}</span>
                </div>
                <div class="card-body">{_escape(c.content)}</div>
                {imgs_html}
                <div class="card-footer">
                    <span class="stat">👍 {c.likes}</span>
                    {f'<span class="stat">💬 {c.reply_count} 回复</span>' if c.reply_count else ''}
                </div>
                {replies_html}
            </div>
            """

        sections_html += f"""
        <div class="work-section">
            <div class="work-header">
                <span class="work-index">#{idx + 1}</span>
                <div class="work-info">
                    <a href="{_escape(work.url)}" target="_blank" class="work-title">{_escape(work.title or work.url)}</a>
                    <span class="work-meta">💬 {len(comments)} 条评论</span>
                </div>
            </div>
            {cards_html if cards_html else '<div class="empty-sm">暂无评论</div>'}
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>账号评论爬取报告 - {_escape(platform)}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #2c3e50; line-height:1.6; }}
.report {{ max-width:960px; margin:0 auto; padding:24px 16px; }}
.header {{ background: linear-gradient(135deg, #e74c3c 0%, #f39c12 100%); color:#fff; border-radius:16px; padding:32px; margin-bottom:24px; box-shadow: 0 4px 20px rgba(231,76,60,.3); }}
.header h1 {{ font-size:26px; margin-bottom:12px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:12px; font-size:14px; opacity:.9; }}
.meta span {{ background:rgba(255,255,255,.2); padding:4px 12px; border-radius:20px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:16px; margin-bottom:32px; }}
.stat-card {{ background:#fff; border-radius:12px; padding:20px; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.stat-num {{ font-size:32px; font-weight:700; color:#e74c3c; }}
.stat-label {{ font-size:13px; color:#888; margin-top:4px; }}
.section-title {{ font-size:20px; font-weight:600; margin-bottom:16px; color:#333; }}
.work-section {{ margin-bottom:32px; }}
.work-header {{ display:flex; align-items:center; gap:12px; margin-bottom:12px; padding:12px 16px; background:#fff; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,.05); }}
.work-index {{ font-size:18px; font-weight:700; color:#e74c3c; min-width:36px; }}
.work-info {{ flex:1; }}
.work-title {{ font-size:15px; font-weight:600; color:#333; text-decoration:none; }}
.work-title:hover {{ color:#e74c3c; text-decoration:underline; }}
.work-meta {{ font-size:12px; color:#aaa; margin-left:12px; white-space:nowrap; }}
.comment-card {{ background:#fff; border-radius:12px; padding:20px; margin-bottom:10px; box-shadow:0 1px 4px rgba(0,0,0,.05); }}
.card-header {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }}
.username {{ font-weight:600; color:#333; font-size:15px; }}
.ip-tag {{ font-size:11px; background:#f0f0f0; color:#999; padding:2px 8px; border-radius:10px; }}
.time {{ font-size:12px; color:#aaa; margin-left:auto; }}
.card-body {{ font-size:15px; line-height:1.8; white-space:pre-wrap; word-break:break-word; color:#444; }}
.cmt-img-wrap {{ display:inline-block; margin:8px 8px 0 0; }}
.cmt-img-wrap img {{ max-width:180px; max-height:180px; border-radius:8px; border:1px solid #eee; cursor:pointer; object-fit:cover; }}
.card-footer {{ margin-top:10px; display:flex; gap:16px; }}
.card-footer .stat {{ font-size:13px; color:#888; }}
.replies-section {{ margin-top:10px; padding-top:10px; border-top:1px solid #f0f0f0; }}
.reply-item {{ padding:4px 0; display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; font-size:13px; }}
.reply-user {{ font-weight:600; color:#e74c3c; white-space:nowrap; }}
.reply-text {{ color:#555; flex:1; }}
.reply-meta {{ font-size:11px; color:#bbb; white-space:nowrap; }}
.empty-sm {{ text-align:center; padding:20px; color:#ccc; font-size:13px; }}
.footer {{ text-align:center; padding:24px; color:#bbb; font-size:12px; }}
@media print {{ body {{ background:#fff; }} .header {{ box-shadow:none; }} }}
</style>
</head>
<body>
<div class="report">
<div class="header">
    <h1>📊 账号评论爬取报告</h1>
    <div class="meta">
        <span>平台: {_escape(platform)}</span>
        <span>账号: {_escape(account_url)}</span>
        <span>爬取时间: {now}</span>
    </div>
</div>

<div class="stats">
    <div class="stat-card"><div class="stat-num">{total_works}</div><div class="stat-label">作品数</div></div>
    <div class="stat-card"><div class="stat-num">{total_comments}</div><div class="stat-label">总评论数</div></div>
    <div class="stat-card"><div class="stat-num">{total_likes}</div><div class="stat-label">总点赞数</div></div>
    <div class="stat-card"><div class="stat-num">{sum(1 for _, comments in all_data for c in comments if c.images) if all_data else 0}</div><div class="stat-label">带图评论</div></div>
</div>

<h2 class="section-title">📋 作品评论区详情</h2>
{sections_html if sections_html else '<div class="empty">暂无数据</div>'}

<div class="footer">由评论区爬取工具自动生成 · {now}</div>
</div>
</body>
</html>"""


# ============================================================
#  启动
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--login-platform":
        try:
            asyncio.run(_run_login_cli(sys.argv[2]))
        except Exception as e:
            print(f"[login:{sys.argv[2]}] {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    print("=" * 50)
    print("  评论区爬取工具 v1.0")
    print("  支持: B站 / 小红书 / 抖音")
    print(f"  打开浏览器访问: http://127.0.0.1:8765")
    print("=" * 50)
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
