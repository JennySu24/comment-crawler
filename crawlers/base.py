"""爬虫基类与数据模型"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Callable


@dataclass
class CommentImage:
    """评论中的图片"""
    url: str
    local_path: str = ""


@dataclass
class Comment:
    """单条评论"""
    username: str
    content: str
    comment_id: str = ""
    time: str = ""
    likes: int = 0
    ip_location: str = ""
    images: List[CommentImage] = field(default_factory=list)
    replies: List['Comment'] = field(default_factory=list)
    reply_count: int = 0


@dataclass
class WorkInfo:
    """作品信息"""
    work_id: str          # 作品 ID
    title: str = ""        # 标题/描述
    url: str = ""          # 作品链接
    cover: str = ""        # 封面图
    stats: dict = field(default_factory=dict)  # 播放/点赞等统计


class BaseCrawler(ABC):
    """平台爬虫抽象基类"""

    platform_name: str = "unknown"
    display_name: str = "未知平台"

    @abstractmethod
    def extract_id(self, url: str) -> str:
        """从 URL 中提取内容 ID"""
        ...

    @abstractmethod
    async def get_comments(
        self,
        url: str,
        max_pages: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ) -> List[Comment]:
        """获取评论列表"""
        ...

    async def get_user_works(
        self,
        user_input: str,
        max_works: Optional[int] = 30,
        progress_callback: Optional[Callable] = None,
    ) -> List[WorkInfo]:
        """
        获取用户的作品列表。
        user_input 可以是用户主页链接或用户 ID。
        默认返回空列表（子类不支持时）。
        """
        return []

    async def login(self) -> bool:
        """处理登录，默认不需要"""
        return True

    async def is_logged_in(self) -> bool:
        """检查登录状态"""
        return True
