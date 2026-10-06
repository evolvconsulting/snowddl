from snowddl.blueprint import FunctionBlueprint, ViewBlueprint
from snowddl.validator.abc_validator import AbstractValidator


class FunctionValidator(AbstractValidator):
    # evolv patch: a function's depends_on names the views its body reads. Only views:
    # ViewDependentFunctionResolver runs after ViewResolver and nothing else, so an edge
    # to any other type would be accepted and then ordered by luck.
    def get_blueprints(self):
        return self.config.get_blueprints_by_type(FunctionBlueprint)

    def validate_blueprint(self, bp: FunctionBlueprint):
        self._validate_depends_on(bp)

    def _validate_depends_on(self, bp: FunctionBlueprint):
        for depends_on_name in bp.depends_on:
            if depends_on_name not in self.config.get_blueprints_by_type(ViewBlueprint):
                raise ValueError(
                    f"Function [{bp.full_name}] depends on view " f"[{depends_on_name}] which does not exist in config"
                )
