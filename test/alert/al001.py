def test_step1(helper):
    alert_show = helper.show_alert("db1", "sc1", "al001_al1")

    assert str(alert_show["warehouse"]).endswith("__AL001_WH1")
    assert alert_show["schedule"] == "1 minute"
    assert alert_show["condition"] == f"SELECT * FROM {helper.env_prefix}db1.sc1.al001_tb1"
    assert alert_show["action"] == "SELECT CURRENT_TIMESTAMP()"
    # Alerts are created SUSPENDED by Snowflake; the resolver RESUMEs them so a
    # declared (enabled, the default) alert is actually running after apply.
    assert str(alert_show["state"]).lower() == "started"


def test_step2(helper):
    alert_show = helper.show_alert("db1", "sc1", "al001_al1")

    assert str(alert_show["warehouse"]).endswith("__AL001_WH2")
    assert alert_show["schedule"] == "USING CRON * * * * * UTC"

    assert alert_show["condition"] == f"SELECT 1\nFROM {helper.env_prefix}db1.sc1.al001_tb1"
    assert alert_show["action"] == "SELECT\nCURRENT_TIMESTAMP() AS abc"
    # After a definition change the resolver suspends → alters → resumes, so the
    # alert must end back in the started state (not left suspended).
    assert str(alert_show["state"]).lower() == "started"


def test_step3(helper):
    alert_show = helper.show_alert("db1", "sc1", "al001_al1")

    assert alert_show is None
