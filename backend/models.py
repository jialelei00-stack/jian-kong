"""请求/响应数据模型。"""
from typing import Optional, List, Any, Dict
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


# ── 双百战役模型 ──

class CampaignWorkIn(BaseModel):
    """首次上传建档。"""
    creator_id: Optional[int] = None
    creator_name: Optional[str] = None
    track: str
    title: Optional[str] = None
    url: Optional[str] = None
    media: Optional[Any] = None
    region: Optional[str] = None
    extra_flags: Optional[Dict[str, bool]] = None


class CampaignMetricsIn(BaseModel):
    """T+7 二次提交指标。"""
    plays: Optional[int] = None
    retention_3s: Optional[float] = None
    completion_rate: Optional[float] = None
    comment_count: Optional[int] = None   # 评论个数
    like_count: Optional[int] = None      # 爱心个数
    share_count: Optional[int] = None  # 转发次数（直接按次数套档位；转发率只作展示）
    screenshot: Optional[Any] = None
    business_proof: Optional[Any] = None
    original: Optional[bool] = True  # 原创/有效二创声明
    url: Optional[str] = None        # 作品链接（回传时必填，会回写到作品上）
    ocr_raw: Optional[Any] = None    # AI 从后台截图识别出的原始六项数值（留痕用）


class CampaignCredentialReviewIn(BaseModel):
    """有效线索库：单条凭证的人工审核。"""
    action: str                       # approve（合格）/ reject（不合格）
    note: Optional[str] = None


class CampaignMeritIn(BaseModel):
    """人工审核追加战功（在不改变系统评分的前提下调整作品战功）。"""
    delta: Optional[float] = None     # 追加/扣减的分值，正负均可
    reason: Optional[str] = None


class CampaignScoreOverrideIn(BaseModel):
    """人工复批改分。"""
    total_score: Optional[float] = None
    ai_content_score: Optional[float] = None
    comment: Optional[str] = None


class CampaignCredentialIn(BaseModel):
    type: str
    file: Optional[str] = None
    note: Optional[str] = None


class BrandRefLearnIn(BaseModel):
    """品牌参考学习：传一张图（base64 或 URL）+ 品牌名 + 可选备注。

    后端先调多模态模型对图生成"字形指纹 + 包装布局"描述，写到 brand_ref_descriptions，
    audit() 调模型时把描述写进 system prompt（而不是每次都附图，省 token）。
    """
    brand: str
    image: str  # base64 data URL（data:image/png;base64,xxx）或 HTTP URL
    note: Optional[str] = None


class CampaignPropagationIn(BaseModel):
    account: Optional[str] = None
    url: Optional[str] = None
    type: Optional[str] = None
    note: Optional[str] = None


class CampaignAuditIn(BaseModel):
    action: str  # sample / clear / disqualify
    result: Optional[str] = None
    note: Optional[str] = None
