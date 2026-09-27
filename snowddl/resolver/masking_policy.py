from json import loads

from snowddl.blueprint import MaskingPolicyBlueprint, ObjectType, Edition, SchemaObjectIdent
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver, ResolveResult
from snowddl.resolver import policy_signature


class MaskingPolicyResolver(AbstractSchemaObjectResolver):
    skip_on_empty_blueprints = True
    skip_min_edition = Edition.ENTERPRISE

    def get_object_type(self) -> ObjectType:
        return ObjectType.MASKING_POLICY

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW MASKING POLICIES IN SCHEMA {database:i}.{schema:i}",
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
                "options": loads(r["options"]) if r["options"] else {},
                "comment": r["comment"] if r["comment"] else None,
            }

        return existing_objects

    def get_blueprints(self):
        return self.config.get_blueprints_by_type(MaskingPolicyBlueprint)

    def create_object(self, bp: MaskingPolicyBlueprint):
        self._create_policy(bp)
        self._apply_policy_refs(bp, skip_existing=True)

        return ResolveResult.CREATE

    def compare_object(self, bp: MaskingPolicyBlueprint, row: dict):
        cur = self.engine.execute_meta(
            "DESC MASKING POLICY {full_name:i}",
            {
                "full_name": bp.full_name,
            },
        )

        r = cur.fetchone()

        # If signature or return type was changed, policy and all references must be dropped and created again
        if (
            r["signature"] != policy_signature.signature(bp.arguments)
            or r["return_type"] != policy_signature.return_type(bp.returns)
            or row["options"].get("EXEMPT_OTHER_POLICIES", False) != bp.exempt_other_policies
        ):
            self._drop_policy_refs(bp.full_name)
            self._drop_policy(bp.full_name)

            self._create_policy(bp)
            self._apply_policy_refs(bp, skip_existing=True)

            return ResolveResult.REPLACE

        result = ResolveResult.NOCHANGE

        if self._apply_policy_refs(bp):
            result = ResolveResult.ALTER

        if r["body"] != bp.body:
            self.engine.execute_unsafe_ddl(
                "ALTER MASKING POLICY {full_name:i} SET BODY -> {body:r}",
                {
                    "full_name": bp.full_name,
                    "body": bp.body,
                },
                condition=self.engine.settings.execute_masking_policy,
            )

            result = ResolveResult.ALTER

        if row["comment"] != bp.comment:
            self.engine.execute_unsafe_ddl(
                "ALTER MASKING POLICY {full_name:i} SET COMMENT = {comment}",
                {
                    "full_name": bp.full_name,
                    "comment": bp.comment,
                },
                condition=self.engine.settings.execute_masking_policy,
            )

            result = ResolveResult.ALTER

        return result

    def drop_object(self, row: dict):
        self._drop_policy_refs(SchemaObjectIdent("", row["database"], row["schema"], row["name"]))
        self._drop_policy(SchemaObjectIdent("", row["database"], row["schema"], row["name"]))

        return ResolveResult.DROP

    def _create_policy(self, bp: MaskingPolicyBlueprint):
        query = self.engine.query_builder()

        query.append(
            "CREATE MASKING POLICY {full_name:i} AS (",
            {
                "full_name": bp.full_name,
            },
        )

        for idx, arg in enumerate(bp.arguments):
            query.append_nl(
                "    {comma:r}{arg_name:i} {arg_type:r}",
                {
                    "comma": "  " if idx == 0 else ", ",
                    "arg_name": arg.name,
                    "arg_type": arg.type,
                },
            )

        query.append_nl(")")

        query.append_nl(
            "RETURNS {ret_type:r} -> ",
            {
                "ret_type": bp.returns,
            },
        )

        query.append_nl(
            "{body:r}",
            {
                "body": bp.body,
            },
        )

        if bp.exempt_other_policies:
            query.append_nl("EXEMPT_OTHER_POLICIES = TRUE")

        if bp.comment:
            query.append_nl(
                "COMMENT = {comment}",
                {
                    "comment": bp.comment,
                },
            )

        self.engine.execute_unsafe_ddl(query, condition=self.engine.settings.execute_masking_policy)

    def _drop_policy(self, policy: SchemaObjectIdent):
        self.engine.execute_unsafe_ddl(
            "DROP MASKING POLICY {full_name:i}", {"full_name": policy}, condition=self.engine.settings.execute_masking_policy
        )

    def _apply_policy_refs(self, bp: MaskingPolicyBlueprint, skip_existing=False):
        existing_policy_refs = {} if skip_existing else self._get_existing_policy_refs(bp.full_name)
        applied_change = False

        for ref in bp.references:
            ref_key = f"{ref.object_type.name}|{ref.object_name}|{ref.columns[0]}"

            # Policy was applied before
            if ref_key in existing_policy_refs:
                del existing_policy_refs[ref_key]
                continue

            # Apply new masking policy.
            # OIE fork (0.67.5-oie.14): if the column already carries a DIFFERENT masking
            # policy, the column's policy is changing. Replace it in one statement with
            # FORCE, so no moment exists where the column carries no policy. Upstream
            # emitted a plain SET, which Snowflake refuses while another policy is set, and
            # the old policy's resolver UNSET the column on its own schedule: an exposure
            # window, or a failed apply, depending on which resolver ran first.
            self.engine.execute_unsafe_ddl(
                "ALTER {object_type:r} {object_name:i} MODIFY COLUMN {first_column:i} SET MASKING POLICY {policy_name:i} USING ({columns:i}){force:r}",
                {
                    "object_type": ref.object_type.singular_for_ref,
                    "object_name": ref.object_name,
                    "policy_name": bp.full_name,
                    "first_column": ref.columns[0],
                    "columns": ref.columns,
                    "force": " FORCE" if self._column_has_other_masking_policy(bp, ref) else "",
                },
                condition=self.engine.settings.execute_masking_policy,
            )

            applied_change = True

        # Remove remaining policy references which no longer exist in blueprint
        for existing_ref in existing_policy_refs.values():
            # OIE fork (0.67.5-oie.14): a column another masking-policy blueprint now
            # claims is moving to that policy, which replaces this one with FORCE. An
            # UNSET here would leave it unmasked until that resolver runs, so skip it.
            if self._is_claimed_by_other_blueprint(bp, existing_ref):
                continue

            self.engine.execute_unsafe_ddl(
                "ALTER {object_type:r} {database:i}.{schema:i}.{name:i} MODIFY COLUMN {first_column:i} UNSET MASKING POLICY",
                {
                    "object_type": ObjectType[existing_ref["object_type"]].singular_for_ref,
                    "database": existing_ref["database"],
                    "schema": existing_ref["schema"],
                    "name": existing_ref["name"],
                    "first_column": existing_ref["first_column"],
                },
                condition=self.engine.settings.execute_masking_policy,
            )

            applied_change = True

        return applied_change

    def _column_has_other_masking_policy(self, bp: MaskingPolicyBlueprint, ref) -> bool:
        # OIE fork (0.67.5-oie.14). Metadata read only, and only for a reference about to
        # be SET, so an apply with no new reference issues no extra query.
        cur = self.engine.execute_meta(
            "SELECT * FROM TABLE(snowflake.information_schema.policy_references(ref_entity_name => {ref_entity_name}, ref_entity_domain => {ref_entity_domain}))",
            {
                "ref_entity_name": str(ref.object_name),
                "ref_entity_domain": ref.object_type.singular_for_ref.lower(),
            },
        )

        for r in cur:
            if r["POLICY_KIND"] != "MASKING_POLICY" or r["REF_COLUMN_NAME"] != str(ref.columns[0]):
                continue

            if f"{r['POLICY_DB']}.{r['POLICY_SCHEMA']}.{r['POLICY_NAME']}" != str(bp.full_name):
                return True

        return False

    def _is_claimed_by_other_blueprint(self, bp: MaskingPolicyBlueprint, existing_ref: dict) -> bool:
        # OIE fork (0.67.5-oie.14). True when another masking-policy blueprint declares a
        # reference on the same object and first column.
        existing = f"{existing_ref['database']}.{existing_ref['schema']}.{existing_ref['name']}|{existing_ref['first_column']}"

        for other in self.get_blueprints().values():
            if str(other.full_name) == str(bp.full_name):
                continue

            for ref in other.references:
                if f"{ref.object_name}|{ref.columns[0]}" == existing:
                    return True

        return False

    def _drop_policy_refs(self, policy_name: SchemaObjectIdent):
        existing_policy_refs = self._get_existing_policy_refs(policy_name)

        for existing_ref in existing_policy_refs.values():
            self.engine.execute_unsafe_ddl(
                "ALTER {object_type:r} {database:i}.{schema:i}.{name:i} MODIFY COLUMN {first_column:i} UNSET MASKING POLICY",
                {
                    "object_type": ObjectType[existing_ref["object_type"]].singular_for_ref,
                    "database": existing_ref["database"],
                    "schema": existing_ref["schema"],
                    "name": existing_ref["name"],
                    "first_column": existing_ref["first_column"],
                },
                condition=self.engine.settings.execute_masking_policy,
            )

    def _get_existing_policy_refs(self, policy_name: SchemaObjectIdent):
        existing_policy_refs = {}

        cur = self.engine.execute_meta(
            "SELECT * FROM TABLE(snowflake.information_schema.policy_references(policy_name => {policy_name}))",
            {
                "policy_name": policy_name,
            },
        )

        for r in cur:
            ref_key = f"{r['REF_ENTITY_DOMAIN']}|{r['REF_DATABASE_NAME']}.{r['REF_SCHEMA_NAME']}.{r['REF_ENTITY_NAME']}|{r['REF_COLUMN_NAME']}"

            existing_policy_refs[ref_key] = {
                "object_type": r["REF_ENTITY_DOMAIN"],
                "database": r["REF_DATABASE_NAME"],
                "schema": r["REF_SCHEMA_NAME"],
                "name": r["REF_ENTITY_NAME"],
                "first_column": r["REF_COLUMN_NAME"],
            }

        return existing_policy_refs
