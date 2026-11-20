from ui.qa_markup import format_markdown


def test_math_backslashes_and_html_are_preserved_safely():
    output = format_markdown(r"公式 \(x_1=\frac{1}{2}\) 与 $$y=2$$" + '\n<script>alert(1)</script>')
    assert r"\(x_1=\frac{1}{2}\)" in output
    assert "$$y=2$$" in output
    assert "<script>" not in output
