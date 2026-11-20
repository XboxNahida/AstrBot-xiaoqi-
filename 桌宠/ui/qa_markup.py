"""答疑Markdown安全格式化，不依赖Qt或网络。"""
import html
import re

import markdown


def format_markdown(text):
    formulas = []
    def hold(match):
        formulas.append(html.escape(match[0]))
        return f"MATHPLACEHOLDER{len(formulas)-1}END"
    protected = re.sub(r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|(?<!\$)\$[^$\n]+\$", hold, text)
    output = markdown.markdown(html.escape(protected), extensions=["fenced_code", "tables", "nl2br"])
    for index, formula in enumerate(formulas):
        output = output.replace(f"MATHPLACEHOLDER{index}END", formula)
    return output
