"""B站评论爬虫 — 基于公开 API，无需浏览器"""
import asyncio
import contextlib
import functools
import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable

import httpx

from .base import BaseCrawler, Comment, CommentImage, WorkInfo


class BilibiliCrawler(BaseCrawler):
    platform_name = "bilibili"
    display_name = "B站 (Bilibili)"
    login_mode = "none"

    COOKIE_PATH = Path(__file__).resolve().parent.parent / "cookies" / "bilibili.json"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    # ---- 登录与Cookie ----
    def _load_cookies(self) -> dict:
        """从文件加载 B站 cookies"""
        if not self.COOKIE_PATH.exists():
            return {}
        try:
            data = json.loads(self.COOKIE_PATH.read_text(encoding="utf-8"))
            cookies = data if isinstance(data, dict) else {}
            # Playwright storage state may contain 'cookies' key
            if "cookies" in cookies:
                cookies = cookies["cookies"]
            return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
        except Exception as e:
            print(f"加载 B站 cookie 失败: {e}")
            return {}

    def _has_login_cookie(self) -> bool:
        cookies = self._load_cookies()
        # B站登录态通常有 SESSDATA 和 bili_jct
        return bool(cookies.get("SESSDATA") or cookies.get("bili_jct"))

    async def login_interactive(self) -> bool:
        """打开浏览器让用户登录 B站，登录完成后关闭窗口即可保存 cookie。"""
        from playwright.async_api import async_playwright

        self.COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
                await page.goto("https://www.bilibili.com", timeout=30000)
                print("\n请在浏览器中登录 B站。登录成功后直接关闭浏览器窗口，程序会自动保存 cookie。")

                deadline = asyncio.get_running_loop().time() + 180
                while asyncio.get_running_loop().time() < deadline:
                    if page.is_closed():
                        break
                    await asyncio.sleep(1)

                await context.storage_state(path=str(self.COOKIE_PATH))
            finally:
                for target in (page, context, browser):
                    if target is None:
                        continue
                    with contextlib.suppress(Exception):
                        await target.close()
        return True

    async def is_logged_in(self) -> bool:
        return self._has_login_cookie()

    def extract_id(self, url: str) -> str:
        """从各种B站URL提取视频ID"""
        patterns = [
            r"bilibili\.com/video/(BV[a-zA-Z0-9]+)",
            r"b23\.tv/([a-zA-Z0-9]+)",
            r"bilibili\.com/video/av(\d+)",
        ]
        for p in patterns:
            m = re.search(p, url)
            if m:
                return m.group(1)
        url = url.strip()
        if re.match(r"^BV[a-zA-Z0-9]{10}$", url):
            return url
        if re.match(r"^\d+$", url):
            return url
        raise ValueError(f"无法解析 B站链接: {url}")

    # ---- WBI 签名 ----
    _wbi_keys: dict = None  # 缓存

    async def _fetch_wbi_keys(self, client: httpx.AsyncClient) -> dict:
        if self._wbi_keys:
            return self._wbi_keys
        resp = await client.get("https://api.bilibili.com/x/web-interface/nav")
        data = resp.json().get("data", {}) or {}
        wbi_img = data.get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        if not img_url or not sub_url:
            raise Exception("B站 wbi 密钥获取失败，可能是风控限制")

        def _extract_key(u: str) -> str:
            return u.rsplit("/", 1)[-1].split(".")[0]

        img_key = _extract_key(img_url)
        sub_key = _extract_key(sub_url)
        mixin_key = img_key + sub_key  # 64 chars total

        # 重排映射
        mapping = [
            46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
            27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
            37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
            22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
        ]
        mixed = "".join(mixin_key[i] for i in mapping if i < len(mixin_key))
        mixed = mixed[:32]  # 取前 32 位作为最终签名密钥

        self._wbi_keys = {"img_key": img_key, "sub_key": sub_key, "mixed": mixed}
        return self._wbi_keys

    def _sign_wbi(self, params: dict, mixed: str) -> dict:
        """对参数做 wbi 签名"""
        params["wts"] = int(time.time())
        # 按键排序
        sorted_items = sorted(params.items(), key=lambda x: x[0])
        query = urllib.parse.urlencode(sorted_items)
        sign = hashlib.md5((query + mixed).encode()).hexdigest()
        params["w_rid"] = sign
        return params

    # ---- 提取用户ID ----
    def _extract_uid(self, user_input: str) -> str:
        """从用户主页链接或纯 UID 中提取"""
        m = re.search(r"space\.bilibili\.com/(\d+)", user_input)
        if m:
            return m.group(1)
        user_input = user_input.strip().rstrip("/")
        # 可能是纯数字 UID
        if re.match(r"^\d+$", user_input):
            return user_input
        # B站短链等
        m = re.search(r"b23\.tv/", user_input)
        raise ValueError("请输入 B站用户主页链接（如 https://space.bilibili.com/123456）或纯数字 UID")

    async def _fetch_fingerprint_cookies(self, client: httpx.AsyncClient) -> None:
        """访问 B站首页获取 buvid3 / b_nut 等指纹 Cookie"""
        try:
            await client.get("https://www.bilibili.com/", follow_redirects=True)
        except Exception:
            pass

    async def _get_user_works_api(
        self,
        uid: str,
        max_works: Optional[int],
        progress_callback: Optional[Callable] = None,
    ) -> List[WorkInfo]:
        """基于 WBI 签名 API 获取用户作品（无需登录，部分账号可能风控）"""
        works: List[WorkInfo] = []
        page = 1
        page_size = 30
        limit = max_works if max_works and max_works > 0 else None
        async with httpx.AsyncClient(
            headers=self.HEADERS, timeout=20
        ) as client:
            # 先拿指纹 cookie，与 WBI 密钥同一 session
            await self._fetch_fingerprint_cookies(client)
            wbi = await self._fetch_wbi_keys(client)
            mixed = wbi["mixed"]
            total = 0

            while limit is None or len(works) < limit:
                last_error = None
                for attempt in range(3):
                    params = self._sign_wbi(
                        {"mid": uid, "ps": str(page_size), "pn": str(page), "order": "pubdate"},
                        mixed,
                    )
                    resp = await client.get(
                        "https://api.bilibili.com/x/space/wbi/arc/search",
                        params=params,
                    )
                    # 某些情况下风控会返回 HTML 页面
                    content_type = resp.headers.get("content-type", "")
                    if "application/json" not in content_type:
                        last_error = Exception("B站 返回非 JSON 响应，疑似风控拦截")
                        await asyncio.sleep(2 ** attempt)
                        continue

                    data = resp.json()
                    if data["code"] == 0:
                        last_error = None
                        break
                    last_error = Exception(f"code={data['code']}: {data.get('message', '未知错误')}")
                    # -352 风控：先等一下重试
                    if data.get("code") == -352:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    # 其他错误不重试
                    break

                if last_error:
                    raise last_error

                data = resp.json()
                vlist = data["data"].get("list", {}).get("vlist", [])
                if not vlist:
                    break

                for v in vlist:
                    works.append(WorkInfo(
                        work_id=v["bvid"],
                        title=v["title"],
                        url=f'https://www.bilibili.com/video/{v["bvid"]}',
                        cover=v.get("pic", ""),
                        stats={
                            "play": v.get("play", 0),
                            "comment": v.get("comment", 0),
                            "created": v.get("created", 0),
                        },
                    ))
                    if limit is not None and len(works) >= limit:
                        break

                if progress_callback:
                    if total > 0:
                        progress_callback(min(90, int(10 + 80 * len(works) / total)))
                    elif limit:
                        progress_callback(min(90, int(10 + 80 * len(works) / limit)))
                    else:
                        progress_callback(min(90, 10 + page * 2))

                total = data["data"].get("page", {}).get("count", 0)
                if page * page_size >= total:
                    break
                page += 1
                await asyncio.sleep(0.5)

        return works

    async def _get_user_works_playwright(
        self,
        uid: str,
        max_works: Optional[int],
        progress_callback: Optional[Callable] = None,
    ) -> List[WorkInfo]:
        """
        使用浏览器访问用户主页并拦截作品列表 API。
        需要已登录 cookie，浏览器环境更容易通过风控。
        """
        from playwright.async_api import async_playwright

        profile_url = f"https://space.bilibili.com/{uid}"
        works: List[WorkInfo] = []
        seen_bvids = set()
        api_data_buf: list = []
        limit = max_works if max_works and max_works > 0 else None

        if progress_callback:
            progress_callback(10)

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

            # 注入已保存的 cookies
            page = await context.new_page()

            async def on_response(response):
                if "x/space/wbi/arc/search" in response.url:
                    try:
                        body = await response.json()
                        api_data_buf.append(body)
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # 滚动触发分页加载
                idle_rounds = 0
                loop_count = max((limit or 300) // 10, 3)
                for _ in range(loop_count):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2500)

                    before_count = len(works)
                    for data in list(api_data_buf):
                        vlist = data.get("data", {}).get("list", {}).get("vlist", []) if data.get("code") == 0 else []
                        for v in vlist:
                            bvid = v.get("bvid", "")
                            if bvid and bvid not in seen_bvids:
                                seen_bvids.add(bvid)
                                works.append(WorkInfo(
                                    work_id=bvid,
                                    title=v.get("title", ""),
                                    url=f'https://www.bilibili.com/video/{bvid}',
                                    cover=v.get("pic", ""),
                                    stats={
                                        "play": v.get("play", 0),
                                        "comment": v.get("comment", 0),
                                        "created": v.get("created", 0),
                                    },
                                ))
                        api_data_buf.clear()

                    if len(works) == before_count:
                        idle_rounds += 1
                    else:
                        idle_rounds = 0

                    if progress_callback:
                        if limit:
                            pct = min(90, 10 + int(80 * len(works) / limit))
                        else:
                            pct = min(90, 10 + _ * 3)
                        progress_callback(pct)

                    if limit is not None and len(works) >= limit:
                        break

                    if limit is None and idle_rounds >= 3:
                        break

                    await asyncio.sleep(1)
            except Exception as e:
                raise Exception(f"浏览器获取 B站作品列表失败: {e}")
            finally:
                await browser.close()

        if progress_callback:
            progress_callback(100)
        return works if limit is None else works[:limit]

    # ---- 获取用户作品列表 ----
    async def get_user_works(
        self,
        user_input: str,
        max_works: Optional[int] = 30,
        progress_callback: Optional[Callable] = None,
    ) -> List[WorkInfo]:
        uid = self._extract_uid(user_input)
        try:
            # 优先走 API（快），若受限则走匿名浏览器兜底
            return await self._get_user_works_api(uid, max_works, progress_callback)
        except Exception as api_err:
            err_msg = str(api_err)
            if "-352" in err_msg or "风控" in err_msg or "非 JSON" in err_msg:
                print("[B站] API 被风控拦截，尝试匿名浏览器模式兜底。")
                if progress_callback:
                    progress_callback(0)
                try:
                    return await self._get_user_works_playwright(uid, max_works, progress_callback)
                except Exception as browser_err:
                    raise Exception(
                        "B站账号级爬取被风控拦截，匿名浏览器兜底也未成功。"
                    ) from browser_err
            raise

    # ---- 视频信息 ----
    async def _get_video_info(self, identifier: str) -> dict:
        """获取视频基本信息，返回 aid + 标题 + 评论数"""
        async with httpx.AsyncClient(headers=self.HEADERS, timeout=15) as client:
            if identifier.startswith("BV"):
                api = f"https://api.bilibili.com/x/web-interface/view?bvid={identifier}"
            else:
                api = f"https://api.bilibili.com/x/web-interface/view?aid={identifier}"
            resp = await client.get(api)
            data = resp.json()
            if data["code"] != 0:
                raise Exception(f"获取视频信息失败: {data.get('message', '未知错误')}")
            return data["data"]

    async def _fetch_sub_replies(
        self, client: httpx.AsyncClient, oid: int, root_rpid: int, max_pages: int = 3
    ) -> List[Comment]:
        """获取某条评论的全部子回复"""
        subs = []
        for pn in range(1, max_pages + 1):
            url = (
                f"https://api.bilibili.com/x/v2/reply/reply"
                f"?type=1&oid={oid}&root={root_rpid}&pn={pn}&ps=20"
            )
            resp = await client.get(url)
            data = resp.json()
            if data["code"] != 0:
                break
            replies = data["data"].get("replies") or []
            if not replies:
                break
            for sub in replies:
                if not sub.get("member"):
                    continue
                subs.append(Comment(
                    username=sub["member"]["uname"],
                    content=sub.get("content", {}).get("message", ""),
                    time=datetime.fromtimestamp(sub["ctime"]).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    likes=sub.get("like", 0),
                ))
            page_info = data["data"].get("page", {})
            total = page_info.get("count", 0)
            seen = page_info.get("num", 0) * page_info.get("size", 20)
            if seen >= total:
                break
            await asyncio.sleep(0.4)
        return subs

    async def get_comments(
        self,
        url: str,
        max_pages: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ) -> List[Comment]:
        identifier = self.extract_id(url)
        video_info = await self._get_video_info(identifier)
        aid = video_info["aid"]
        title = video_info.get("title", "")
        total_reply = video_info.get("stat", {}).get("reply", 0)

        comments: List[Comment] = []
        cursor = 0
        page = 0
        page_limit = max_pages if max_pages and max_pages > 0 else None

        if progress_callback:
            progress_callback(5)

        async with httpx.AsyncClient(headers=self.HEADERS, timeout=15) as client:
            while page_limit is None or page < page_limit:
                api = (
                    f"https://api.bilibili.com/x/v2/reply/main"
                    f"?type=1&oid={aid}&mode=3&next={cursor}"
                )
                resp = await client.get(api)
                data = resp.json()

                if data["code"] != 0:
                    break  # 12061 = no more, or other errors

                replies = data["data"].get("replies") or []
                if not replies:
                    break

                for reply in replies:
                    member = reply.get("member", {})
                    content = reply.get("content", {})

                    # 解析图片（评论区表情包/截图）
                    images: List[CommentImage] = []
                    for pic in content.get("pictures") or []:
                        img_url = pic.get("img_src", "")
                        if img_url:
                            # 去掉缩略图后缀拿原图
                            full = re.sub(r"@\d+w_\d+h.*?$", "", img_url)
                            images.append(CommentImage(url=full))

                    # 解析子回复
                    rc = reply.get("rcount", 0)
                    sub_replies: List[Comment] = []
                    if reply.get("replies"):
                        for sub in reply["replies"]:
                            sub_replies.append(Comment(
                                username=sub.get("member", {}).get("uname", ""),
                                content=sub.get("content", {}).get("message", ""),
                                time=datetime.fromtimestamp(sub["ctime"]).strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ) if "ctime" in sub else "",
                                likes=sub.get("like", 0),
                            ))
                    # 如果子回复数量超过自带数量（B站API自带最多3条），额外拉取
                    if rc > len(sub_replies) and rc > 0:
                        try:
                            rpid = reply.get("rpid", 0)
                            sub_replies = await self._fetch_sub_replies(client, aid, rpid)
                        except Exception:
                            pass

                    comments.append(Comment(
                        username=member.get("uname", "未知用户"),
                        content=content.get("message", ""),
                        time=datetime.fromtimestamp(reply["ctime"]).strftime("%Y-%m-%d %H:%M:%S"),
                        likes=reply.get("like", 0),
                        ip_location=reply.get("reply_control", {}).get("location", ""),
                        images=images,
                        replies=sub_replies,
                        reply_count=rc,
                    ))

                # 翻页：cursor 模式
                cursor_info = data["data"].get("cursor", {})
                cursor = cursor_info.get("next", 0)
                if cursor == 0:
                    break

                page += 1
                if progress_callback:
                    if page_limit:
                        progress_callback(5 + int(85 * page / page_limit))
                    else:
                        progress_callback(min(90, 5 + page * 3))
                await asyncio.sleep(0.6)

        if progress_callback:
            progress_callback(100)

        # 注入视频标题到第一个 comment 的额外属性（用于报告展示）
        # 这里我们用 content 上额外记录的方式，在 app 层处理
        self._last_title = title
        self._last_total = total_reply

        return comments
