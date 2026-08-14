from typing import TYPE_CHECKING

from snowddl.blueprint import Ident

if TYPE_CHECKING:
    from snowddl.engine import SnowDDLEngine


class SchemaCache:
    def __init__(self, engine: "SnowDDLEngine"):
        self.engine = engine

        self.databases = {}
        self.schemas = {}

        self.database_params = {}
        self.schema_params = {}

        self.reload()

    def reload(self):
        self.databases = {}
        self.schemas = {}

        self.database_params = {}
        self.schema_params = {}

        cur = self.engine.execute_meta(
            "SHOW DATABASES LIKE {env_prefix:ls}",
            {
                "env_prefix": self.engine.config.env_prefix,
            },
        )

        for r in cur:
            # Skip databases created by other roles
            if r["owner"] != self.engine.context.current_role and not self.engine.settings.ignore_ownership:
                continue

            # Skip non-standard databases
            if r["kind"] != "STANDARD":
                continue

            # OIE patch (#11): skip a database whose name is not a valid identifier.
            #
            # Snowflake renames a dropped user's personal database to
            # DROPPED_USER$<login>_<epoch>, and the login is an email address, so the
            # name contains dots -- while `kind` stays STANDARD, which is why the check
            # above does not catch it. Ident() rejects a dot, and this line raised
            # ValueError before any planning began, so a single deprovisioned user broke
            # `plan` and `apply` outright for any identity that can SEE that database
            # (measured 2026-08-14: five users dropped in 34 minutes; an ACCOUNTADMIN
            # secondary role is enough to see them, a plain OIE_ADMIN is not).
            #
            # Skipping is exactly right rather than merely defensive: a name SnowDDL
            # cannot parse can never appear in include_databases, so the branch below
            # would always have continued anyway. The only behaviour that changes is
            # that it now continues instead of crashing.
            # Logged, not silent: skipping is correct, but a database vanishing from
            # SnowDDL's view with no trace is how a real config problem gets read as
            # "nothing to do".
            try:
                database_ident = Ident(r["name"])
            except ValueError:
                self.engine.logger.debug(
                    f"Skipped database [{r['name']}]: name is not a valid SnowDDL identifier"
                )
                continue

            # Skip databases not listed in settings explicitly
            if self.engine.settings.include_databases and database_ident not in self.engine.settings.include_databases:
                continue

            self.databases[r["name"]] = {
                "database": r["name"],
                "owner": r["owner"],
                "comment": r["comment"] if r["comment"] else None,
                "is_transient": "TRANSIENT" in r["options"],
                "retention_time": int(r["retention_time"]),
            }

        # Load schemas in parallel
        for database_schemas in self.engine.executor.map(self._get_database_schemas, self.databases.values()):
            self.schemas.update(database_schemas)

        # Load database parameters in parallel
        for database_params in self.engine.executor.map(self._get_database_params, self.databases.values()):
            self.database_params.update(database_params)

        # Load schema params parameters in parallel
        for schema_params in self.engine.executor.map(self._get_schema_params, self.schemas.values()):
            self.schema_params.update(schema_params)

    def _get_database_schemas(self, database_row):
        schemas = {}

        cur = self.engine.execute_meta(
            "SHOW SCHEMAS IN DATABASE {database:i}",
            {
                "database": database_row["database"],
            },
        )

        for r in cur:
            # Skip INFORMATION_SCHEMA
            if r["name"] == "INFORMATION_SCHEMA":
                continue

            schemas[f"{r['database_name']}.{r['name']}"] = {
                "database": r["database_name"],
                "schema": r["name"],
                "owner": r["owner"],
                "comment": r["comment"] if r["comment"] else None,
                "is_transient": "TRANSIENT" in r["options"],
                "is_managed_access": "MANAGED ACCESS" in r["options"],
                "retention_time": int(r["retention_time"]) if r["retention_time"].isdigit() else 0,
            }

        return schemas

    def _get_database_params(self, database_row):
        database_params = {database_row["database"]: {}}

        cur = self.engine.execute_meta(
            "SHOW PARAMETERS IN DATABASE {database:i}",
            {
                "database": database_row["database"],
            },
        )

        for r in cur:
            if r["level"] == "DATABASE":
                database_params[database_row["database"]][r["key"]] = self._cast_param_value(r["value"], r["type"])

        return database_params

    def _get_schema_params(self, schema_row):
        schema_name = f"{schema_row['database']}.{schema_row['schema']}"
        schema_params = {schema_name: {}}

        cur = self.engine.execute_meta(
            "SHOW PARAMETERS IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema_row["database"],
                "schema": schema_row["schema"],
            },
        )

        for r in cur:
            if r["level"] == "SCHEMA":
                schema_params[schema_name][r["key"]] = self._cast_param_value(r["value"], r["type"])

        return schema_params

    def _cast_param_value(self, value, value_type):
        if value_type == "BOOLEAN":
            return value == "true"

        if value_type == "NUMBER":
            return int(value)

        return value
