"""Finite local schema subset; no third-party package or network resolution."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'schema_subject', Path(__file__).resolve().parents[1] / 'scripts/schema_validation.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class SchemaValidationTests(unittest.TestCase):
    def valid(self, value, schema):
        self.assertIsNone(s.validate_instance(value, schema))

    def invalid(self, value, schema):
        with self.assertRaises(s.SchemaError):
            s.validate_instance(value, schema)

    def bad_schema(self, schema):
        with self.assertRaises(s.SchemaError):
            s.validate_schema(schema)

    def test_object_contract_checks_fields_and_extra_data(self):
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'},
                  'count': {'type': 'integer'}}, 'required': ['answer'], 'additionalProperties': False}
        self.valid({'answer': 'yes', 'count': 2}, schema)
        self.invalid({'count': 2}, schema)
        self.invalid({'answer': 4}, schema)
        self.invalid({'answer': 'yes', 'unexpected': True}, schema)

    def test_additional_properties_can_have_a_schema(self):
        schema = {'properties': {'name': True}, 'additionalProperties': {'type': 'integer'}}
        self.valid({'name': None, 'extra': 1}, schema)
        self.invalid({'extra': 'one'}, schema)

    def test_keywords_do_not_imply_a_type(self):
        self.valid('anything', {'required': ['name'], 'minimum': 3})
        self.valid(123, {'minLength': 5})
        self.valid(None, {'properties': {'x': False}})

    def test_all_json_types_and_type_union(self):
        for kind, value in [('null', None), ('boolean', True), ('object', {}),
                            ('array', []), ('number', 1.5), ('integer', 1), ('string', 'x')]:
            with self.subTest(kind=kind):
                self.valid(value, {'type': kind})
        self.valid(None, {'type': ['integer', 'null']})
        self.invalid('1', {'type': ['integer', 'null']})

    def test_integer_includes_integral_floats_but_excludes_booleans(self):
        self.valid(1.0, {'type': 'integer'})
        self.valid(-2.0, {'type': 'integer'})
        self.invalid(1.5, {'type': 'integer'})
        self.invalid(True, {'type': 'integer'})
        self.invalid(False, {'type': 'number'})

    def test_numeric_inclusive_and_exclusive_bounds(self):
        schema = {'type': 'number', 'minimum': 1, 'maximum': 3}
        self.valid(1, schema)
        self.valid(3, schema)
        self.invalid(0, schema)
        self.invalid(4, schema)
        self.invalid(1, {'exclusiveMinimum': 1})
        self.invalid(3, {'exclusiveMaximum': 3})
        self.valid(2, {'exclusiveMinimum': 1, 'exclusiveMaximum': 3})

    def test_multiple_of_uses_decimal_arithmetic(self):
        self.valid(4.02, {'multipleOf': .01})
        self.valid(.3, {'multipleOf': .1})
        self.valid(-6, {'multipleOf': 3})
        self.valid(0, {'multipleOf': .3})
        self.invalid(4.021, {'multipleOf': .01})

    def test_string_lengths_count_unicode_characters(self):
        self.valid('😀', {'minLength': 1, 'maxLength': 1})
        self.invalid('', {'minLength': 1})
        self.invalid('ab', {'maxLength': 1})

    def test_pattern_search_is_unanchored_unless_requested(self):
        self.valid('xabcx', {'pattern': 'abc'})
        self.invalid('xabcx', {'pattern': '^abc$'})
        self.valid('123', {'pattern': r'^\d+$'})
        self.invalid('١٢٣', {'pattern': r'^\d+$'})

    def test_pattern_dot_and_dollar_obey_common_ecmascript_semantics(self):
        for ending in ['\n', '\r', '\u2028', '\u2029']:
            with self.subTest(ending=repr(ending)):
                self.invalid('abc' + ending, {'pattern': '^abc$'})
                self.invalid(ending, {'pattern': '^.$'})
        self.valid('x', {'pattern': '^.$'})
        self.valid('.', {'pattern': r'^\.$'})

    def test_array_items_size_and_boolean_items(self):
        schema = {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 2}
        self.valid([1, 2], schema)
        self.invalid([], schema)
        self.invalid([1, 2, 3], schema)
        self.invalid([True], schema)
        self.valid([], {'items': False})
        self.invalid([1], {'items': False})

    def test_unique_items_uses_json_equality_not_python_equality(self):
        self.valid([True, 1, False, 0], {'uniqueItems': True})
        self.invalid([1, 1.0], {'uniqueItems': True})
        self.invalid([{'a': [1]}, {'a': [1.0]}], {'uniqueItems': True})
        self.valid([{'a': [True]}, {'a': [1]}], {'uniqueItems': True})

    def test_enum_and_const_use_deep_json_equality(self):
        self.valid(1.0, {'enum': [1, 2]})
        self.invalid(True, {'enum': [1]})
        self.valid({'a': [1.0]}, {'const': {'a': [1]}})
        self.invalid({'a': [True]}, {'const': {'a': [1]}})
        self.valid(None, {'const': None})

    def test_boolean_combinations(self):
        self.valid(2, {'anyOf': [{'type': 'string'}, {'type': 'integer'}]})
        self.invalid(None, {'anyOf': [{'type': 'string'}, {'type': 'integer'}]})
        self.valid(2, {'allOf': [{'minimum': 1}, {'maximum': 3}]})
        self.invalid(4, {'allOf': [{'minimum': 1}, {'maximum': 3}]})
        self.valid('two', {'oneOf': [{'type': 'string'}, {'type': 'number'}]})
        self.invalid(2, {'oneOf': [{'type': 'integer'}, {'type': 'number'}]})
        self.invalid(2, {'not': {'type': 'number'}})
        self.valid('two', {'not': {'type': 'number'}})

    def test_local_ref_and_sibling_constraints_both_apply(self):
        schema = {'$defs': {'x': {'type': 'number'}}, '$ref': '#/$defs/x', 'minimum': 3}
        self.valid(3, schema)
        self.invalid(2, schema)
        self.invalid('3', schema)

    def test_definitions_and_escaped_json_pointer(self):
        schema = {'definitions': {'a/b~c': {'const': 'value'}}, '$ref': '#/definitions/a~1b~0c'}
        self.valid('value', schema)
        self.invalid('other', schema)
        self.valid(3, {'$defs': {'a b': {'type': 'integer'}}, '$ref': '#/$defs/a%20b'})

    def test_recursive_ref_can_progress_through_instance_children(self):
        schema = {'type': 'object', 'properties': {'child': {'$ref': '#'}}, 'additionalProperties': False}
        self.valid({'child': {'child': {}}}, schema)
        self.invalid({'child': {'invalid': 1}}, schema)

    def test_nonprogressing_recursion_is_not_swallowed_by_combinators(self):
        for schema in [{'$ref': '#'}, {'not': {'$ref': '#'}},
                       {'anyOf': [{'$ref': '#'}, True]}, {'oneOf': [{'$ref': '#'}, True]}]:
            with self.subTest(schema=schema):
                self.invalid(1, schema)

    def test_boolean_schemas(self):
        self.valid({'any': ['JSON']}, True)
        self.invalid(None, False)

    def test_annotations_do_not_coerce_or_apply_defaults(self):
        schema = {'title': 'x', 'description': 'y', '$schema': 'https://json-schema.org/draft/2020-12/schema',
                  'default': 5, 'examples': [1, 2]}
        self.valid(None, schema)
        self.valid('not a default', schema)

    def test_unsupported_keywords_are_rejected_even_in_unused_branches(self):
        for keyword, value in [('format', 'email'), ('patternProperties', {}), ('if', True),
                               ('unevaluatedProperties', False), ('$id', 'urn:example'), ('typo', True)]:
            with self.subTest(keyword=keyword):
                self.bad_schema({keyword: value})
                self.bad_schema({'anyOf': [True, {keyword: value}]})
                self.bad_schema({'$defs': {'unused': {keyword: value}}})

    def test_malformed_schema_containers_and_constraints_are_rejected(self):
        cases = [None, [], {'type': []}, {'type': ['number', 'number']}, {'type': 3},
                 {'required': ['x', 'x']}, {'required': [1]}, {'properties': []},
                 {'properties': {'x': 1}}, {'items': [True]}, {'enum': []}, {'enum': [1, 1.0]},
                 {'minItems': True}, {'maxLength': -1}, {'minimum': True}, {'multipleOf': 0},
                 {'exclusiveMaximum': False}, {'uniqueItems': 'yes'}, {'anyOf': []},
                 {'allOf': [None]}, {'not': 'no'}, {'additionalProperties': []},
                 {'title': 1}, {'examples': {}}, {'pattern': '['}]
        for schema in cases:
            with self.subTest(schema=schema):
                self.bad_schema(schema)

    def test_unsupported_regex_features_are_rejected_explicitly(self):
        for pattern in [r'\p{Letter}', r'\s', '(?P<name>x)', '(?i)x', '(x)+', r'\1', 'a++', 'a{2,}+', '(?>a)']:
            with self.subTest(pattern=pattern):
                self.bad_schema({'pattern': pattern})

    def test_remote_refs_anchors_and_invalid_local_refs_are_rejected(self):
        for reference in ['https://example.invalid/schema', 'file:///schema', '#name',
                          '#/$defs/missing', '#/wrong~2escape', '#/$defs/%FF', '#/$defs/%ZZ', 3]:
            with self.subTest(reference=reference):
                self.bad_schema({'$ref': reference})
        self.bad_schema({'default': 3, '$ref': '#/default'})

    def test_nonfinite_numbers_and_python_only_values_are_rejected_everywhere(self):
        for value in [float('nan'), float('inf'), -float('inf'), (1, 2), {1: 'x'}, set()]:
            with self.subTest(value=repr(value)):
                self.invalid(value, True)
                self.bad_schema({'default': value})
        self.bad_schema({'minimum': float('inf')})

    def test_cyclic_python_input_is_rejected_without_recursion_error(self):
        value = []
        value.append(value)
        self.invalid(value, True)
        schema = {'properties': {}}
        schema['properties']['x'] = schema
        self.bad_schema(schema)

    def test_depth_limit_is_controlled_for_deep_json(self):
        value = 1
        for _ in range(s.MAX_DEPTH + 1):
            value = [value]
        self.invalid(value, True)

    def test_excessive_unique_comparisons_stop_with_a_controlled_error(self):
        self.invalid(list(range(1000)), {'uniqueItems': True})

    def test_error_diagnostics_do_not_echo_instance_or_property_names(self):
        private_value = 'synthetic-sensitive-value'
        with self.assertRaises(s.SchemaError) as captured:
            s.validate_instance(private_value, {'const': 'something else'})
        self.assertNotIn(private_value, str(captured.exception))
        with self.assertRaises(s.SchemaError) as captured:
            s.validate_instance({private_value: 1}, {'additionalProperties': False})
        self.assertNotIn(private_value, str(captured.exception))


if __name__ == '__main__':
    unittest.main()
