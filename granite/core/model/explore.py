from .base import GraniteBase
from .join import Join


class Explore(GraniteBase):
    def __init__(self, definition: dict = {}, project=None) -> None:
        if definition.get("from") is not None:
            definition["from_"] = definition["from"]
        else:
            definition["from_"] = definition["name"]

        self.project = project
        self.validate(definition)
        super().__init__(definition)

    def validate(self, definition: dict):
        required_keys = ["name", "model", "from_"]
        for k in required_keys:
            if k not in definition:
                raise ValueError(f"Explore missing required key {k}")

    def view_names(self):
        return [self.from_] + [j.name for j in self.joins()]

    def joins(self):
        output = []
        for j in self._definition.get("joins", []):
            join = Join({**j, "explore_from": self.from_})
            if join.is_valid():
                output.append(join)
        return output
