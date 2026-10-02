from pathlib import Path

import shutil
import sys

assets_dir = Path(__file__).parent.parent.resolve() / "assets"


def configure_ocr_model():
    # 先看目标：已经拷过就直接返回，不必要求子模块存在。
    # 本地重复打包时 assets/resource/model/ocr 是上一次的产物（未被 git 跟踪），
    # 原顺序会在这里误报 File Not Found 而退出。
    ocr_dir = assets_dir / "resource" / "model" / "ocr"
    if ocr_dir.exists():
        print("Found existing OCR directory, skipping default OCR model import.")
        return

    # 只有真的要拷贝时才需要子模块：CI 用 checkout 的 submodules: true，
    # 本地用 git submodule update --init --recursive。
    assets_ocr_dir = assets_dir / "MaaCommonAssets" / "OCR"
    if not assets_ocr_dir.exists():
        print(f"File Not Found: {assets_ocr_dir}")
        print("本地请先执行：git submodule update --init --recursive")
        sys.exit(1)

    shutil.copytree(
        assets_ocr_dir / "ppocr_v6" / "small",
        ocr_dir,
        dirs_exist_ok=True,
    )


if __name__ == "__main__":
    configure_ocr_model()

    print("OCR model configured.")
