"""API 路由模块。

每个子模块独立管理一组 API 端点，通过 FastAPI APIRouter 注册。
main.py 只负责中间件、静态文件和应用启动。
"""
from .auth import router as auth_router
from .submissions import router as submissions_router
from .admin import router as admin_router, usage_router
from .notifications import router as notifications_router
