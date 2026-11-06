from types import SimpleNamespace

from exchangelib.properties import Body, HTMLBody

from exchange_ews_bridge.app import sender_value
from exchange_ews_bridge.mail_reads import complete_body_text, mail_fields


def test_native_from_is_requested_and_used_when_sender_address_is_absent():
    assert "author" in mail_fields(True)
    item = SimpleNamespace(
        sender=SimpleNamespace(name="Transport account", email_address=None),
        author=SimpleNamespace(name="Source author", email_address="author@example.com"),
    )
    assert sender_value(item, "name") == "Source author"
    assert sender_value(item, "email_address") == "author@example.com"


def test_explicit_sender_keeps_priority_over_a_different_from_mailbox():
    item = SimpleNamespace(
        sender=SimpleNamespace(name="Sender", email_address="sender@example.com"),
        author=SimpleNamespace(name="Author", email_address="author@example.com"),
    )
    assert sender_value(item, "name") == "Sender"
    assert sender_value(item, "email_address") == "sender@example.com"


def test_missing_source_addresses_are_not_inferred_from_a_name():
    item = SimpleNamespace(sender=SimpleNamespace(name="Known name", email_address=None))
    assert sender_value(item, "email_address") is None


def test_replacement_character_can_be_recovered_from_native_html_without_guessing():
    item = SimpleNamespace(
        text_body=Body("Лимит: 50\ufffd рублей. Доля 100%."),
        body=HTMLBody("<html><body>Лимит: 500 рублей. Доля 100%.</body></html>"),
    )
    text, fmt, source = complete_body_text(item)
    assert "500 рублей" in text and "100%" in text
    assert fmt == "html" and source == "html_body_replacement_fallback"


def test_alternate_html_must_preserve_all_undamaged_text():
    original = "Лимит: 50\ufffd рублей. Доля 100%."
    item = SimpleNamespace(text_body=Body(original), body=HTMLBody("<html><body>Лимит: 500 рублей.</body></html>"))
    assert complete_body_text(item) == (original, "text", "text_body")


def test_malformed_alternate_html_keeps_the_damaged_but_complete_source():
    original = "Ответ: \ufffd. Лимит 500."
    item = SimpleNamespace(text_body=Body(original), body=HTMLBody("<html><head><style>unterminated"))
    assert complete_body_text(item) == (original, "text", "text_body")
