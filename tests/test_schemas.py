import json
import unittest
from importlib.resources import files

from jsonschema import Draft202012Validator


class SchemaTests(unittest.TestCase):
    def test_all_schemas_are_valid_draft_2020_12(self):
        schema_dir = files("course_pr_reviewer").joinpath("schemas")
        for name in (
            "ai-review.schema.json",
            "course-review.schema.json",
            "students.schema.json",
            "review-result.schema.json",
            "vision-review.schema.json",
        ):
            with self.subTest(name=name):
                schema = json.loads(
                    schema_dir.joinpath(name).read_text(encoding="utf-8")
                )
                Draft202012Validator.check_schema(schema)


if __name__ == "__main__":
    unittest.main()
