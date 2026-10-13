from src.tools.sql.normalize import normalize_digest_text


def test_placeholder_next_to_a_word_stays_a_separate_token():
    assert normalize_digest_text("?AS", "mysql").text == "NULL AS"
    assert normalize_digest_text("SELECT a FROM t WHERE x=?AND y=?", "mysql").text == (
        "SELECT a FROM t WHERE x=NULL AND y=NULL"
    )


def test_normalizing_twice_changes_nothing_next_to_words():
    once = normalize_digest_text("?AS", "mysql")
    twice = normalize_digest_text(once.text, "mysql")
    assert twice.text == once.text
    assert twice.truncated == once.truncated
