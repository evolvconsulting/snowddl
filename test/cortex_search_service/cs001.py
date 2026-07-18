def test_step1(helper):
    svc = helper.show_cortex_search_service("db1", "sc1", "cs001_css1")

    # Cortex Search Service was created
    assert svc is not None
    assert svc["name"] == "CS001_CSS1"


def test_step2(helper):
    desc = helper.desc_cortex_search_service("db1", "sc1", "cs001_css1")

    assert desc is not None

    # target_lag was changed 1 day -> 2 days (applied via CREATE OR REPLACE).
    # DESC column naming varies, so scan all values defensively.
    values = " ".join(str(v).lower() for v in desc.values())
    assert "2 day" in values


def test_step3(helper):
    # step3 config is identical to step2, so re-apply must be a no-op: the service still exists
    # and its definition is unchanged (idempotency / no plan churn).
    # NB: CortexSearchServiceResolver sets skip_on_empty_blueprints (like the semantic_view template
    # it is cloned from), so dropping the *last* service via config removal is intentionally not
    # exercised here; drop_object is covered by the destroy sequence.
    svc = helper.show_cortex_search_service("db1", "sc1", "cs001_css1")

    assert svc is not None
    assert svc["name"] == "CS001_CSS1"

    desc = helper.desc_cortex_search_service("db1", "sc1", "cs001_css1")
    values = " ".join(str(v).lower() for v in desc.values())
    assert "2 day" in values
