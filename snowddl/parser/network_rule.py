from snowddl.blueprint import NetworkRuleBlueprint, SchemaObjectIdent
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
network_rule_json_schema = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string"
        },
        "value_list": {
            "type": "array",
            "items": {
                "type": "string"
            },
        },
        "mode": {
            "type": "string"
        },
        "grants": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                # OIE patch (#10): minItems 0 -- an EMPTY list is the declaration that
                # carries the point, "no role holds this privilege", so it must be
                # expressible. Patch #8 used minItems 1 and could not say it.
                "minItems": 0
            }
        },
        "comment": {
            "type": "string"
        },
    },
    "required": ["type", "mode"],
    "additionalProperties": False
}
# fmt: on


class NetworkRuleParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files("network_rule", network_rule_json_schema, self.process_sequence)

    def process_sequence(self, f: ParsedFile):
        bp = NetworkRuleBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            type=str(f.params["type"]).upper(),
            value_list=[str(v) for v in f.params.get("value_list", [])],
            mode=str(f.params["mode"]).upper(),
            grants=f.params.get("grants"),
            comment=f.params.get("comment"),
        )

        self.config.add_blueprint(bp)
