from itertools import islice
from re import compile

from snowddl.blueprint import (
    AccountObjectIdent,
    Ident,
    TableBlueprint,
    TableColumn,
    DataType,
    BaseDataType,
    SchemaObjectIdent,
    SearchOptimizationItem,
)
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver, ResolveResult, ObjectType

cluster_by_syntax_re = compile(r"^(\w+)?\((.*)\)$")
collate_type_syntax_re = compile(r"^(.*) COLLATE \'(.*)\'$")


class TableResolver(AbstractSchemaObjectResolver):
    def get_object_type(self) -> ObjectType:
        return ObjectType.TABLE

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW TABLES IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            # Skip other table types
            if (
                r.get("is_external") == "Y"
                or r.get("is_event") == "Y"
                or r.get("is_hybrid") == "Y"
                or r.get("is_iceberg") == "Y"
                or r.get("is_dynamic") == "Y"
            ):
                continue

            full_name = f"{r['database_name']}.{r['schema_name']}.{r['name']}"
            existing_objects[full_name] = {
                "database": r["database_name"],
                "schema": r["schema_name"],
                "name": r["name"],
                "owner": r["owner"],
                "is_transient": r["kind"] == "TRANSIENT",
                "retention_time": int(r["retention_time"]),
                "cluster_by": r["cluster_by"] if r["cluster_by"] else None,
                "change_tracking": bool(r["change_tracking"] == "ON"),
                "search_optimization": bool(r.get("search_optimization") == "ON"),
                "comment": r["comment"] if r["comment"] else None,
            }

        return existing_objects

    def get_blueprints(self):
        return self.config.get_blueprints_by_type(TableBlueprint)

    def create_object(self, bp: TableBlueprint):
        query = self._build_create_table(bp)
        self.engine.execute_safe_ddl(query)

        self._create_search_optimization(bp)
        # is_new: a table that does not exist yet has no grants to read. Under
        # `plan` the CREATE is not executed at all, so SHOW GRANTS would run
        # against a missing object and abort the whole plan on the one case this
        # feature is most likely to be introduced with -- a new table.
        self._apply_object_grants(bp, is_new=True)

        return ResolveResult.CREATE

    def compare_object(self, bp: TableBlueprint, row: dict):
        safe_alters = []
        unsafe_alters = []

        replace_reasons = []
        replace_notices = []

        bp_cols = {str(c.name): c for c in bp.columns}
        snow_cols = self._get_existing_columns(bp)

        dropping_col_names = []
        remaining_col_names = list(snow_cols.keys())

        for col_name, snow_c in snow_cols.items():
            # Drop columns which do not exist in blueprint
            if col_name not in bp_cols:
                unsafe_alters.append(
                    self.engine.format(
                        "DROP COLUMN {col_name:i}",
                        {
                            "col_name": col_name,
                        },
                    )
                )

                dropping_col_names.append(col_name)
                remaining_col_names.remove(col_name)

                replace_notices.append(f"Column {col_name} is about to be dropped")
                continue

            bp_c = bp_cols[col_name]

            # Set or drop NOT NULL constraint
            if snow_c.not_null and not bp_c.not_null:
                unsafe_alters.append(
                    self.engine.format(
                        "MODIFY COLUMN {col_name:i} DROP NOT NULL",
                        {
                            "col_name": col_name,
                        },
                    )
                )
            elif not snow_c.not_null and bp_c.not_null:
                unsafe_alters.append(
                    self.engine.format(
                        "MODIFY COLUMN {col_name:i} SET NOT NULL",
                        {
                            "col_name": col_name,
                        },
                    )
                )

            # Default
            bp_c_default_value = self._normalize_bp_default(bp_c.default)

            if snow_c.default != bp_c_default_value:
                # DROP DEFAULT is supported
                if snow_c.default is not None and bp_c_default_value is None:
                    unsafe_alters.append(
                        self.engine.format(
                            "MODIFY COLUMN {col_name:i} DROP DEFAULT",
                            {
                                "col_name": col_name,
                            },
                        )
                    )

                # Switch to another sequence is supported
                elif (
                    isinstance(snow_c.default, str)
                    and snow_c.default.upper().endswith(".NEXTVAL")
                    and isinstance(bp_c_default_value, str)
                    and bp_c_default_value.upper().endswith(".NEXTVAL")
                ):
                    unsafe_alters.append(
                        self.engine.format(
                            "MODIFY COLUMN {col_name:i} SET DEFAULT {default:r}",
                            {
                                "col_name": col_name,
                                "default": bp_c_default_value,
                            },
                        )
                    )

                # All other DEFAULT changes are not supported
                else:
                    replace_reasons.append(f"Default for column {col_name} was changed")

            # Expression
            if snow_c.expression != bp_c.expression:
                replace_reasons.append(f"Expression for column {col_name} was changed")

            # Collate
            if snow_c.collate != bp_c.collate:
                replace_reasons.append(f"Collate for column {col_name} was changed")

            # Comments
            if snow_c.comment != bp_c.comment:
                if bp_c.comment:
                    safe_alters.append(
                        self.engine.format(
                            "MODIFY COLUMN {col_name:i} COMMENT {comment}",
                            {
                                "col_name": col_name,
                                "comment": bp_c.comment,
                            },
                        )
                    )
                else:
                    safe_alters.append(
                        self.engine.format(
                            "MODIFY COLUMN {col_name:i} UNSET COMMENT",
                            {
                                "col_name": col_name,
                            },
                        )
                    )

            # If type matches exactly, skip all other checks
            if snow_c.type == bp_c.type:
                continue

            # Only a few optimized MODIFY COLUMN ... TYPE actions are supported
            # https://docs.snowflake.com/en/sql-reference/sql/alter-table-column.html
            if snow_c.type.base_type == bp_c.type.base_type:
                # Increase or decrease precision of NUMBER, but not scale
                if (
                    snow_c.type.base_type == BaseDataType.NUMBER
                    and snow_c.type.val1 != bp_c.type.val2
                    and snow_c.type.val2 == bp_c.type.val2
                ):
                    unsafe_alters.append(
                        self.engine.format(
                            "MODIFY COLUMN {col_name:i} TYPE {col_type:r}",
                            {
                                "col_name": col_name,
                                "col_type": bp_c.type,
                            },
                        )
                    )

                    continue

                if snow_c.type.base_type == BaseDataType.VARCHAR and snow_c.type.val1 < bp_c.type.val1:
                    unsafe_alters.append(
                        self.engine.format(
                            "MODIFY COLUMN {col_name:i} TYPE {col_type:r}",
                            {
                                "col_name": col_name,
                                "col_type": bp_c.type,
                            },
                        )
                    )

                    continue

            # All other data type transformations require full table replace
            replace_reasons.append(f"Data type for column {col_name} was changed from {snow_c.type} to {bp_c.type}")

        # Remaining column names exactly match initial part of blueprint column names
        if remaining_col_names == list(islice(bp_cols.keys(), 0, len(remaining_col_names))):
            # Get remaining part of blueprint columns
            for col_name, bp_c in islice(bp_cols.items(), len(remaining_col_names), None):
                query = self.engine.query_builder()
                query.append(
                    "ADD COLUMN {col_name:i} {col_type:r}",
                    {
                        "col_name": col_name,
                        "col_type": bp_c.type,
                    },
                )

                if bp_c.collate:
                    query.append(
                        "COLLATE {collate}",
                        {
                            "collate": bp_c.collate,
                        },
                    )

                if bp_c.default is not None:
                    query.append(
                        "DEFAULT {default:r}",
                        {
                            "default": self._normalize_bp_default(bp_c.default),
                        },
                    )

                if bp_c.not_null:
                    query.append("NOT NULL")

                if bp_c.comment:
                    query.append(
                        "COMMENT {comment}",
                        {
                            "comment": bp_c.comment,
                        },
                    )

                safe_alters.append(query)
        else:
            # Reordering of columns is not supported
            replace_reasons.append("Order of columns was changed")

        # Changing TRANSIENT tables to permanent and back are not supported
        if bp.is_transient is True and row["is_transient"] is False:
            replace_reasons.append("Table type was changed to TRANSIENT")
        elif bp.is_transient is False and row["is_transient"] is True:
            replace_reasons.append("Table type was changed to PERMANENT")

        # Retention time
        if bp.retention_time is not None and bp.retention_time != row["retention_time"]:
            unsafe_alters.append(
                self.engine.format("SET DATA_RETENTION_TIME_IN_DAYS = {retention_time:d}", {"retention_time": bp.retention_time})
            )

        # Clustering key
        if not self._compare_cluster_by(bp, row):
            if bp.cluster_by:
                unsafe_alters.append(
                    self.engine.format(
                        "CLUSTER BY ({cluster_by:r})",
                        {
                            "cluster_by": bp.cluster_by,
                        },
                    )
                )
            else:
                unsafe_alters.append(self.engine.format("DROP CLUSTERING KEY"))

        # Change tracking
        if bp.change_tracking != row["change_tracking"]:
            unsafe_alters.append(
                self.engine.format(
                    "SET CHANGE_TRACKING = {change_tracking:b}",
                    {
                        "change_tracking": bp.change_tracking,
                    },
                )
            )

        # Comment
        if bp.comment != row["comment"]:
            if bp.comment:
                safe_alters.append(
                    self.engine.format(
                        "SET COMMENT = {comment}",
                        {
                            "comment": bp.comment,
                        },
                    )
                )
            else:
                safe_alters.append(self.engine.format("UNSET COMMENT"))

        # Apply changes
        result = ResolveResult.NOCHANGE

        if replace_reasons:
            # fmt: off
            replace_query = "\n".join(f"-- {r}" for r in replace_reasons + replace_notices) + "\n" + str(self._build_create_table(bp, snow_cols))
            self.engine.execute_unsafe_ddl(replace_query, condition=self.engine.settings.execute_replace_table)
            # fmt: on

            result = ResolveResult.REPLACE

        elif safe_alters or unsafe_alters:
            for alter in safe_alters:
                self.engine.execute_safe_ddl(
                    "ALTER TABLE {full_name:i} {alter:r}",
                    {
                        "full_name": bp.full_name,
                        "alter": alter,
                    },
                )

            for alter in unsafe_alters:
                self.engine.execute_unsafe_ddl(
                    "ALTER TABLE {full_name:i} {alter:r}",
                    {
                        "full_name": bp.full_name,
                        "alter": alter,
                    },
                )

            result = ResolveResult.ALTER

            for col_name in dropping_col_names:
                self.engine.intention_cache.add_column_drop_intention(str(bp.full_name), col_name)

        # If table was re-created, apply or suggest search optimization using exactly the same condition value
        if result == ResolveResult.REPLACE:
            self._create_search_optimization(bp, condition=self.engine.settings.execute_replace_table)
        else:
            if self._compare_search_optimization(bp, row["search_optimization"]) and result == ResolveResult.NOCHANGE:
                result = ResolveResult.ALTER

        # OIE patch (#9): grants are reconciled on EVERY pass, not only when the
        # table DDL changed. A hand-issued grant leaves the column list untouched,
        # so a check that only ran on REPLACE/ALTER would never see the drift it
        # exists to catch. Reported as ALTER so `plan --detailed-exitcode` is
        # non-zero and the conformance gate goes red.
        if self._apply_object_grants(bp) and result == ResolveResult.NOCHANGE:
            result = ResolveResult.ALTER

        return result

    def _apply_object_grants(self, bp: TableBlueprint, is_new: bool = False):
        # OIE patch (#9): reconcile object-level grants on a table against config.
        # Returns True if any GRANT or REVOKE was issued.
        #
        # AUTHORITATIVE, BUT ONLY FOR THE PRIVILEGES NAMED IN CONFIG. For each
        # privilege that appears as a key, the declared role list becomes the live
        # grantee list -- missing roles are granted, extra roles are revoked, and an
        # empty list means nobody holds it. A privilege that does NOT appear as a key
        # is not touched at all.
        #
        # That scoping is what makes the feature usable in a repo where Terraform
        # owns the broad grants. OIE grants SELECT to six roles through schema-wide
        # ALL + FUTURE resources; if this reconciled the whole grant set, it would
        # revoke those on every apply and Terraform would re-grant them on every
        # terraform apply -- a permanent ping-pong. Naming only INSERT/UPDATE/DELETE
        # leaves the SELECT tier alone by construction.
        #
        # OWNERSHIP is skipped (it is transferred, not granted) and non-ROLE grantees
        # are skipped (user grants are SCIM's, not SnowDDL's) -- same exclusions as
        # patch #8.
        if bp.grants is None:
            return False

        live_grantees = {}

        if not is_new:
            cur = self.engine.execute_meta(
                "SHOW GRANTS ON TABLE {full_name:i}",
                {
                    "full_name": bp.full_name,
                },
            )

            for r in cur:
                if r["granted_to"] != "ROLE" or r["privilege"] == "OWNERSHIP":
                    continue

                live_grantees.setdefault(r["privilege"], set()).add(r["grantee_name"])

        is_changed = False

        for privilege, roles in bp.grants.items():
            privilege = str(privilege).upper()

            # Declared names are bare; live names carry the env prefix. Compare in
            # the live namespace, and keep the bare name so the GRANT re-prefixes
            # exactly once -- re-wrapping a prefixed name would double the prefix.
            desired = {str(AccountObjectIdent(self.config.env_prefix, role)): role for role in roles}
            current = live_grantees.get(privilege, set())

            for full_role_name in sorted(set(desired) - current):
                self.engine.execute_safe_ddl(
                    "GRANT {privilege:r} ON TABLE {full_name:i} TO ROLE {role:i}",
                    {
                        "privilege": privilege,
                        "full_name": bp.full_name,
                        "role": AccountObjectIdent(self.config.env_prefix, desired[full_role_name]),
                    },
                )

                is_changed = True

            # REVOKE is the destructive direction, so it goes through unsafe DDL and
            # is gated by --apply-unsafe, like every other removal in SnowDDL. It is
            # still PRINTED by plan either way, which is what the conformance gate
            # reads -- so drift is visible on a run that is not permitted to fix it.
            for full_role_name in sorted(current - set(desired)):
                self.engine.execute_unsafe_ddl(
                    "REVOKE {privilege:r} ON TABLE {full_name:i} FROM ROLE {role:i}",
                    {
                        "privilege": privilege,
                        "full_name": bp.full_name,
                        # Already env-prefixed by Snowflake; do not prefix again.
                        "role": AccountObjectIdent("", full_role_name),
                    },
                )

                is_changed = True

        return is_changed

    def drop_object(self, row: dict):
        self.engine.execute_unsafe_ddl(
            "DROP TABLE {database:i}.{schema:i}.{table_name:i}",
            {
                "database": row["database"],
                "schema": row["schema"],
                "table_name": row["name"],
            },
        )

        return ResolveResult.DROP

    def _get_existing_columns(self, bp: TableBlueprint):
        existing_columns = {}

        cur = self.engine.execute_meta(
            "DESC TABLE {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        for r in cur:
            m = collate_type_syntax_re.match(r["type"])

            if m:
                dtype = m.group(1)
                collate = m.group(2)
            else:
                dtype = r["type"]
                collate = None

            existing_columns[r["name"]] = TableColumn(
                name=Ident(r["name"]),
                type=DataType(dtype),
                not_null=bool(r["null?"] == "N"),
                default=r["default"] if r["default"] else None,
                expression=r["expression"] if r["expression"] else None,
                collate=collate,
                comment=r["comment"] if r["comment"] else None,
            )

        return existing_columns

    def _build_create_table(self, bp: TableBlueprint, snow_cols=None):
        query = self.engine.query_builder()
        query.append("CREATE")

        if snow_cols:
            query.append("OR REPLACE")

        if bp.is_transient:
            query.append("TRANSIENT")

        query.append(
            "TABLE {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        query.append_nl("(")

        for idx, c in enumerate(bp.columns):
            query.append_nl(
                "    {comma:r}{col_name:i} {col_type:r}",
                {
                    "comma": "  " if idx == 0 else ", ",
                    "col_name": c.name,
                    "col_type": c.type,
                },
            )

            if c.collate:
                query.append(
                    "COLLATE {collate}",
                    {
                        "collate": c.collate,
                    },
                )

            if c.default is not None:
                query.append(
                    "DEFAULT {default:r}",
                    {
                        "default": self._normalize_bp_default(c.default),
                    },
                )

            if c.not_null:
                query.append("NOT NULL")

            if c.expression:
                query.append(
                    "AS ({expression:r})",
                    {
                        "expression": c.expression,
                    },
                )

            if c.comment:
                query.append(
                    "COMMENT {comment}",
                    {
                        "comment": c.comment,
                    },
                )

        query.append_nl(")")

        if bp.cluster_by:
            query.append_nl(
                "CLUSTER BY ({cluster_by:r})",
                {
                    "cluster_by": bp.cluster_by,
                },
            )

        if bp.change_tracking:
            query.append_nl("CHANGE_TRACKING = TRUE")

        if bp.retention_time is not None:
            query.append_nl("DATA_RETENTION_TIME_IN_DAYS = {retention_time:d}", {"retention_time": bp.retention_time})

        if bp.comment:
            query.append_nl(
                "COMMENT = {comment}",
                {
                    "comment": bp.comment,
                },
            )

        if snow_cols:
            query.append_nl("COPY GRANTS")
            query.append_nl("AS")
            query.append_nl("SELECT")

            # Skip virtual columns before enumeration
            for idx, c in enumerate(c for c in bp.columns if c.expression is None):
                col_name = str(c.name)

                if col_name in snow_cols:
                    # Column already exist

                    if c.type == snow_cols[col_name].type:
                        # Column has the same type
                        query.append_nl(
                            "    {comma:r}{col_name:i} AS {col_name:i}",
                            {
                                "comma": "  " if idx == 0 else ", ",
                                "col_name": c.name,
                            },
                        )
                    elif c.type.base_type == BaseDataType.VECTOR and snow_cols[col_name].type.base_type == BaseDataType.VECTOR:
                        # Change to another VECTOR type requires intermediate ARRAY
                        query.append_nl(
                            "    {comma:r}{col_name:i}::ARRAY::{col_type:r} AS {col_name:i}",
                            {
                                "comma": "  " if idx == 0 else ", ",
                                "col_name": c.name,
                                "col_type": c.type,
                            },
                        )
                    else:
                        # Column has a different type, add type cast
                        query.append_nl(
                            "    {comma:r}{col_name:i}::{col_type:r} AS {col_name:i}",
                            {
                                "comma": "  " if idx == 0 else ", ",
                                "col_name": c.name,
                                "col_type": c.type,
                            },
                        )
                else:
                    # Column does not exist, use default value with type cast
                    if c.default is not None:
                        query.append_nl(
                            "    {comma:r}{col_default:r}::{col_type:r} AS {col_name:i}",
                            {
                                "comma": "  " if idx == 0 else ", ",
                                "col_name": c.name,
                                "col_type": c.type,
                                "col_default": self._normalize_bp_default(c.default),
                            },
                        )
                    else:
                        query.append_nl(
                            "    {comma:r}NULL::{col_type:r} AS {col_name:i}",
                            {
                                "comma": "  " if idx == 0 else ", ",
                                "col_name": c.name,
                                "col_type": c.type,
                            },
                        )

            query.append_nl(
                "FROM {full_name:i}",
                {
                    "full_name": bp.full_name,
                },
            )

        return query

    def _compare_cluster_by(self, bp: TableBlueprint, row: dict):
        bp_cluster_by = ", ".join(bp.cluster_by) if bp.cluster_by else None
        snow_cluster_by = cluster_by_syntax_re.sub(r"\2", row["cluster_by"]) if row["cluster_by"] else None

        return bp_cluster_by == snow_cluster_by

    def _create_search_optimization(self, bp: TableBlueprint, condition=True):
        # Legacy search optimization on an entire table
        if isinstance(bp.search_optimization, bool):
            if bp.search_optimization:
                self.engine.execute_unsafe_ddl(
                    "ALTER TABLE {full_name:i} ADD SEARCH OPTIMIZATION",
                    {
                        "full_name": bp.full_name,
                    },
                    condition=condition,
                )

            return

        # Detailed search optimization on specific columns
        for bp_item in bp.search_optimization:
            self.engine.execute_unsafe_ddl(
                "ALTER TABLE {full_name:i} ADD SEARCH OPTIMIZATION ON {method:r}({target:i})",
                {
                    "full_name": bp.full_name,
                    "method": bp_item.method,
                    "target": bp_item.target,
                },
                condition=condition,
            )

    def _compare_search_optimization(self, bp: TableBlueprint, is_search_optimization_enabled=False, condition=True):
        # Legacy search optimization on an entire table
        if isinstance(bp.search_optimization, bool):
            if bp.search_optimization and not is_search_optimization_enabled:
                self.engine.execute_unsafe_ddl(
                    "ALTER TABLE {full_name:i} ADD SEARCH OPTIMIZATION",
                    {
                        "full_name": bp.full_name,
                    },
                    condition=condition,
                )

                return True

            elif not bp.search_optimization and is_search_optimization_enabled:
                self.engine.execute_unsafe_ddl(
                    "ALTER TABLE {full_name:i} DROP SEARCH OPTIMIZATION",
                    {
                        "full_name": bp.full_name,
                    },
                    condition=condition,
                )

                return True

            return False

        # Detailed search optimization on specific columns
        existing_search_optimization_items = []
        result = False

        cur = self.engine.execute_meta(
            "DESC SEARCH OPTIMIZATION ON {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        for r in cur:
            t_parts = r["target"].split(":", 2)

            existing_search_optimization_items.append(
                SearchOptimizationItem(
                    method=r["method"],
                    target=Ident(t_parts[0]),
                )
            )

        for bp_item in bp.search_optimization:
            if bp_item not in existing_search_optimization_items:
                self.engine.execute_unsafe_ddl(
                    "ALTER TABLE {full_name:i} ADD SEARCH OPTIMIZATION ON {method:r}({target:i})",
                    {
                        "full_name": bp.full_name,
                        "method": bp_item.method,
                        "target": bp_item.target,
                    },
                    condition=condition,
                )

                result = True

        for ex_item in existing_search_optimization_items:
            if ex_item not in bp.search_optimization:
                self.engine.execute_unsafe_ddl(
                    "ALTER TABLE {full_name:i} DROP SEARCH OPTIMIZATION ON {method:r}({target:i})",
                    {
                        "full_name": bp.full_name,
                        "method": ex_item.method,
                        "target": ex_item.target,
                    },
                    condition=condition,
                )

                result = True

        return result

    def _normalize_bp_default(self, bp_default):
        if isinstance(bp_default, SchemaObjectIdent):
            # This cannot be formatted properly with double-quotes ("), Snowflake strips it from DEFAULT value returned by DESC TABLE
            return f"{bp_default}.NEXTVAL"

        return bp_default
