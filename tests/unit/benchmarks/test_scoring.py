from benchmarks.runner.scoring import score_assignment


def test_all_acceptable():
    expected = {
        "q1": {"acceptable": ["dynamodb", "elasticache"], "ideal": "dynamodb"},
        "q2": {"acceptable": ["opensearch"], "ideal": "opensearch"},
    }
    actual = {"q1": "elasticache", "q2": "opensearch"}
    r = score_assignment(actual, expected)
    assert r.scored_count == 2
    assert r.acceptable_count == 2
    assert r.acceptable_accuracy == 1.0
    assert r.ideal_count == 1


def test_one_wrong():
    expected = {
        "q1": {"acceptable": ["dynamodb"], "ideal": "dynamodb"},
        "q2": {"acceptable": ["opensearch"], "ideal": "opensearch"},
    }
    actual = {"q1": "aurora_mysql", "q2": "opensearch"}
    r = score_assignment(actual, expected)
    assert r.acceptable_count == 1
    assert r.acceptable_accuracy == 0.5
    v = {pv.query_id: pv.acceptable for pv in r.per_query}
    assert v == {"q1": False, "q2": True}


def test_unmatched_query_reported_not_scored():
    expected = {"q1": {"acceptable": ["dynamodb"]}}
    actual = {"q1": "dynamodb", "q_extra": "opensearch"}
    r = score_assignment(actual, expected)
    assert r.scored_count == 1
    assert r.acceptable_accuracy == 1.0
    assert r.unmatched == ["q_extra"]


def test_missing_assignment_counts_as_not_acceptable():
    expected = {"q1": {"acceptable": ["dynamodb"]}, "q2": {"acceptable": ["opensearch"]}}
    actual = {"q1": "dynamodb"}
    r = score_assignment(actual, expected)
    assert r.scored_count == 2
    assert r.acceptable_count == 1
    missing = {pv.query_id: pv.assigned for pv in r.per_query if pv.query_id == "q2"}
    assert missing["q2"] is None
