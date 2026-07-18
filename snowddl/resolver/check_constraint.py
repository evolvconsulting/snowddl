from re import compile, IGNORECASE

from snowddl.blueprint import CheckConstraintBlueprint
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver, ResolveResult, ObjectType


# Matches the start of an out-of-line named CHECK constraint as rendered by GET_DDL('TABLE', ...):
#     constraint <NAME> check (<expression>)
# The expression itself is extracted separately via balanced-parenthesis scanning, since it may
# contain parentheses (e.g. IN (...)) and string literals.
check_constraint_re = compile(
    r"constraint\s+(?P<name>\"(?:[^\"]|\"\")+\"|[A-Za-z_][A-Za-z0-9_$]*)\s+check\s*\(",
    IGNORECASE,
)


def parse_check_constraints(ddl_text: str):
    # Extract named CHECK constraints from GET_DDL('TABLE', ...) output as {constraint_name: expression}.
    # The expression is extracted via a balanced-parenthesis scan, ignoring parens inside string literals.
    constraints = {}

    for m in check_constraint_re.finditer(ddl_text):
        name = m.group("name")

        # Unquote double-quoted constraint name
        if name.startswith('"'):
            name = name[1:-1].replace('""', '"')

        open_paren_idx = m.end() - 1
        depth = 0
        in_string = False
        i = open_paren_idx

        while i < len(ddl_text):
            ch = ddl_text[i]

            if in_string:
                if ch == "'":
                    # Escaped single quote inside a string literal
                    if i + 1 < len(ddl_text) and ddl_text[i + 1] == "'":
                        i += 2
                        continue
                    in_string = False
            else:
                if ch == "'":
                    in_string = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break

            i += 1

        constraints[name] = ddl_text[open_paren_idx + 1 : i].strip()

    return constraints


class CheckConstraintResolver(AbstractSchemaObjectResolver):
    def get_object_type(self) -> ObjectType:
        return ObjectType.CHECK_CONSTRAINT

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        # Snowflake does NOT expose CHECK constraints via SHOW or INFORMATION_SCHEMA in a reliable way,
        # but GET_DDL('TABLE', ...) always renders them. Enumerate base tables, then read each table DDL.
        cur = self.engine.execute_meta(
            "SHOW TABLES IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        table_names = []

        for r in cur:
            # Skip other table types which do not support CHECK constraints authored via this resolver
            if (
                r.get("is_external") == "Y"
                or r.get("is_event") == "Y"
                or r.get("is_hybrid") == "Y"
                or r.get("is_iceberg") == "Y"
                or r.get("is_dynamic") == "Y"
            ):
                continue

            table_names.append((r["database_name"], r["schema_name"], r["name"]))

        for database_name, schema_name, table_name in table_names:
            full_table_name = f"{database_name}.{schema_name}.{table_name}"

            ddl_cur = self.engine.execute_meta(
                "SELECT GET_DDL('TABLE', {full_table_name}, TRUE) AS ddl_text",
                {
                    "full_table_name": full_table_name,
                },
            )

            ddl_row = ddl_cur.fetchone()

            if not ddl_row:
                continue

            ddl_text = ddl_row["DDL_TEXT"]

            for constraint_name, expression in parse_check_constraints(ddl_text).items():
                full_name = f"{full_table_name}({constraint_name})"

                existing_objects[full_name] = {
                    "database": database_name,
                    "schema": schema_name,
                    "table": table_name,
                    "constraint_name": constraint_name,
                    "expression": expression,
                }

        return existing_objects

    def get_blueprints(self):
        return self.config.get_blueprints_by_type(CheckConstraintBlueprint)

    def create_object(self, bp: CheckConstraintBlueprint):
        self.engine.execute_safe_ddl(
            # ENABLE NOVALIDATE is required: Snowflake rejects ALTER TABLE ADD CHECK with the default
            # ENABLE VALIDATE. NOVALIDATE still enforces the constraint for new DML (the OIE behavior),
            # it only skips validation of pre-existing rows. GET_DDL renders this identically to an
            # inline CHECK, so create/compare round-trips are idempotent (verified live on OIE).
            "ALTER TABLE {table_name:i} ADD CONSTRAINT {constraint_name:i} CHECK ({expression:r}) ENABLE NOVALIDATE",
            {
                "table_name": bp.table_name,
                "constraint_name": bp.full_name.constraint_name,
                "expression": bp.expression,
            },
        )

        return ResolveResult.CREATE

    def compare_object(self, bp: CheckConstraintBlueprint, row: dict):
        # NB: the blueprint expression must match Snowflake's normalized GET_DDL rendering; only
        # whitespace is normalized here, so authored expressions should follow the live rendered form.
        if self._normalise_expression(bp.expression) == self._normalise_expression(row["expression"]):
            return ResolveResult.NOCHANGE

        self.engine.execute_safe_ddl(
            "ALTER TABLE {table_name:i} DROP CONSTRAINT {constraint_name:i}",
            {
                "table_name": bp.table_name,
                "constraint_name": bp.full_name.constraint_name,
            },
        )

        self.engine.execute_safe_ddl(
            "ALTER TABLE {table_name:i} ADD CONSTRAINT {constraint_name:i} CHECK ({expression:r}) ENABLE NOVALIDATE",
            {
                "table_name": bp.table_name,
                "constraint_name": bp.full_name.constraint_name,
                "expression": bp.expression,
            },
        )

        return ResolveResult.ALTER

    def drop_object(self, row: dict):
        self.engine.execute_safe_ddl(
            "ALTER TABLE {database:i}.{schema:i}.{table:i} DROP CONSTRAINT {constraint_name:i}",
            {
                "database": row["database"],
                "schema": row["schema"],
                "table": row["table"],
                "constraint_name": row["constraint_name"],
            },
        )

        return ResolveResult.DROP

    def _normalise_expression(self, expression: str) -> str:
        return " ".join(expression.split())
