"""把 _style.css + _<page>_body/_extra/_js.html 拼成可直接渲染的 <page>.html。

用法（在插件目录下）：
    python -B webui/_build.py now organs timeline

只改 webui/ 下的碎片文件时需要重跑。
"""
import sys
from pathlib import Path

W = Path(__file__).resolve().parent
css = (W / "_style.css").read_text(encoding="utf-8")


def build(name):
    body_path = W / f"_{name}_body.html"
    js_path = W / f"_{name}_js.html"
    parts = [css]
    if body_path.exists():
        parts.append(body_path.read_text(encoding="utf-8"))
    extra = W / f"_{name}_extra.html"
    if extra.exists():
        parts.append(extra.read_text(encoding="utf-8"))
    if js_path.exists():
        parts.append(js_path.read_text(encoding="utf-8"))
    out = W / f"{name}.html"
    # 每个碎片自带的尾部换行要先削掉再拼，否则接缝处会多出空行。
    # newline="\n" 是关键：Windows 默认把 \n 翻成 \r\n，会让产物悄悄变大。
    out.write_text("\n".join(x.rstrip("\n") for x in parts) + "\n", encoding="utf-8", newline="\n")
    print(name, len(out.read_text(encoding="utf-8")))


for n in sys.argv[1:]:
    build(n)
