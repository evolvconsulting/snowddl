from abc import abstractmethod
from typing import Dict

from snowddl.blueprint import AccountObjectIdent, AbstractBlueprint, SchemaBlueprint
from snowddl.resolver.abc_resolver import AbstractResolver, ResolveResult, ObjectType


class AbstractSchemaObjectResolver(AbstractResolver):
    # OIE patch (#10): object types that carry no grants of their own. Constraints are
    # properties of a table, not grantable objects, so a `grants:` block on one is
    # meaningless rather than merely unused.
    NON_GRANTABLE_OBJECT_TYPES = frozenset(
        {
            ObjectType.PRIMARY_KEY,
            ObjectType.UNIQUE_KEY,
            ObjectType.FOREIGN_KEY,
            ObjectType.CHECK_CONSTRAINT,
        }
    )

    def _create_object_entry_point(self, bp: AbstractBlueprint) -> ResolveResult:
        # OIE patch (#10): the object does not exist yet, so there are no live grants to
        # read -- and under `plan` the CREATE never executes, so SHOW GRANTS would run
        # against a missing object and abort the whole plan.
        #
        # OIE patch (apply-revision-guard): THIS OVERRIDE DOES NOT CALL super(), so the
        # base class's guard call does not reach any schema object -- which is nearly
        # every object there is. The call is repeated here on purpose, and
        # test_every_entry_point_override_calls_the_revision_guard asserts that any
        # FUTURE override does the same. That test is the whole reason this duplication
        # is safe.
        self._revision_guard_check(bp)
        result = self.create_object(bp)
        self._revision_guard_record(bp, result)
        self._reconcile_object_grants(bp, object_exists=False)

        return result

    def _compare_object_entry_point(self, bp: AbstractBlueprint, row: Dict) -> ResolveResult:
        # OIE patch (apply-revision-guard): see _create_object_entry_point above.
        self._revision_guard_check(bp)
        result = self.compare_object(bp, row)
        self._revision_guard_record(bp, result)

        # Reconciled AFTER compare_object, deliberately. For the object types whose
        # CREATE OR REPLACE cannot COPY GRANTS (procedures, functions, streams, tasks),
        # the replace drops the grants, so reading them before it would see a set that no
        # longer exists a moment later. Running after means apply re-grants what the
        # replace dropped, which is what patch #8 did by hand for two object types.
        #
        # Grants are reconciled on EVERY compare, not only when the object's DDL changed:
        # a hand-issued grant leaves the object definition untouched, so a check gated on
        # REPLACE/ALTER would never see the drift it exists to catch. Reported as ALTER so
        # `plan --detailed-exitcode` is non-zero and a conformance gate goes red.
        if self._reconcile_object_grants(bp, object_exists=True) and result == ResolveResult.NOCHANGE:
            result = ResolveResult.ALTER

        return result

    def _reconcile_object_grants(self, bp: AbstractBlueprint, object_exists: bool) -> bool:
        # OIE patch (#10): reconcile object-level grants for ANY schema object type.
        # Returns True if a GRANT or REVOKE was issued. Generalizes patch #9 (tables) and
        # supersedes patch #8 (procedures/functions), which was additive-only.
        #
        # AUTHORITATIVE, BUT ONLY FOR THE PRIVILEGES NAMED IN CONFIG. For each privilege
        # that appears as a key, the declared role list becomes the live grantee list --
        # missing roles are granted, extra roles are revoked, and an empty list means
        # nobody holds it. A privilege that does NOT appear as a key is not touched.
        #
        # That scoping is what makes this usable where another tool owns the broad grants.
        # OIE grants SELECT on MDM tables through Terraform database-wide ALL + FUTURE
        # resources and through migrations; reconciling the whole grant set would revoke
        # them and start a permanent revoke/re-grant fight. Naming only the privileges a
        # config means to own leaves every other tier alone by construction.
        #
        # OWNERSHIP is skipped (transferred, not granted) and non-ROLE grantees are
        # skipped (user grants belong to SCIM) -- same exclusions as patch #8.
        grants = getattr(bp, "grants", None)

        if not grants:
            return False

        object_type = self.get_object_type()

        if object_type in self.NON_GRANTABLE_OBJECT_TYPES:
            return False

        live_grantees: Dict[str, set] = {}

        if object_exists:
            cur = self.engine.execute_meta(
                "SHOW GRANTS ON {object_type:r} {full_name:i}",
                {
                    "object_type": object_type.singular_for_grant,
                    "full_name": bp.full_name,
                },
            )

            for r in cur:
                if r["granted_to"] != "ROLE" or r["privilege"] == "OWNERSHIP":
                    continue

                live_grantees.setdefault(r["privilege"], set()).add(r["grantee_name"])

        is_changed = False

        for privilege, roles in grants.items():
            privilege = str(privilege).upper()

            # Declared names are bare; live names carry the env prefix. Compare in the
            # live namespace but keep the bare name, so the GRANT re-prefixes exactly once
            # -- re-wrapping an already-prefixed name would double the prefix.
            desired = {str(AccountObjectIdent(self.config.env_prefix, role)): role for role in roles}
            current = live_grantees.get(privilege, set())

            for full_role_name in sorted(set(desired) - current):
                self.engine.execute_safe_ddl(
                    "GRANT {privilege:r} ON {object_type:r} {full_name:i} TO ROLE {role:i}",
                    {
                        "privilege": privilege,
                        "object_type": object_type.singular_for_grant,
                        "full_name": bp.full_name,
                        "role": AccountObjectIdent(self.config.env_prefix, desired[full_role_name]),
                    },
                )

                is_changed = True

            # REVOKE is the destructive direction, so it goes through unsafe DDL and is
            # gated by --apply-unsafe like every other removal in SnowDDL. It is still
            # PRINTED by plan either way, so a run that is not permitted to fix the drift
            # still reports it.
            for full_role_name in sorted(current - set(desired)):
                self.engine.execute_unsafe_ddl(
                    "REVOKE {privilege:r} ON {object_type:r} {full_name:i} FROM ROLE {role:i}",
                    {
                        "privilege": privilege,
                        "object_type": object_type.singular_for_grant,
                        "full_name": bp.full_name,
                        # Already env-prefixed by Snowflake; do not prefix again.
                        "role": AccountObjectIdent("", full_role_name),
                    },
                )

                is_changed = True

        return is_changed

    def get_existing_objects(self):
        existing_objects = {}

        # Process schemas in parallel
        for schema_objects in self.engine.executor.map(
            self.get_existing_objects_in_schema, self.engine.schema_cache.schemas.values()
        ):
            existing_objects.update(schema_objects)

        return existing_objects

    @abstractmethod
    def get_existing_objects_in_schema(self, schema: dict):
        pass

    def _is_unmanaged_blueprint(self, full_name: str) -> bool:
        # D-218, re-keyed by ADR-005: skip create/compare of any schema object whose
        # parent schema is flagged is_unmanaged (recognize-but-not-managed), so a
        # declared child in another tier's schema (e.g. OPS.SP_PROVISION_REHEARSAL_CLONE)
        # is never CREATE-OR-REPLACE'd by a role that does not own OPS.
        #
        # The is_sandbox drop-skip in _resolve_drop below is deliberately NOT this flag.
        # A schema wanting "never drop what I did not declare" while still deploying its
        # own declared objects carries is_sandbox alone; one wanting both carries both.
        schema_full_name = ".".join(full_name.split(".")[:2])
        schema_bp = self.config.get_blueprints_by_type(SchemaBlueprint).get(schema_full_name)
        return bool(schema_bp is not None and getattr(schema_bp, "is_unmanaged", False))

    def _resolve_drop(self):
        tasks = {}

        for object_full_name in sorted(self.existing_objects):
            # Object exists in blueprints, should not be dropped
            if object_full_name in self.blueprints:
                continue

            # Another object is going to be dropped, which implicitly drops this object
            if self._check_implicit_drop_intention(object_full_name):
                continue

            schema_full_name = ".".join(object_full_name.split(".")[:2])
            schema_bp = self.config.get_blueprints_by_type(SchemaBlueprint).get(schema_full_name)

            # Object schema does not exist in blueprints, object will be dropped automatically on DROP DATABASE or DROP SCHEMA
            if schema_bp is None:
                continue

            # Objects without blueprints are allowed in sandbox schemas, should not be dropped
            if schema_bp.is_sandbox:
                continue

            tasks[object_full_name] = (self.drop_object, self.existing_objects[object_full_name])

        self._process_tasks(tasks)
