"""采集模块说明（重要）。

微信视频号是封闭生态，没有公开可爬取的接口，内容只能在微信 App 内
通过加密私有协议访问。本系统**不会编造/虚构任何视频内容**。

真实内容只能通过以下方式进入系统：
  1) 真机 Appium 自动化采集 —— 见 appium_collector.py（需用户在本地配置
     已登录微信的安卓真机/模拟器、Appium Server、ADB）；
  2) 手动录入真实内容 —— 通过 API POST /api/contents 把某条视频的真实
     标题/文案/字幕/语音转写文本提交进来，再由规则引擎审核。

本模块不再返回任何模拟/虚构内容；若被调用，仅返回空结果并提示，
以保证看板上不会出现非真实数据。
"""
from typing import List, Dict, Any


def collect_channel(channel_name: str, max_videos: int = 5) -> List[Dict[str, Any]]:
    print(
        f"[collector] 未接入真实数据源，无法采集『{channel_name}』的真实内容。"
        f"请使用真机 Appium 采集，或通过 POST /api/contents 录入真实内容。"
    )
    return []
