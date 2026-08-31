"""微信视频号 Appium 自动化采集脚本（需真机/模拟器环境）。

================== 运行前提（必读） ==================
1. 一台 Android 真机或模拟器，已安装并登录微信（建议使用小号，存在封号风险）。
2. 本机已安装并配置：
     - Android SDK / adb（adb devices 能看到设备）
     - Appium Server 2.x，并安装 uiautomator2 driver:
         npm i -g appium
         appium driver install uiautomator2
         appium            # 启动服务，默认 http://127.0.0.1:4723
     - pip install Appium-Python-Client
3. （可选，启用内容提取）安装 OCR / 语音转写依赖：
     pip install paddlepaddle paddleocr      # 画面文字
     pip install openai-whisper              # 语音转写

================== 重要说明 ==================
- 微信视频号内容大量为自绘渲染（SurfaceView/Canvas），Appium 通常无法直接读取
  文本节点，因此文案/字幕主要通过【截图 + OCR】、语音通过【录屏取音轨 + ASR】获取。
- 下面代码中的元素定位（resource-id / xpath / 坐标）会随微信版本变化，
  必须用 Appium Inspector 按你设备上的实际界面进行调整，标记为 TODO 处尤其注意。
- 本脚本提供完整采集骨架；将其返回结果交给规则引擎即可完成合规分析。
"""
import os
import time
import tempfile
from typing import List, Dict, Any

from . import extractor


class AppiumChannelCollector:
    def __init__(
        self,
        server_url: str = "http://127.0.0.1:4723",
        device_name: str = "Android Device",
        wechat_pkg: str = "com.tencent.mm",
        screenshot_dir: str = None,
    ):
        self.server_url = server_url
        self.device_name = device_name
        self.wechat_pkg = wechat_pkg
        self.screenshot_dir = screenshot_dir or tempfile.mkdtemp(prefix="jiankong_")
        self.driver = None

    def connect(self):
        """建立与 Appium Server 的会话并启动微信。"""
        from appium import webdriver  # type: ignore
        from appium.options.android import UiAutomator2Options  # type: ignore

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.device_name = self.device_name
        options.app_package = self.wechat_pkg
        options.app_activity = ".ui.LauncherUI"  # 微信主入口 Activity
        options.no_reset = True   # 不清除微信登录态
        options.new_command_timeout = 300
        self.driver = webdriver.Remote(self.server_url, options=options)
        time.sleep(5)
        return self

    # ---------- 导航 ----------
    def _open_channels_search(self, channel_name: str):
        """进入【发现 → 视频号 → 搜索】并搜索目标视频号。

        TODO: 以下定位需用 Appium Inspector 按实际界面替换。
        """
        from appium.webdriver.common.appiumby import AppiumBy  # type: ignore

        # 1. 底部「发现」Tab —— 通常用文本定位
        self.driver.find_element(AppiumBy.XPATH, '//*[@text="发现"]').click()
        time.sleep(1)
        # 2. 「视频号」入口
        self.driver.find_element(AppiumBy.XPATH, '//*[@text="视频号"]').click()
        time.sleep(2)
        # 3. 右上角搜索图标（TODO: 替换为实际 resource-id 或坐标）
        self.driver.find_element(
            AppiumBy.XPATH, '//*[@content-desc="搜索"]'
        ).click()
        time.sleep(1)
        # 4. 输入视频号名称并搜索
        search_box = self.driver.find_element(
            AppiumBy.CLASS_NAME, "android.widget.EditText"
        )
        search_box.send_keys(channel_name)
        self.driver.press_keycode(66)  # 回车
        time.sleep(2)
        # 5. 点击搜索结果中的目标视频号，进入其主页（TODO: 精确匹配名称）
        self.driver.find_element(
            AppiumBy.XPATH, f'//*[contains(@text, "{channel_name}")]'
        ).click()
        time.sleep(2)

    def _extract_current_video(self, index: int) -> Dict[str, Any]:
        """对当前正在播放/展示的视频提取文案、画面文字、语音。"""
        # 截图用于画面 OCR
        shot_path = os.path.join(self.screenshot_dir, f"frame_{index}.png")
        self.driver.save_screenshot(shot_path)
        ocr_text = extractor.ocr_image(shot_path)

        # 文案：部分文案是可读文本节点，优先尝试读取；失败则并入 OCR 结果
        caption = ""
        try:
            from appium.webdriver.common.appiumby import AppiumBy  # type: ignore

            # TODO: 替换为文案区域的实际 resource-id
            els = self.driver.find_elements(
                AppiumBy.ID, f"{self.wechat_pkg}:id/caption_text"
            )
            caption = " ".join(e.text for e in els if e.text)
        except Exception:
            pass

        # 语音转写：需先通过录屏/录音得到音频文件，再转写
        # 录屏(self.driver.start_recording_screen / stop_recording_screen) 得到视频，
        # 用 ffmpeg 抽音轨后调用 extractor.transcribe_audio(audio_path)
        transcript = ""  # TODO: 接入录屏->抽音轨->转写

        return {
            "title": caption[:30] if caption else f"视频_{index}",
            "video_url": "",
            "caption": caption,
            "transcript": transcript,
            "ocr_text": ocr_text,
            "publish_time": "",
        }

    def collect_channel(self, channel_name: str, max_videos: int = 5) -> List[Dict[str, Any]]:
        """采集指定视频号最近 max_videos 条视频内容。"""
        from appium.webdriver.common.appiumby import AppiumBy  # type: ignore

        self._open_channels_search(channel_name)

        results: List[Dict[str, Any]] = []
        # 点进第一条视频，随后通过上滑切换下一条
        try:
            self.driver.find_element(
                AppiumBy.XPATH, "(//android.widget.ImageView)[1]"
            ).click()  # TODO: 第一条视频缩略图
            time.sleep(2)
        except Exception:
            return results

        size = self.driver.get_window_size()
        for i in range(max_videos):
            results.append(self._extract_current_video(i + 1))
            time.sleep(2)
            # 上滑切换到下一条视频
            self.driver.swipe(
                size["width"] // 2,
                int(size["height"] * 0.8),
                size["width"] // 2,
                int(size["height"] * 0.2),
                500,
            )
            time.sleep(2)
        return results

    def quit(self):
        if self.driver:
            self.driver.quit()
            self.driver = None


def collect_channel(channel_name: str, max_videos: int = 5) -> List[Dict[str, Any]]:
    """供后端调用的入口：完成一次真机采集并返回内容列表。"""
    collector = AppiumChannelCollector()
    try:
        collector.connect()
        return collector.collect_channel(channel_name, max_videos)
    finally:
        collector.quit()


def check_environment() -> bool:
    """连接自检：验证 Appium 能连上设备并唤起微信。

    在调试元素定位之前，先跑这个确认环境是否就绪。
    """
    collector = AppiumChannelCollector()
    try:
        collector.connect()
        size = collector.driver.get_window_size()
        try:
            act = collector.driver.current_activity
        except Exception:
            act = "(无法读取)"
        print("✅ 已连接设备并成功启动微信")
        print(f"   屏幕尺寸: {size}")
        print(f"   当前 Activity: {act}")
        print("   下一步：用 Appium Inspector 核对本文件中标 TODO 的元素定位（视频号 UI 随版本变化）。")
        return True
    except ImportError:
        print("❌ 未安装 Appium-Python-Client：请执行  pip install Appium-Python-Client")
        return False
    except Exception as e:  # noqa
        print(f"❌ 连接失败：{e}")
        print("   排查清单：")
        print("   1) adb devices  是否能看到你的设备/模拟器")
        print("   2) Appium Server 是否在 http://127.0.0.1:4723 运行")
        print("   3) 设备上的微信是否已登录")
        print("   4) 是否已安装驱动： appium driver install uiautomator2")
        return False
    finally:
        collector.quit()


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] in ("check", "--check"):
        check_environment()
    else:
        name = args[0] if args else "测试视频号"
        for item in collect_channel(name, 3):
            print(item)

