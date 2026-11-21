"""业务路由汇总：分片预检等专用路由先注册，通用资源路由兜底动态模块路径。"""
from __future__ import annotations

from app.routers import import_batch as import_batch_router
from app.routers import resource

ROUTERS = [import_batch_router, resource]
