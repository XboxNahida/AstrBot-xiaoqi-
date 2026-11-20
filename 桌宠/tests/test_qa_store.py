from qa_store import ConversationStore


def test_history_survives_new_chat_restart_and_deletes_attachments(tmp_path):
    store = ConversationStore(tmp_path)
    first = store.create({"persona": "local:test", "profile": "model"})
    store.append(first, "user", "求解这个公式", [b"synthetic-jpeg"])
    store.append(first, "assistant", "$x=2$")
    second = store.create({"persona": "cloud:one"})
    assert len(store.conversations()) == 2
    image_id = store.messages(first)[0]["images"][0]
    store.close()
    store = ConversationStore(tmp_path)
    assert store.image(image_id) == b"synthetic-jpeg"
    assert store.messages(first)[1]["text"] == "$x=2$"
    assert store.settings(first)["persona"] == "local:test"
    store.delete(first)
    assert store.messages(first) == []
    assert [row["id"] for row in store.conversations()] == [second]
    assert store.db.execute("SELECT count(*) FROM images").fetchone()[0] == 0
    store.close()
