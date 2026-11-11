"""
快速识别模块

目前只有 ocr
"""

from typing import Optional
from maa.context import Context, JRecognitionType, OCRResult
from maa.pipeline import JOCR
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction

from agentlog import log


def ocr(
    context: Context, argv: CustomRecognition.AnalyzeArg, jocr: JOCR
) -> Optional[OCRResult]:
    # ocr识别
    detail = context.run_recognition_direct(JRecognitionType.OCR, jocr, argv.image)
    if detail is None or not detail.hit:
        log.debug(f"{argv.node_name} OCR失败")
        return None
    best = detail.best_result
    assert type(best) == OCRResult
    return best
