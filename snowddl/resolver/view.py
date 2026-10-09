from snowddl.blueprint import ViewBlueprint

from snowddl.error import SnowDDLExecuteError
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver, ResolveResult, ObjectType


class ViewResolver(AbstractSchemaObjectResolver):
    def get_object_type(self) -> ObjectType:
        return ObjectType.VIEW

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW VIEWS IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            if r["is_materialized"] == "true":
                continue

            existing_objects[f"{r['database_name']}.{r['schema_name']}.{r['name']}"] = {
                "database": r["database_name"],
                "schema": r["schema_name"],
                "name": r["name"],
                "owner": r["owner"],
                "text": str(r["text"]).rstrip(";"),
                "is_secure": r["is_secure"] == "true",
                "comment": r["comment"] if r["comment"] else None,
            }

        return existing_objects

    def get_blueprints(self):
        return self.config.get_blueprints_by_type(ViewBlueprint)

    def create_object(self, bp: ViewBlueprint):
        self.engine.execute_safe_ddl(self._build_create_view(bp))

        # Comments on views are broken and must be applied separately
        if bp.comment:
            self.engine.execute_safe_ddl(
                "COMMENT ON VIEW {full_name:i} IS {comment}",
                {
                    "full_name": bp.full_name,
                    "comment": bp.comment,
                },
            )

        return ResolveResult.CREATE

    def compare_object(self, bp: ViewBlueprint, row: dict):
        query = self._build_create_view(bp)

        # OIE patch (references-only view visibility, D-291/D-293). A SECURE view's
        # `text` comes back "" from SHOW VIEWS for any role that is not its owner --
        # Snowflake withholds it regardless of privilege, so a REFERENCES-only plan
        # role (Layer-1 CI conformance) can never match it against config and always
        # falls into the REPLACE path below. The view exists (it is in `row` at all)
        # and the comment is still visible, so treat it as present with an unknown
        # body instead of mismatched. Opt-in: a role that owns or can read the view
        # keeps `text` populated, so this branch is never taken for it.
        if (
            self.engine.settings.ignore_unreadable_view_definitions
            and bp.is_secure
            and row["is_secure"]
            and not row["text"]
        ):
            if bp.comment != row["comment"]:
                self.engine.execute_safe_ddl(
                    "COMMENT ON VIEW {full_name:i} IS {comment}",
                    {
                        "full_name": bp.full_name,
                        "comment": bp.comment if bp.comment else "",
                    },
                )

                return ResolveResult.ALTER

            return ResolveResult.NOCHANGE

        # If view text is exactly the same
        if row["text"] == str(query):
            try:
                # ... and it is possible to query view (underlying objects were not changed)
                self.engine.describe_meta(
                    "SELECT * FROM {full_name:i}",
                    {
                        "full_name": bp.full_name,
                    },
                )
            except SnowDDLExecuteError as e:
                self.engine.logger.debug(
                    f"View [{bp.full_name}] caused describe error [{e.snow_exc.errno}]: {e.snow_exc.raw_msg}"
                )

                # OIE patch (OIE-2314 item 75 follow-up): log the errno at INFO
                # whenever the flag is on, so a real dispatch shows which code fired
                # without needing a DEBUG-level log level change. Run 37944740290
                # left V_SIGNAL_SENSITIVITY_GOLD_COMPARISON REPLACEing under this
                # branch with no visible errno -- this line is what the next
                # dispatch will show instead.
                if self.engine.settings.ignore_unreadable_view_definitions:
                    self.engine.logger.info(
                        f"View [{bp.full_name}] describe probe errno [{e.snow_exc.errno}] "
                        f"under --ignore-unreadable-view-definitions"
                    )

                # OIE patch (same visibility class): both 2003 and 3001 are Snowflake
                # errors that mean "this role cannot reach the object", never "the
                # object itself is broken" -- the same pair this repo's own retrieval
                # layer already treats as equivalent and verified live against this
                # account (src/oie/retrieval/errors.py `_UNREACHABLE_ERRNOS`,
                # 2026-08-14):
                #   2003 (42S02) "does not exist or not authorized" -- the one error
                #     Snowflake gives for both a genuinely missing object and one this
                #     role has no visibility into at all.
                #   3001 (42501) "SQL access control error: Insufficient privileges to
                #     operate on <object>" -- the object IS visible (name resolves,
                #     e.g. via a REFERENCES grant) but the specific operation (SELECT)
                #     needs a privilege this role does not hold. Observed repeatedly in
                #     this repo for exactly this shape -- a role that can see an object
                #     but lacks the one privilege an operation needs (migrations/README.md,
                #     `archive/handovers/HANDOVER-2026-07-29-OIE-621-wave2-superseded.md`).
                # Both are privilege-class SQL access control errors, not compilation
                # errors against the view body (e.g. 904 "invalid identifier" would mean
                # the view itself is broken, and is NOT in this set). Text already
                # matched config byte for byte (the branch above), so under the same
                # opt-in, accept either rather than replace an object nobody changed. A
                # role that holds SELECT never hits either errno for a missing-privilege
                # reason, so its real drift is still caught.
                if self.engine.settings.ignore_unreadable_view_definitions and e.snow_exc.errno in (2003, 3001):
                    if bp.comment != row["comment"]:
                        self.engine.execute_safe_ddl(
                            "COMMENT ON VIEW {full_name:i} IS {comment}",
                            {
                                "full_name": bp.full_name,
                                "comment": bp.comment if bp.comment else "",
                            },
                        )

                        return ResolveResult.ALTER

                    return ResolveResult.NOCHANGE
            else:
                # Comments on views are broken and must be applied separately
                if bp.comment != row["comment"]:
                    self.engine.execute_safe_ddl(
                        "COMMENT ON VIEW {full_name:i} IS {comment}",
                        {
                            "full_name": bp.full_name,
                            "comment": bp.comment if bp.comment else "",
                        },
                    )

                    return ResolveResult.ALTER
                else:
                    return ResolveResult.NOCHANGE
        else:
            self.engine.logger.debug(f"View [{bp.full_name}] text did not match")

        # Replace view if we got here
        self.engine.execute_safe_ddl(query)

        # Comments on views are broken and must be applied separately
        if bp.comment:
            self.engine.execute_safe_ddl(
                "COMMENT ON VIEW {full_name:i} IS {comment}",
                {
                    "full_name": bp.full_name,
                    "comment": bp.comment,
                },
            )

        return ResolveResult.REPLACE

    def drop_object(self, row: dict):
        self.engine.execute_safe_ddl(
            "DROP VIEW {database:i}.{schema:i}.{view_name:i}",
            {
                "database": row["database"],
                "schema": row["schema"],
                "view_name": row["name"],
            },
        )

        return ResolveResult.DROP

    def _build_create_view(self, bp: ViewBlueprint):
        query = self.engine.query_builder()

        query.append("CREATE OR REPLACE")

        if bp.is_secure:
            query.append("SECURE")

        query.append(
            "VIEW {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        if bp.columns:
            query.append_nl("(")

            for idx, c in enumerate(bp.columns):
                query.append_nl(
                    "    {comma:r}{col_name:i}",
                    {
                        "comma": "  " if idx == 0 else ", ",
                        "col_name": c.name,
                    },
                )

                if c.comment:
                    query.append(
                        "COMMENT {col_comment}",
                        {
                            "col_comment": c.comment,
                        },
                    )

            query.append_nl(")")

        if bp.change_tracking:
            query.append_nl("CHANGE_TRACKING = TRUE")

        query.append_nl("COPY GRANTS")
        query.append_nl("AS")
        query.append_nl(bp.text)

        return query
