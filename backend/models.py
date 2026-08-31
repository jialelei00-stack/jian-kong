"""请求/响应数据模型。"""
from typing import Optional, List, Any
from pydantic import BaseModel


class ChannelIn(BaseModel):
    name: str
    wechat_id: Optional[str] = None
    region: Optional[str] = None       # 自定义分类：区域
    owner: Optional[str] = None        # 自定义分类：账号归属者


class ChannelCategoryIn(BaseModel):
    region: Optional[str] = None
    owner: Optional[str] = None


class RuleIn(BaseModel):
    name: str
    category: str = "通用"
    rule_type: str = "keyword"  # keyword / regex
    pattern: str
    severity: str = "medium"     # high / medium / low
    description: Optional[str] = None
    enabled: bool = True


class ContentIn(BaseModel):
    channel_id: int
    title: Optional[str] = None
    video_url: Optional[str] = None
    caption: Optional[str] = None
    transcript: Optional[str] = None
    ocr_text: Optional[str] = None
    publish_time: Optional[str] = None


class ModelConfigIn(BaseModel):
    enabled: bool = False
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None  # 留空表示沿用已保存的 Key


class MatchedRule(BaseModel):
    rule_id: int
    rule_name: str
    category: str
    severity: str
    matched_text: str
    field: str  # caption / transcript / ocr_text


class AnalysisResult(BaseModel):
    content_id: int
    status: str
    risk_level: str
    matched_rules: List[MatchedRule]
    summary: str


# ── 用户系统模型 ──

class RegisterIn(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None
    region: str  # 大区/细分区域


class LoginIn(BaseModel):
    username: str
    password: str


class GuestIn(BaseModel):
    display_name: str
    region: str


class UserOut(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    role: str
    region: Optional[str] = None


class SubmissionReviewIn(BaseModel):
    action: str  # approve / reject
    comment: Optional[str] = None


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str
