from qa_models import QAClient, build_messages


def test_multimodal_history_keeps_previous_and_added_images():
    messages = build_messages([{"role": "user", "text": "题目", "images": ["first"]},
                               {"role": "assistant", "text": "回答", "images": []},
                               {"role": "user", "text": "补充", "images": ["second"]}], lambda k: k.encode())
    assert len(messages) == 3
    assert messages[0]["content"][1]["image_url"]["url"].endswith("Zmlyc3Q=")
    assert messages[2]["content"][1]["image_url"]["url"].endswith("c2Vjb25k")


def test_openai_stream_preserves_full_text_and_context():
    class Response:
        status_code = 200
        headers = {}  # noqa: RUF012 -- 测试响应只读。

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_lines(self, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"公式"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"$x=2$"}}]}'
            yield 'data: [DONE]'

    class Session:
        def post(self, url, **kwargs):
            assert kwargs["json"]["messages"][0]["content"] == "答疑人设"
            assert kwargs["json"]["messages"][1]["content"] == "题目"
            return Response()

    client = QAClient({"base_url": "http://127.0.0.1:1234/v1", "model": "vision"}, "答疑人设")
    client.model.session = Session()
    chunks = []
    assert client.chat([{"role": "user", "content": "题目"}], chunks.append) == "公式$x=2$"
    assert chunks == ["公式", "$x=2$"]
