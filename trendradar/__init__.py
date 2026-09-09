# coding=utf-8
"""
TrendRadar - 热点新闻聚合与分析工具

使用方式:
  python -m trendradar        # 模块执行
  trendradar                  # 安装后执行
"""

__version__ = "6.10.0"
__all__ = ["AppContext", "__version__"]


def __getattr__(name):
    # Lightweight subpackages (papers/content_pool) do not require the news app's
    # optional AI, notification and crawler dependencies merely to open a DB.
    if name == "AppContext":
        from trendradar.context import AppContext

        return AppContext
    raise AttributeError(name)
