from json import loads as json_loads

from snowddl.blueprint import CortexSearchServiceBlueprint
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver, ResolveResult, ObjectType


class CortexSearchServiceResolver(AbstractSchemaObjectResolver):
    skip_on_empty_blueprints = True

    def get_object_type(self) -> ObjectType:
        return ObjectType.CORTEX_SEARCH_SERVICE

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        # SHOW CORTEX SEARCH SERVICES exposes every field needed to drive the diff (verified live on OIE):
        # search_column, attribute_columns, warehouse, target_lag, refresh_mode, vector_indexes, definition.
        # No per-service DESC is required.
        cur = self.engine.execute_meta(
            "SHOW CORTEX SEARCH SERVICES IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            full_name = f"{r['database_name']}.{r['schema_name']}.{r['name']}"

            existing_objects[full_name] = {
                "database": r["database_name"],
                "schema": r["schema_name"],
                "name": r["name"],
                "owner": r.get("owner"),
                "search_column": r["search_column"],
                "vector_indexes": self._parse_vector_indexes(r.get("vector_indexes")),
                "attributes": self._split_columns(r.get("attribute_columns")),
                "warehouse": r["warehouse"],
                "target_lag": r["target_lag"],
                "refresh_mode": r["refresh_mode"] if r.get("refresh_mode") else None,
                "text": self._normalise_text(r.get("definition")),
                "comment": r["comment"] if r["comment"] else None,
            }

        return existing_objects

    def get_blueprints(self):
        return self.config.get_blueprints_by_type(CortexSearchServiceBlueprint)

    def create_object(self, bp: CortexSearchServiceBlueprint):
        self.engine.execute_safe_ddl(self._build_create_cortex_search_service_sql(bp))

        return ResolveResult.CREATE

    def compare_object(self, bp: CortexSearchServiceBlueprint, row: dict):
        # Cortex Search has very limited ALTER support, so any change to the defining fields is applied
        # via CREATE OR REPLACE (which rebuilds the index — hence the aggressive normalization below to
        # avoid spurious, expensive replaces on cosmetic whitespace differences).
        replace_reasons = []

        if str(bp.search_column) != str(row["search_column"]):
            replace_reasons.append("Search column was changed")

        bp_vector_indexes = [str(v) for v in bp.vector_indexes] if bp.vector_indexes else []
        if bp_vector_indexes != row["vector_indexes"]:
            replace_reasons.append("Vector indexes were changed")

        if [str(a) for a in bp.attributes] != row["attributes"]:
            replace_reasons.append("Attributes were changed")

        if str(bp.warehouse) != str(row["warehouse"]):
            replace_reasons.append("Warehouse was changed")

        if self._normalise_target_lag(bp.target_lag) != self._normalise_target_lag(row["target_lag"]):
            replace_reasons.append("Target lag was changed")

        if bp.refresh_mode and row["refresh_mode"] and bp.refresh_mode != row["refresh_mode"]:
            replace_reasons.append("Refresh mode was changed")

        if self._normalise_text(bp.text) != row["text"]:
            replace_reasons.append("Base query was changed")

        if replace_reasons:
            query = self._build_create_cortex_search_service_sql(bp)
            self.engine.execute_unsafe_ddl("\n".join(f"-- {r}" for r in replace_reasons) + "\n" + str(query))

            return ResolveResult.REPLACE

        if bp.comment != row["comment"]:
            self.engine.execute_safe_ddl(
                "ALTER CORTEX SEARCH SERVICE {full_name:i} SET COMMENT = {comment}",
                {
                    "full_name": bp.full_name,
                    "comment": bp.comment,
                },
            )

            return ResolveResult.ALTER

        return ResolveResult.NOCHANGE

    def drop_object(self, row: dict):
        self.engine.execute_safe_ddl(
            "DROP CORTEX SEARCH SERVICE {database:i}.{schema:i}.{name:i}",
            {
                "database": row["database"],
                "schema": row["schema"],
                "name": row["name"],
            },
        )

        return ResolveResult.DROP

    def _build_create_cortex_search_service_sql(self, bp: CortexSearchServiceBlueprint):
        query = self.engine.query_builder()

        query.append(
            "CREATE OR REPLACE CORTEX SEARCH SERVICE {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        if bp.vector_indexes:
            # Hybrid (text + user-provided vectors) form, matching GET_DDL's re-executable rendering
            query.append_nl(
                "TEXT INDEXES {search_column:i}",
                {
                    "search_column": bp.search_column,
                },
            )

            query.append_nl(
                "VECTOR INDEXES {vector_indexes:i}",
                {
                    "vector_indexes": bp.vector_indexes,
                },
            )
        else:
            # Classic text-only form
            query.append_nl(
                "ON {search_column:i}",
                {
                    "search_column": bp.search_column,
                },
            )

        if bp.attributes:
            query.append_nl(
                "ATTRIBUTES {attributes:i}",
                {
                    "attributes": bp.attributes,
                },
            )

        query.append_nl(
            "WAREHOUSE = {warehouse:i}",
            {
                "warehouse": bp.warehouse,
            },
        )

        query.append_nl(
            "TARGET_LAG = {target_lag}",
            {
                "target_lag": bp.target_lag,
            },
        )

        if bp.refresh_mode:
            query.append_nl(
                "REFRESH_MODE = {refresh_mode:r}",
                {
                    "refresh_mode": bp.refresh_mode,
                },
            )

        if bp.comment:
            query.append_nl(
                "COMMENT = {comment}",
                {
                    "comment": bp.comment,
                },
            )

        query.append_nl("AS")
        query.append_nl(bp.text)

        return query

    def _parse_vector_indexes(self, vector_indexes):
        if not vector_indexes:
            return []

        # SHOW renders this as a JSON array, e.g. [{"auto_embedded":false,"column":"CHUNK_VEC"}].
        # Only user-provided vector columns (auto_embedded=false) are part of the declarative config;
        # entries with auto_embedded=true are implicitly generated by Snowflake from the text/search
        # column (classic ON form) and must be excluded, or a text-only service would churn every plan.
        return [item["column"] for item in json_loads(vector_indexes) if not item.get("auto_embedded")]

    def _split_columns(self, columns):
        if not columns:
            return []

        return [c.strip() for c in columns.split(",") if c.strip()]

    def _normalise_text(self, text):
        if text is None:
            return None

        return " ".join(text.rstrip(";").split())

    def _normalise_target_lag(self, target_lag):
        if target_lag is None:
            return None

        return " ".join(str(target_lag).lower().split())
