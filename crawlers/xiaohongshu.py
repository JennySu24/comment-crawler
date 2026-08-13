"""小红书评论爬虫 — Playwright 浏览器自动化 + API 响应拦截"""
import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import List, Optional, Callable

from .base import BaseCrawler, Comment, CommentImage, WorkInfo

COOKIE_DIR = Path(__file__).parent.parent / "cookies"
COOKIE_FILE = COOKIE_DIR / "xiaohongshu.json"


class XiaohongshuCrawler(BaseCrawler):
    platform_name = "xiaohongshu"
    display_name = "小红书"
    login_mode = "none"

    BROWSER_ARGS = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    def extract_id(self, url: str) -> str:
        # https://www.xiaohongshu.com/explore/64a1b2c3000000000102d4e5
        # https://www.xiaohongshu.com/discovery/item/...
        # https://xhslink.com/xxxx (短链)
        for pat in [r"/explore/([a-zA-Z0-9]+)", r"/discovery/item/([a-zA-Z0-9]+)"]:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        raise ValueError(f"无法解析小红书链接: {url}（请使用完整笔记链接）")

    def _normalize_note_url(self, url: str) -> str:
        """规范化用户输入的笔记链接，保留查询参数并去掉首尾脏字符。"""
        cleaned = url.strip().strip('"').strip("'")
        parts = urlsplit(cleaned)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return cleaned
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment))

    async def _load_cookies(self, context):
        """加载已保存的 cookie"""
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if isinstance(cookies, dict):
                    cookies = cookies.get("cookies", [])
                safe_cookies = []
                for cookie in cookies or []:
                    if not isinstance(cookie, dict):
                        continue
                    safe_cookie = {
                        key: cookie[key]
                        for key in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
                        if key in cookie
                    }
                    if "name" in safe_cookie and "value" in safe_cookie:
                        safe_cookies.append(safe_cookie)
                if safe_cookies:
                    await context.add_cookies(safe_cookies)
                return True
            except Exception:
                return False
        return False

    async def _save_cookies(self, context):
        """保存 cookie 供下次使用"""
        cookies = await context.cookies()
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "w") as f:
            json.dump(cookies, f, ensure_ascii=False)

    async def _close_quietly(self, page=None, context=None, browser=None):
        """关闭 Playwright 对象，避免清理阶段覆盖真正异常。"""
        for target in (page, context, browser):
            if target is None:
                continue
            with contextlib.suppress(Exception):
                await target.close()

    async def _drain_tasks(self, tasks: set):
        if not tasks:
            return
        await asyncio.gather(*list(tasks), return_exceptions=True)

    def _track_json_response(self, response, tasks: set, buf: list):
        async def _read():
            try:
                buf.append(await response.json())
            except Exception:
                pass

        task = asyncio.create_task(_read())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _extract_comment_id(self, item: dict) -> str:
        for key in ("comment_id", "id", "cid", "sub_comment_id", "reply_id", "rpid"):
            value = item.get(key)
            if value:
                return str(value)
        return ""

    def _parse_comment_items(self, data: dict) -> List[dict]:
        return (
            data.get("data", {}).get("comments", [])
            or data.get("data", {}).get("items", [])
            or data.get("data", {}).get("list", [])
            or data.get("comments", [])
            or data.get("items", [])
            or data.get("list", [])
            or data.get("data", {}).get("sub_comments", [])
            or data.get("sub_comments", [])
            or data.get("reply_comments", [])
            or []
        )

    def _parse_comment_item(self, item: dict) -> Comment:
        user = item.get("user_info", {}) or item.get("user", {})
        username = user.get("nickname", user.get("nick_name", ""))
        content = item.get("content", item.get("desc", item.get("text", "")))
        create_time = item.get("create_time", 0)
        time_str = str(create_time)
        if isinstance(create_time, (int, float)) and create_time > 0:
            try:
                from datetime import datetime
                time_str = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, OverflowError, ValueError):
                time_str = str(create_time)

        likes_raw = item.get("like_count", item.get("likes", 0))
        try:
            likes = int(likes_raw)
        except (ValueError, TypeError):
            likes = 0

        images: List[CommentImage] = []
        for pic in item.get("pictures", []) or []:
            img_url = ""
            if isinstance(pic, dict):
                for key in ("url", "url_default", "origin_url"):
                    val = pic.get(key)
                    if isinstance(val, list) and val:
                        img_url = val[0]
                        break
                    if isinstance(val, str) and val:
                        img_url = val
                        break
            elif isinstance(pic, str):
                img_url = pic
            if img_url:
                images.append(CommentImage(url=img_url))

        sub_replies: List[Comment] = []
        for sub in item.get("sub_comments", []) or item.get("reply_comments", []) or []:
            sub_replies.append(self._parse_comment_item(sub))

        reply_count_raw = item.get("sub_comment_count", item.get("reply_count", len(sub_replies)))
        try:
            reply_count = int(reply_count_raw)
        except (ValueError, TypeError):
            reply_count = len(sub_replies)

        return Comment(
            username=username or "未知用户",
            content=content,
            comment_id=self._extract_comment_id(item),
            time=time_str,
            likes=likes,
            images=images,
            replies=[r for r in sub_replies if r.content],
            reply_count=reply_count,
        )

    def _build_reply_api_urls(self, api_url: str, root_comment_id: str, cursor: int | str = 0) -> List[str]:
        from urllib.parse import urlencode, parse_qs, urlparse, urlunparse

        parsed = urlparse(api_url)
        base_query = parse_qs(parsed.query)
        base_query["cursor"] = [str(cursor)]

        candidate_paths = [parsed.path]
        for marker, replacement in (
            ("/comment/list", "/comment/reply/list"),
            ("/comment", "/comment/reply"),
        ):
            if marker in parsed.path:
                candidate_paths.extend([
                    parsed.path.replace(marker, replacement),
                    parsed.path.replace(marker, f"{replacement}/list"),
                ])

        candidate_param_names = (
            "comment_id",
            "cid",
            "root_comment_id",
            "sub_comment_id",
            "reply_id",
            "parent_comment_id",
            "note_id",
            "cursor",
        )
        urls: List[str] = []
        for path in dict.fromkeys(candidate_paths):
            for param_name in candidate_param_names:
                query = {key: list(value) for key, value in base_query.items()}
                query[param_name] = [str(root_comment_id)]
                urls.append(urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query, doseq=True), parsed.fragment)))
        return urls

    async def _fetch_reply_payload(self, page, api_url: str, root_comment_id: str, cursor: int | str = 0) -> dict:
        for next_url in self._build_reply_api_urls(api_url, root_comment_id, cursor):
            try:
                payload = await page.evaluate("""
                    async (url) => {
                        const resp = await fetch(url, { credentials: 'include' });
                        const text = await resp.text();
                        try {
                            return JSON.parse(text);
                        } catch (err) {
                            return { comments: [], cursor: 0, has_more: 0, __fetch_error: text.slice(0, 200) };
                        }
                    }
                """, next_url)
            except Exception as e:
                payload = {"comments": [], "cursor": 0, "has_more": 0, "__fetch_error": str(e)}

            if self._parse_comment_items(payload):
                return payload

        return {"comments": [], "cursor": 0, "has_more": 0}

    async def _fetch_all_replies(self, page, api_url: str, comment: Comment, max_depth: int = 3) -> List[Comment]:
        if max_depth <= 0 or not comment.comment_id:
            return []

        collected: List[Comment] = []
        seen_reply_ids = set()
        cursor = 0
        has_more = True
        page_count = 0

        while has_more and page_count < 20:
            payload = await self._fetch_reply_payload(page, api_url, comment.comment_id, cursor)
            reply_items = self._parse_comment_items(payload)
            if not reply_items:
                break

            next_cursor = int(payload.get("cursor", payload.get("data", {}).get("cursor", 0)) or 0)
            raw_has_more = payload.get("has_more", payload.get("data", {}).get("has_more", 0))
            has_more = str(raw_has_more).lower() not in {"0", "false", "none", ""}

            for item in reply_items:
                reply = self._parse_comment_item(item)
                if not reply.content:
                    continue
                reply_key = reply.comment_id or reply.content[:80]
                if reply_key in seen_reply_ids:
                    continue
                seen_reply_ids.add(reply_key)
                collected.append(reply)

                if max_depth > 1 and reply.reply_count > len(reply.replies) and reply.comment_id:
                    nested_replies = await self._fetch_all_replies(page, api_url, reply, max_depth=max_depth - 1)
                    for nested in nested_replies:
                        nested_key = nested.comment_id or nested.content[:80]
                        if nested_key not in seen_reply_ids:
                            seen_reply_ids.add(nested_key)
                            collected.append(nested)

            if not has_more or next_cursor == cursor:
                break

            cursor = next_cursor
            page_count += 1
            await asyncio.sleep(0.6)

        return collected

    async def _attach_full_replies(self, page, api_url: str, comments: List[Comment]):
        for comment in comments:
            reply_count = int(comment.reply_count or 0)
            if reply_count <= len(comment.replies) or not comment.comment_id:
                continue
            try:
                extra_replies = await self._fetch_all_replies(page, api_url, comment, max_depth=3)
            except Exception:
                continue
            existing_keys = {r.comment_id or r.content[:80] for r in comment.replies}
            for reply in extra_replies:
                reply_key = reply.comment_id or reply.content[:80]
                if reply_key not in existing_keys:
                    existing_keys.add(reply_key)
                    comment.replies.append(reply)

    async def _looks_like_login_required(self, page) -> bool:
        try:
            if "/login" in page.url.lower():
                return True
        except Exception:
            return False

        for text in ("登录后查看更多", "扫码登录", "登录后可浏览", "请登录"):
            try:
                if await page.locator(f"text={text}").first.is_visible(timeout=800):
                    return True
            except Exception:
                pass
        return False

    async def login_interactive(self) -> bool:
        """打开浏览器让用户扫码登录，登录完成后关闭窗口即可保存 cookie。"""
        from playwright.async_api import async_playwright

        browser = context = page = None
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=False, args=self.BROWSER_ARGS)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                await page.goto("https://www.xiaohongshu.com", timeout=60000)
                print("\n请在浏览器中扫码登录小红书。登录成功后直接关闭浏览器窗口，程序会自动保存 cookie。")

                deadline = asyncio.get_running_loop().time() + 180
                while asyncio.get_running_loop().time() < deadline:
                    if page.is_closed():
                        break
                    await asyncio.sleep(1)

                await self._save_cookies(context)
            finally:
                await self._close_quietly(page=page, context=context, browser=browser)
        return True

    # ---- 获取用户作品列表 ----
    def _extract_user_id(self, user_input: str) -> str:
        """从用户主页链接提取 user_id"""
        m = re.search(r"xiaohongshu\.com/user/profile/([a-zA-Z0-9]+)", user_input)
        if m:
            return m.group(1)
        user_input = user_input.strip()
        if re.match(r"^[a-zA-Z0-9]{20,}$", user_input):
            return user_input
        raise ValueError("请输入小红书用户主页链接（如 https://www.xiaohongshu.com/user/profile/xxx）")

    async def get_user_works(
        self,
        user_input: str,
        max_works: Optional[int] = 30,
        progress_callback: Optional[Callable] = None,
    ) -> List[WorkInfo]:
        from playwright.async_api import async_playwright

        user_id = self._extract_user_id(user_input)
        profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        works: List[WorkInfo] = []
        seen_ids = set()
        api_data_buf: list = []
        limit = max_works if max_works and max_works > 0 else None

        if progress_callback:
            progress_callback(5)

        async with async_playwright() as p:
            browser = context = page = None
            pending_responses = set()

            def on_response(response):
                url_lower = response.url.lower()
                if "user_posted" in url_lower or "profile/note" in url_lower:
                    self._track_json_response(response, pending_responses, api_data_buf)

            try:
                browser = await p.chromium.launch(headless=True, args=self.BROWSER_ARGS)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                page.on("response", on_response)

                # 加载已保存的 cookie
                await self._load_cookies(context)

                try:
                    await page.goto(profile_url, wait_until="commit", timeout=60000)
                    await page.wait_for_timeout(4000)
                except Exception as goto_err:
                    raise Exception(
                        f"用户主页加载超时，可能原因：\n"
                        f"1. 网络不稳定或需要代理\n"
                        f"2. 小红书反爬拦截（建议先点击「登录」扫码登录后再爬取）\n"
                        f"3. 用户主页链接无效\n"
                        f"原始错误: {goto_err}"
                    )
                await self._drain_tasks(pending_responses)

                if progress_callback:
                    progress_callback(15)

                # 滚动加载更多笔记
                idle_rounds = 0
                loop_count = max((limit or 300) // 10, 3)
                for round_idx in range(loop_count):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)
                    await self._drain_tasks(pending_responses)

                    before_count = len(works)
                    for data in list(api_data_buf):
                        notes = (data.get("data", {}).get("notes", [])
                                 or data.get("notes", [])
                                 or [])
                        for note in notes:
                            nid = note.get("note_id", "")
                            if nid and nid not in seen_ids:
                                seen_ids.add(nid)
                                works.append(WorkInfo(
                                    work_id=nid,
                                    title=note.get("display_title", note.get("title", "")),
                                    url=f'https://www.xiaohongshu.com/explore/{nid}',
                                    cover="",
                                    stats={
                                        "liked": note.get("liked_count", 0),
                                        "type": note.get("type", "normal"),
                                    },
                                ))
                    api_data_buf.clear()

                    if len(works) == before_count:
                        idle_rounds += 1
                    else:
                        idle_rounds = 0

                    if progress_callback:
                        if limit:
                            pct = min(90, 15 + int(75 * len(works) / limit))
                        else:
                            pct = min(90, 15 + round_idx * 3)
                        progress_callback(pct)

                    if limit is not None and len(works) >= limit:
                        break

                    if limit is None and idle_rounds >= 3:
                        break

                    await asyncio.sleep(1)

                if not works:
                    if await self._looks_like_login_required(page):
                        raise Exception("小红书账号主页当前不支持匿名访问，或页面接口已变更。")
                    raise Exception("未从小红书账号主页抓到任何作品，可能是账号无公开作品，或页面接口已变更。")
            except Exception as e:
                raise Exception(f"获取小红书作品列表失败: {e}")
            finally:
                if page is not None:
                    with contextlib.suppress(Exception):
                        page.remove_listener("response", on_response)
                await self._drain_tasks(pending_responses)
                await self._close_quietly(page=page, context=context, browser=browser)

        if progress_callback:
            progress_callback(100)
        return works if limit is None else works[:limit]

    async def get_comments(
        self,
        url: str,
        max_pages: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ) -> List[Comment]:
        from playwright.async_api import async_playwright

        note_url = self._normalize_note_url(url)
        comments: List[Comment] = []
        seen_contents = set()
        api_data_buf: list = []
        page_limit = max_pages if max_pages and max_pages > 0 else None
        comment_api_url: Optional[str] = None

        if progress_callback:
            progress_callback(5)

        async with async_playwright() as p:
            browser = context = page = None
            pending_responses = set()

            def on_response(response):
                nonlocal comment_api_url
                url_lower = response.url.lower()
                if "comment" in url_lower and (
                    "api/sns" in url_lower or "/comment/" in url_lower
                ):
                    comment_api_url = response.url
                    self._track_json_response(response, pending_responses, api_data_buf)

            try:
                browser = await p.chromium.launch(headless=True, args=self.BROWSER_ARGS)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )

                page = await context.new_page()
                page.on("response", on_response)

                # 加载已保存的 cookie，避免被反爬拦截
                await self._load_cookies(context)

                # 访问笔记页面，使用更宽松的等待策略避免超时
                try:
                    await page.goto(note_url, wait_until="commit", timeout=60000)
                    # "commit" 只等响应头返回，再手动等待关键内容出现
                    await page.wait_for_timeout(3000)
                except Exception as goto_err:
                    raise Exception(
                        f"页面加载超时，可能原因：\n"
                        f"1. 网络不稳定或需要代理\n"
                        f"2. 小红书反爬拦截（建议先点击「登录」扫码登录后再爬取）\n"
                        f"3. 笔记链接已失效或需要登录才能查看\n"
                        f"原始错误: {goto_err}"
                    )

                try:
                    for text in ("评论", "展开评论", "查看评论"):
                        btn = page.locator(f"text={text}").first
                        if await btn.is_visible(timeout=1500):
                            await btn.click()
                            await page.wait_for_timeout(1500)
                            break
                except Exception:
                    pass

                await self._drain_tasks(pending_responses)

                if progress_callback:
                    progress_callback(15)

                # ---- 滚动页面触发评论加载 ----
                last_comment_count = 0
                i = 0
                while page_limit is None or i < page_limit:
                    # 滚动到评论区（通常靠下）
                    await page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight * 0.6)"
                    )
                    await page.wait_for_timeout(1500)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)
                    await self._drain_tasks(pending_responses)

                    # 尝试点击"展开更多评论"
                    try:
                        btns = page.locator("text=展开更多")
                        cnt = await btns.count()
                        for j in range(cnt):
                            btn = btns.nth(j)
                            if await btn.is_visible():
                                await btn.click()
                                await page.wait_for_timeout(1500)
                    except Exception:
                        pass

                    # 解析缓冲区中的 API 数据
                    current_comment_count = len(comments)
                    for data in list(api_data_buf):
                        for c in self._parse_api_data(data):
                            key = c.content[:80]
                            if key not in seen_contents:
                                seen_contents.add(key)
                                comments.append(c)

                    api_data_buf.clear()

                    if progress_callback:
                        if page_limit:
                            progress_callback(15 + int(75 * (i + 1) / page_limit))
                        else:
                            progress_callback(min(90, 15 + i * 4))

                    # 如果本轮没新数据，可能已到底
                    if len(comments) == current_comment_count:
                        await page.wait_for_timeout(2000)
                        # 再试一次
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(2000)
                        await self._drain_tasks(pending_responses)
                        retry_count = len(comments)
                        for data in list(api_data_buf):
                            for c in self._parse_api_data(data):
                                key = c.content[:80]
                                if key not in seen_contents:
                                    seen_contents.add(key)
                                    comments.append(c)
                        api_data_buf.clear()
                        if len(comments) == retry_count:
                            break

                    last_comment_count = len(comments)
                    i += 1
                    await asyncio.sleep(1)

                if comment_api_url and comments:
                    await self._attach_full_replies(page, comment_api_url, comments)

                if not comments and await self._looks_like_login_required(page):
                    raise Exception("小红书笔记评论当前不支持匿名访问，或页面接口已变更。")

            except Exception as e:
                raise Exception(f"小红书爬取出错: {e}")
            finally:
                if page is not None:
                    with contextlib.suppress(Exception):
                        page.remove_listener("response", on_response)
                await self._drain_tasks(pending_responses)
                await self._close_quietly(page=page, context=context, browser=browser)

        if progress_callback:
            progress_callback(100)
        return comments

    def _parse_api_data(self, data: dict) -> List[Comment]:
        """解析小红书评论 API 返回的 JSON 数据"""
        result: List[Comment] = []
        comments_list = self._parse_comment_items(data)
        for item in comments_list:
            comment = self._parse_comment_item(item)
            if comment.content:
                result.append(comment)
        return result
