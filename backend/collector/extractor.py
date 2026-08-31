"""内容提取：画面 OCR 与音频语音转写。

这两项依赖体积较大（paddleocr / whisper），因此采用懒加载并支持降级：
未安装对应依赖时返回空字符串并给出提示，不会阻塞主流程。

真机 Appium 采集时，对每条视频：
  - 抽取关键帧图片 -> ocr_image() 得到画面文字
  - 抽取音轨 -> transcribe_audio() 得到语音转写
"""
from typing import Optional

_ocr_engine = None
_whisper_model = None


def ocr_image(image_path: str) -> str:
    """对单张图片做 OCR，返回识别文本。依赖 paddleocr。"""
    global _ocr_engine
    try:
        if _ocr_engine is None:
            from paddleocr import PaddleOCR  # type: ignore

            _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        result = _ocr_engine.ocr(image_path, cls=True)
        lines = []
        for page in result or []:
            for line in page or []:
                lines.append(line[1][0])
        return " ".join(lines)
    except ImportError:
        print("[extractor] 未安装 paddleocr，跳过画面OCR。可执行: pip install paddlepaddle paddleocr")
        return ""
    except Exception as e:  # noqa
        print(f"[extractor] OCR 失败: {e}")
        return ""


def transcribe_audio(audio_path: str, model_size: str = "base") -> str:
    """对音频做语音转写，返回文本。依赖 openai-whisper。"""
    global _whisper_model
    try:
        if _whisper_model is None:
            import whisper  # type: ignore

            _whisper_model = whisper.load_model(model_size)
        result = _whisper_model.transcribe(audio_path, language="zh")
        return (result or {}).get("text", "")
    except ImportError:
        print("[extractor] 未安装 whisper，跳过语音转写。可执行: pip install openai-whisper")
        return ""
    except Exception as e:  # noqa
        print(f"[extractor] 语音转写失败: {e}")
        return ""
