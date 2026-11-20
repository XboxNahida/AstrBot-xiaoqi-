import time

from companion_link import CommandJournal


def test_offline_qa_records_do_not_lose_first_message_at_1000(tmp_path):
    journal = CommandJournal(tmp_path / 'state.db')
    for i in range(1001):
        journal.queue_report({'id': str(i), 'observed_at': time.time(), 'facts': {'_record_type': 'qa_message', 'text': '题目'}})
    assert journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1001
    assert journal.reports()[0]['id'] == '0'
    journal.close()
