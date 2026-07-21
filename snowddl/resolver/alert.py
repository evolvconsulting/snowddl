from snowddl.blueprint import AlertBlueprint
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver, ResolveResult, ObjectType


class AlertResolver(AbstractSchemaObjectResolver):
    skip_on_empty_blueprints = True

    def get_object_type(self) -> ObjectType:
        return ObjectType.ALERT

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW ALERTS IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            existing_objects[f"{r['database_name']}.{r['schema_name']}.{r['name']}"] = {
                "database": r["database_name"],
                "schema": r["schema_name"],
                "name": r["name"],
                "owner": r["owner"],
                "warehouse": r["warehouse"],
                "schedule": r["schedule"],
                "state": r["state"],
                "condition": r["condition"],
                "action": r["action"],
                "comment": r["comment"] if r["comment"] else None,
            }

        return existing_objects

    def get_blueprints(self):
        return self.config.get_blueprints_by_type(AlertBlueprint)

    def create_object(self, bp: AlertBlueprint):
        query = self.engine.query_builder()

        query.append(
            "CREATE ALERT {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        if bp.warehouse:
            query.append_nl(
                "WAREHOUSE = {warehouse:i}",
                {
                    "warehouse": bp.warehouse,
                },
            )

        query.append_nl(
            "SCHEDULE = {schedule}",
            {
                "schedule": bp.schedule,
            },
        )

        query.append_nl("IF(EXISTS(")
        query.append_nl(bp.condition)
        query.append_nl("))")

        query.append_nl("THEN")
        query.append_nl(bp.action)

        self.engine.execute_safe_ddl(query)

        # Snowflake creates every alert SUSPENDED. Resume it unless the blueprint
        # explicitly disables it, so a declared alert is actually running after
        # `snowddl apply` — otherwise the object exists but never fires.
        if bp.enabled:
            self.engine.execute_safe_ddl(
                "ALTER ALERT {full_name:i} RESUME",
                {
                    "full_name": bp.full_name,
                },
            )

        return ResolveResult.CREATE

    def compare_object(self, bp: AlertBlueprint, row: dict):
        result = ResolveResult.NOCHANGE

        # Normalise warehouse for comparison: bp.warehouse is an Ident (or None),
        # row["warehouse"] is a bare string from SHOW ALERTS ("" when unset).
        bp_warehouse = str(bp.warehouse) if bp.warehouse else None
        row_warehouse = row["warehouse"] if row["warehouse"] else None

        warehouse_changed = bp_warehouse != row_warehouse
        schedule_changed = str(bp.schedule) != row["schedule"]
        condition_changed = str(bp.condition) != row["condition"]
        action_changed = str(bp.action) != row["action"]

        needs_definition_alter = warehouse_changed or schedule_changed or condition_changed or action_changed

        was_started = (row.get("state") or "").upper() == "STARTED"

        # Snowflake requires an alert to be SUSPENDED before its definition can be
        # modified. Suspend first if we're about to alter a running alert; we
        # restore the desired run-state at the end.
        suspended_for_alter = False
        if needs_definition_alter and was_started:
            self.engine.execute_safe_ddl(
                "ALTER ALERT {full_name:i} SUSPEND",
                {
                    "full_name": bp.full_name,
                },
            )
            suspended_for_alter = True

        if warehouse_changed:
            if bp.warehouse:
                self.engine.execute_safe_ddl(
                    "ALTER ALERT {full_name:i} SET WAREHOUSE = {warehouse:i}",
                    {
                        "full_name": bp.full_name,
                        "warehouse": bp.warehouse,
                    },
                )
            else:
                self.engine.execute_safe_ddl(
                    "ALTER ALERT {full_name:i} UNSET WAREHOUSE",
                    {
                        "full_name": bp.full_name,
                    },
                )

            result = ResolveResult.ALTER

        if schedule_changed:
            self.engine.execute_safe_ddl(
                "ALTER ALERT {full_name:i} SET SCHEDULE = {schedule}",
                {
                    "full_name": bp.full_name,
                    "schedule": bp.schedule,
                },
            )

            result = ResolveResult.ALTER

        if condition_changed:
            query = self.engine.query_builder()

            query.append(
                "ALTER ALERT {full_name:i} MODIFY CONDITION EXISTS (",
                {
                    "full_name": bp.full_name,
                },
            )

            query.append_nl(bp.condition)
            query.append_nl(")")

            self.engine.execute_safe_ddl(query)

            result = ResolveResult.ALTER

        if action_changed:
            query = self.engine.query_builder()

            query.append(
                "ALTER ALERT {full_name:i} MODIFY ACTION",
                {
                    "full_name": bp.full_name,
                },
            )

            query.append_nl(bp.action)

            self.engine.execute_safe_ddl(query)

            result = ResolveResult.ALTER

        # Reconcile the run-state (RESUME/SUSPEND). A state mismatch on its own is
        # a real ALTER; re-resuming after a definition alter merely restores the
        # state we interrupted and is not itself counted as a change.
        if bp.enabled:
            if not was_started:
                self.engine.execute_safe_ddl(
                    "ALTER ALERT {full_name:i} RESUME",
                    {
                        "full_name": bp.full_name,
                    },
                )
                result = ResolveResult.ALTER
            elif suspended_for_alter:
                self.engine.execute_safe_ddl(
                    "ALTER ALERT {full_name:i} RESUME",
                    {
                        "full_name": bp.full_name,
                    },
                )
        else:
            if was_started and not suspended_for_alter:
                self.engine.execute_safe_ddl(
                    "ALTER ALERT {full_name:i} SUSPEND",
                    {
                        "full_name": bp.full_name,
                    },
                )
                result = ResolveResult.ALTER

        return result

    def drop_object(self, row: dict):
        self.engine.execute_safe_ddl(
            "DROP ALERT {database:i}.{schema:i}.{name:i}",
            {
                "database": row["database"],
                "schema": row["schema"],
                "name": row["name"],
            },
        )

        return ResolveResult.DROP
