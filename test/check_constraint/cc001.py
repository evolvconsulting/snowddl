from snowddl.resolver.check_constraint import parse_check_constraints


def test_step1(helper):
    checks = parse_check_constraints(helper.get_table_ddl("db1", "sc1", "cc001_tb1"))

    # All three named CHECK constraints were created, names preserved
    assert set(checks) == {"CC001_SOURCE_CHK", "CC001_BODY_CT_CHK", "CC001_SCORE_RANGE_CHK"}

    # Single-column equality
    assert "SOURCE_PIPELINE" in checks["CC001_SOURCE_CHK"]
    assert "'outlook'" in checks["CC001_SOURCE_CHK"]

    # IN (...) list
    assert "BODY_CONTENT_TYPE" in checks["CC001_BODY_CT_CHK"]
    assert "'text'" in checks["CC001_BODY_CT_CHK"]
    assert "'html'" in checks["CC001_BODY_CT_CHK"]

    # Multi-column range
    assert "100" in checks["CC001_SCORE_RANGE_CHK"]


def test_step2(helper):
    checks = parse_check_constraints(helper.get_table_ddl("db1", "sc1", "cc001_tb1"))

    # CC001_BODY_CT_CHK was dropped
    assert set(checks) == {"CC001_SOURCE_CHK", "CC001_SCORE_RANGE_CHK"}

    # CC001_SOURCE_CHK is unchanged
    assert "SOURCE_PIPELINE" in checks["CC001_SOURCE_CHK"]

    # CC001_SCORE_RANGE_CHK expression was altered (0..100 -> 0..1000)
    assert "1000" in checks["CC001_SCORE_RANGE_CHK"]


def test_step3(helper):
    table = helper.show_table("db1", "sc1", "cc001_tb1")

    # Table (and its CHECK constraints) were dropped
    assert table is None
