"""i18n 语言包必须打进 Docker 镜像（fork 修复）。

    python tests/_test_i18n_image.py

背景：v1.7.10 的 /i18n/manifest 是**扫描 <app>/i18n/*.json** 生成清单的。
上游 Dockerfile 的 COPY 列表里没有 `i18n`，于是仓库里有语言包、镜像里没有，
容器部署的面板永远只有中文（`{"default":"zh","packs":[]}`）。
本套件钉住「Dockerfile 必须 COPY i18n」「仓库里确实有 en.json」，防止再退化。
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


def main():
    print("[i18n-image] 语言包随镜像分发")
    dockerfile = io.open(os.path.join(ROOT, "Dockerfile"),
                         encoding="utf-8").read()
    check("Dockerfile 有 COPY i18n", re.search(r"^COPY\s+i18n\b", dockerfile,
                                               re.M) is not None)
    # 必须发生在 WORKDIR /app 之后，否则会落到 builder 阶段或根目录
    app_pos = dockerfile.index("WORKDIR /app")
    check("COPY i18n 在 WORKDIR /app 之后",
          dockerfile.index("COPY i18n") > app_pos)

    i18n_dir = os.path.join(ROOT, "i18n")
    check("仓库里有 i18n 目录", os.path.isdir(i18n_dir))
    packs = [n for n in os.listdir(i18n_dir) if n.endswith(".json")]
    check("至少有一个语言包", bool(packs), packs)
    check("有 en.json", "en.json" in packs, packs)

    with io.open(os.path.join(i18n_dir, "en.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    check("en.json 有 @locale 元数据", isinstance(data.get("@locale"), dict),
          type(data.get("@locale")).__name__)
    check("语言包里有成对的原文/译文", bool(data.get("messages")), list(data)[:5])

    src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("i18n 目录按代码所在目录解析（镜像里是 /app/i18n）",
          'I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")'
          in src)
    check("/i18n/manifest 扫描目录而不是硬编码清单",
          "os.listdir(I18N_DIR)" in src)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
