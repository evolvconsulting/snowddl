# SnowDDL

[![PyPI](https://badge.fury.io/py/snowddl.svg)](https://badge.fury.io/py/snowddl)
[![Getting Started](https://github.com/littleK0i/SnowDDL/actions/workflows/getting_started.yml/badge.svg)](https://github.com/littleK0i/SnowDDL/actions/workflows/getting_started.yml)
[![Pytest](https://github.com/littleK0i/SnowDDL/actions/workflows/pytest.yml/badge.svg)](https://github.com/littleK0i/SnowDDL/actions/workflows/pytest.yml)

---

## About this fork

`evolvconsulting/snowddl` is evolv Consulting's fork of upstream SnowDDL. It carries patches on top
of upstream `0.67.5` and is consumed by pinning a tag, never a branch.

**Tag scheme.** Releases are tagged `0.67.5-evolv.N`. This supersedes the earlier `0.67.5-oie.N`
scheme, which stopped at `0.67.5-oie.12` — there is no `oie.13`. The old tags are left in place so
existing pins keep resolving; move to `evolv.N` at your next bump. The rename is cosmetic: the fork
is shared across projects, and the old name implied it belonged to one of them.

**CI.** `fork_tests.yml` runs this fork's own regression tests on every push and pull request. They
are pure-unit and need no credentials. Upstream's `Pytest` and `Getting Started` workflows require a
live Snowflake account — `test/run_test_full.sh` issues `destroy` then `apply` — so they are
`workflow_dispatch` only here, and their badges above describe upstream, not this fork.

### `is_unmanaged` (fork-only)

Upstream's `is_sandbox` does one thing: it suppresses drops. Objects that exist in Snowflake but
not in the config are left alone. Everything the config *does* declare in that schema is still
created and altered normally.

This fork adds a second, independent key, `is_unmanaged`, for the case `is_sandbox` was being
stretched to cover — a schema this config recognizes but must never issue DDL against, because
another tier owns it and the applying role may not even be able to see it. Blueprints in an
unmanaged schema resolve to `SKIP` instead of create/compare, so a whole-config apply exits cleanly
rather than erroring on `CREATE`/`ALTER`.

| Key            | Drops undeclared objects | Creates/alters declared objects |
| -------------- | ------------------------ | ------------------------------- |
| *(neither)*    | yes                      | yes                             |
| `is_sandbox`   | no                       | yes                             |
| `is_unmanaged` | yes                      | no                              |
| both           | no                       | no                              |

Both keys are valid on `DATABASE` and `SCHEMA` params, and both inherit database → schema. A schema
that needs both behaviors carries both. `is_sandbox` keeps its upstream meaning exactly, so existing
configs are unaffected.

```yaml
# <database>/<schema>/params.yaml
is_unmanaged: true
```

---

SnowDDL is a [declarative-style](https://www.snowflake.com/blog/embracing-agile-software-delivery-and-devops-with-snowflake/) tool for object management automation in [Snowflake](http://snowflake.com).

It is not intended to replace other tools entirely, but to provide an alternative approach focused on practical data engineering challenges.

You may find SnowDDL useful if:

- complexity of object schema grows exponentially, and it becomes hard to manage;
- your organization maintains multiple Snowflake accounts (dev, stage, prod);
- your organization has multiple developers sharing the same Snowflake account and suffering from conflicts;
- it is necessary to generate some part of configuration dynamically using Python;

## Main features

1. SnowDDL is "stateless".
2. SnowDDL can revert any changes.
3. SnowDDL supports ALTER COLUMN.
4. SnowDDL provides built-in "Role hierarchy" model.
5. SnowDDL re-creates invalid views automatically.
6. SnowDDL simplifies code review.
7. SnowDDL supports creation of isolated "environments" for individual developers and CI/CD scripts.
8. SnowDDL strikes a good balance between dependency management overhead and parallelism.
9. SnowDDL configuration can be generated dynamically in Python code.
10. SnowDDL can manage packages for Java and Python UDF scripts natively.

## Quick links

- [Getting started](https://docs.snowddl.com/getting-started)
- [Main features](https://docs.snowddl.com/features)
- [Object types](https://docs.snowddl.com/object-types)
- [Role hierarchy](https://docs.snowddl.com/guides/role-hierarchy)
- [CLI interface](https://docs.snowddl.com/basic/cli)
- [YAML configs](https://docs.snowddl.com/basic/yaml-configs)
- [Changelog](/CHANGELOG.md)

## Introduction videos

- [:video_camera: Main features](https://www.youtube.com/watch?v=e5K4jmlxvWc "SnowDDL: Main Features")
- [:video_camera: Getting started](https://www.youtube.com/watch?v=OtMebyQizRA "SnowDDL: Getting Started")

## Mini-roadmap

- ~~placeholders in YAML configs~~ (done)
- ~~documentation for dynamic config generation in Python ("advanced mode")~~ (done)
- ~~video tutorials~~ (done, but more tutorials are coming in future)
- full test coverage for all object types and transformations

## Issues? Questions? Feedback?

Please use GitHub "Issues" to report bugs and technical problems.

Please use GitHub "Discussions" to ask questions and provide feedback.

## Created by
[Vitaly Markov](https://www.linkedin.com/in/markov-vitaly/), 2026

Enjoy!
