"""抖音评论爬虫 — Playwright 浏览器自动化 + API 响应拦截"""
import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from typing import List, Optional, Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .base import BaseCrawler, Comment, CommentImage, WorkInfo

COOKIE_DIR = Path(__file__).parent.parent / "cookies"
COOKIE_FILE = COOKIE_DIR / "douyin.json"


class DouyinCrawler(BaseCrawler):
    platform_name = "douyin"
    login_mode = "none"
    display_name = "抖音"

    def extract_id(self, url: str) -> str:
        """从抖音URL提取视频ID"""
        # https://www.douyin.com/video/7123456789012345678
        # https://v.douyin.com/xxxxx/ (短链)
        for pat in [
            r"douyin\.com/video/(\d+)",
            r"douyin\.com/note/(\d+)",
            r"v\.douyin\.com/([a-zA-Z0-9]+)",
        ]:
            m = re.search(pat, url)
            if m:
                return m.group(1)

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        modal_ids = query.get("modal_id", [])
        if modal_ids:
            modal_id = modal_ids[0].strip()
            if re.fullmatch(r"\d+", modal_id):
                return modal_id

        raise ValueError(f"无法解析抖音链接: {url}")

    async def _load_cookies(self, context):
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        safe_cookies = self._read_safe_cookies()
        if safe_cookies:
            try:
                await context.add_cookies(safe_cookies)
                return True
            except Exception:
                return False
        return False

    def _read_safe_cookies(self) -> List[dict]:
        if not COOKIE_FILE.exists():
            return []
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception:
            return []

        if isinstance(cookies, dict):
            cookies = cookies.get("cookies", [])

        safe_cookies: List[dict] = []
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue

            name = str(cookie.get("name", "")).strip()
            value = str(cookie.get("value", "")).strip()
            domain = str(cookie.get("domain", "")).strip()
            if not name or not value or not domain:
                continue

            safe_cookie = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie.get("path") or "/",
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", False)),
            }

            same_site = cookie.get("sameSite")
            if same_site in {"Strict", "Lax", "None"}:
                safe_cookie["sameSite"] = same_site

            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                safe_cookie["expires"] = float(expires)

            safe_cookies.append(safe_cookie)

        return safe_cookies

    def _has_saved_cookies(self) -> bool:
        return bool(self._read_safe_cookies())

    async def _save_cookies(self, context):
        cookies = await context.cookies()
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "w") as f:
            json.dump(cookies, f, ensure_ascii=False)

    async def _close_quietly(self, page=None, context=None, browser=None):
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
        for key in ("cid", "comment_id", "reply_id", "rpid", "id"):
            value = item.get(key)
            if value:
                return str(value)
        return ""

    def _parse_comment_items(self, data: dict) -> List[dict]:
        return (
            data.get("comments", [])
            or data.get("data", {}).get("comments", [])
            or data.get("reply_comment", [])
            or data.get("data", {}).get("reply_comment", [])
            or data.get("data", {}).get("reply_comments", [])
            or data.get("data", {}).get("items", [])
            or []
        )

    def _parse_comment_item(self, item: dict) -> Comment:
        user = item.get("user", {})
        username = user.get("nickname", user.get("nick_name", ""))

        content = item.get("text", item.get("content", ""))
        create_time = item.get("create_time", 0)
        time_str = str(create_time)
        if isinstance(create_time, (int, float)) and create_time > 0:
            try:
                from datetime import datetime
                time_str = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, OverflowError, ValueError):
                time_str = str(create_time)

        likes_raw = item.get("digg_count", item.get("like_count", 0))
        try:
            likes = int(likes_raw)
        except (ValueError, TypeError):
            likes = 0

        images: List[CommentImage] = []
        for img_info in item.get("image_list", []) or []:
            img_url = ""
            if isinstance(img_info, dict):
                for key in ["url", "origin_url", "url_list"]:
                    val = img_info.get(key)
                    if isinstance(val, list) and val:
                        img_url = val[0]
                        break
                    elif isinstance(val, str) and val:
                        img_url = val
                        break
            elif isinstance(img_info, str):
                img_url = img_info
            if img_url:
                images.append(CommentImage(url=img_url))

        sub_replies: List[Comment] = []
        for sub in item.get("reply_comment", []) or []:
            sub_replies.append(self._parse_comment_item(sub))

        reply_count_raw = item.get("reply_comment_total", len(sub_replies))
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
        parsed = urlparse(api_url)
        base_query = parse_qs(parsed.query)
        base_query["cursor"] = [str(cursor)]

        candidate_paths = [parsed.path]
        if "/comment/list/" in parsed.path:
            candidate_paths.extend([
                parsed.path.replace("/comment/list/", "/comment/reply/"),
                parsed.path.replace("/comment/list/", "/comment/list/reply/"),
                parsed.path.replace("/comment/list/", "/comment/reply/list/"),
            ])

        candidate_param_names = ("comment_id", "cid", "root_comment_id", "reply_id", "parent_comment_id")
        urls: List[str] = []
        for path in candidate_paths:
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
        """递归抓取某条评论下的全部回复，返回扁平列表。"""
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
            await asyncio.sleep(0.2)

    async def _scroll_comment_area(self, page):
        await page.evaluate("""
            const candidates = Array.from(document.querySelectorAll('*')).filter(el => {
                const cls = (el.className || '').toString().toLowerCase();
                return cls.includes('comment') && el.scrollHeight > el.clientHeight + 40;
            });
            const scrollEl = candidates.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
            if (scrollEl) {
                scrollEl.scrollTop = scrollEl.scrollHeight;
            } else {
                window.scrollTo(0, document.body.scrollHeight);
            }
        """)

    async def _fetch_comment_page_via_browser(self, page, api_url: str, cursor: int | str) -> dict:
        parsed = urlparse(api_url)
        query = parse_qs(parsed.query)
        query["cursor"] = [str(cursor)]
        next_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        ))
        try:
            return await page.evaluate("""
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
            return {"comments": [], "cursor": 0, "has_more": 0, "__fetch_error": str(e)}

    async def login_interactive(self) -> bool:
        """打开浏览器让用户扫码登录抖音，登录完成后关闭窗口即可保存 cookie。"""
        from playwright.async_api import async_playwright

        browser = context = page = None
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-gpu"],
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                await page.goto("https://www.douyin.com", timeout=30000)
                print("\n请在浏览器中扫码登录抖音。登录成功后直接关闭浏览器窗口，程序会自动保存 cookie。")

                deadline = asyncio.get_running_loop().time() + 180
                while asyncio.get_running_loop().time() < deadline:
                    if page.is_closed():
                        break
                    await asyncio.sleep(1)

                await self._save_cookies(context)
            finally:
                for target in (page, context, browser):
                    if target is None:
                        continue
                    with contextlib.suppress(Exception):
                        await target.close()
        return True

    # ---- 获取用户作品列表 ----
    def _extract_user_id(self, user_input: str) -> str:
        """从用户主页链接提取 sec_uid"""
        # https://www.douyin.com/user/MS4wLjABAAAAxxx
        m = re.search(r"douyin\.com/user/([a-zA-Z0-9_-]+)", user_input)
        if m:
            return m.group(1)
        user_input = user_input.strip().rstrip("/")
        if re.match(r"^[a-zA-Z0-9_-]{20,}$", user_input):
            return user_input
        raise ValueError("请输入抖音用户主页链接（如 https://www.douyin.com/user/MS4wLjABAAAAxxx）")

    async def get_user_works(
        self,
        user_input: str,
        max_works: Optional[int] = 30,
        progress_callback: Optional[Callable] = None,
    ) -> List[WorkInfo]:
        return await self._get_user_works_once(
            user_input,
            max_works=max_works,
            progress_callback=progress_callback,
            use_saved_cookies=False,
        )

    async def _get_user_works_once(
        self,
        user_input: str,
        max_works: Optional[int] = 30,
        progress_callback: Optional[Callable] = None,
        use_saved_cookies: bool = False,
    ) -> List[WorkInfo]:
        from playwright.async_api import async_playwright

        sec_uid = self._extract_user_id(user_input)
        profile_url = f"https://www.douyin.com/user/{sec_uid}"
        works: List[WorkInfo] = []
        seen_ids = set()
        api_data_buf: list = []
        limit = max_works if max_works and max_works > 0 else None

        if progress_callback:
            progress_callback(5)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            if use_saved_cookies:
                await self._load_cookies(context)
            page = await context.new_page()

            async def on_response(response):
                url_lower = response.url.lower()
                if "aweme/post" in url_lower or "aweme_list" in url_lower:
                    try:
                        body = await response.json()
                        api_data_buf.append(body)
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                if progress_callback:
                    progress_callback(15)

                # 滚动加载更多作品
                idle_rounds = 0
                loop_count = max((limit or 300) // 10, 5)
                for round_idx in range(loop_count):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2500)

                    before_count = len(works)
                    for data in api_data_buf:
                        aweme_list = (data.get("aweme_list", [])
                                      or data.get("data", {}).get("aweme_list", [])
                                      or [])
                        for aweme in aweme_list:
                            aweme_id = aweme.get("aweme_id", "")
                            if aweme_id and aweme_id not in seen_ids:
                                seen_ids.add(aweme_id)
                                works.append(WorkInfo(
                                    work_id=aweme_id,
                                    title=aweme.get("desc", ""),
                                    url=f'https://www.douyin.com/video/{aweme_id}',
                                    cover="",
                                    stats={
                                        "digg": aweme.get("statistics", {}).get("digg_count", 0),
                                        "comment": aweme.get("statistics", {}).get("comment_count", 0),
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

                    await asyncio.sleep(1.5)

                if use_saved_cookies:
                    await self._save_cookies(context)
            except Exception as e:
                raise Exception(f"获取抖音作品列表失败: {e}")
            finally:
                await browser.close()

        if progress_callback:
            progress_callback(100)
        return works if limit is None else works[:limit]

    async def get_comments(
        self,
        url: str,
        max_pages: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ) -> List[Comment]:
        return await self._get_comments_once(
            url,
            max_pages=max_pages,
            progress_callback=progress_callback,
            use_saved_cookies=False,
        )

    async def _get_comments_once(
        self,
        url: str,
        max_pages: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
        use_saved_cookies: bool = False,
    ) -> List[Comment]:
        from playwright.async_api import async_playwright

        video_id = self.extract_id(url)
        video_url = f"https://www.douyin.com/video/{video_id}"
        comments: List[Comment] = []
        seen_contents = set()
        api_data_buf: list = []
        page_limit = max_pages if max_pages and max_pages > 0 else None
        comment_api_url: Optional[str] = None
        self._last_total = 0

        def consume_payloads(payloads: List[dict]) -> tuple[int, int, bool]:
            added = 0
            next_cursor = 0
            has_more = False
            for data in payloads:
                total = int(data.get("total", 0) or 0)
                if total > self._last_total:
                    self._last_total = total
                for c in self._parse_api_data(data):
                    key = c.content[:80]
                    if key not in seen_contents:
                        seen_contents.add(key)
                        comments.append(c)
                        added += 1
                next_cursor = int(data.get("cursor", data.get("data", {}).get("cursor", 0)) or 0)
                raw_has_more = data.get("has_more", data.get("data", {}).get("has_more", 0))
                has_more = str(raw_has_more).lower() not in {"0", "false", "none", ""}
            return added, next_cursor, has_more

        if progress_callback:
            progress_callback(5)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )

            if use_saved_cookies:
                await self._load_cookies(context)
            page = await context.new_page()
            pending_responses = set()

            # ---- 拦截评论相关 API ----
            def on_response(response):
                nonlocal comment_api_url
                url_lower = response.url.lower()
                if "aweme/v1/web/comment/list/" in url_lower:
                    comment_api_url = response.url
                    self._track_json_response(response, pending_responses, api_data_buf)
                elif "comment" in url_lower and (
                    "aweme" in url_lower or "api" in url_lower
                ):
                    self._track_json_response(response, pending_responses, api_data_buf)

            page.on("response", on_response)

            try:
                await page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
                # 抖音页面加载较慢
                await page.wait_for_timeout(5000)
                await self._drain_tasks(pending_responses)

                if progress_callback:
                    progress_callback(15)

                # ---- 先触发首屏评论接口 ----
                for _ in range(6):
                    if api_data_buf:
                        break
                    try:
                        comment_tab = page.locator('text=评论').first
                        if await comment_tab.is_visible(timeout=2000):
                            await comment_tab.click()
                            await page.wait_for_timeout(1500)
                    except Exception:
                        pass

                    # 在评论区内部滚动
                    await self._scroll_comment_area(page)
                    await page.wait_for_timeout(2000)
                    await self._drain_tasks(pending_responses)
                    if api_data_buf:
                        break

                pages_fetched = 0
                next_cursor = 0
                has_more = False

                if api_data_buf:
                    _, next_cursor, has_more = consume_payloads(list(api_data_buf))
                    api_data_buf.clear()
                    pages_fetched = 1

                if comment_api_url:
                    while has_more and (page_limit is None or pages_fetched < page_limit):
                        data = await self._fetch_comment_page_via_browser(page, comment_api_url, next_cursor)
                        _, next_cursor, has_more = consume_payloads([data])
                        pages_fetched += 1

                        if progress_callback:
                            if page_limit:
                                progress_callback(15 + int(75 * pages_fetched / page_limit))
                            else:
                                progress_callback(min(90, 15 + pages_fetched * 3))

                        if not has_more:
                            break

                        await asyncio.sleep(0.8)

                elif api_data_buf:
                    # 未抓到评论列表接口 URL 时，至少返回首屏已拿到的数据
                    pass
                else:
                    # 兜底：如果首屏接口未稳定触发，再尝试一次 DOM 滚动解析
                    last_comment_count = 0
                    idle_rounds = 0
                    i = 0
                    while page_limit is None or i < page_limit:
                        await self._scroll_comment_area(page)
                        await page.wait_for_timeout(2000)
                        await self._drain_tasks(pending_responses)
                        consume_payloads(list(api_data_buf))
                        api_data_buf.clear()

                        if progress_callback:
                            if page_limit:
                                progress_callback(15 + int(75 * (i + 1) / page_limit))
                            else:
                                progress_callback(min(90, 15 + i * 4))

                        if len(comments) == last_comment_count:
                            idle_rounds += 1
                        else:
                            idle_rounds = 0
                        max_idle_rounds = 6 if page_limit is None else (4 if not comments else 2)
                        if idle_rounds >= max_idle_rounds:
                            break

                        last_comment_count = len(comments)
                        i += 1
                        await asyncio.sleep(1.5)

                if comment_api_url and comments:
                    await self._attach_full_replies(page, comment_api_url, comments)

                if use_saved_cookies:
                    await self._save_cookies(context)

            except Exception as e:
                raise Exception(f"抖音爬取出错: {e}")
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
        """解析抖音评论 API 返回的 JSON 数据"""
        result: List[Comment] = []
        comments_list = self._parse_comment_items(data)
        for item in comments_list:
            comment = self._parse_comment_item(item)
            if comment.content:
                result.append(comment)
        return result
