"""Offline, fail-closed JSON Schema subset for native structured worker results.

This is NOT a full JSON Schema implementation. Modern numeric exclusive bounds
and $ref sibling semantics are used. Supported keywords are listed in KEYWORDS;
every other keyword (including format, $id, $anchor and unevaluatedProperties)
is rejected. References are root-relative JSON Pointers, never network requests.
The regex subset is Python's ASCII-compatible common syntax: unsupported escapes,
extensions, and quantified groups are rejected. Schema authors remain responsible
for reasonable patterns; this module is not an untrusted-regex execution sandbox.
Validation has depth/node limits; no defaults/coercion/annotation application.
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import math
import re
from urllib.parse import unquote


class SchemaError(ValueError):
    """Malformed/unsupported schema or a nonconforming JSON instance."""


class _LimitError(SchemaError):
    pass


TYPES = {'null', 'boolean', 'object', 'array', 'number', 'integer', 'string'}
ANNOTATIONS = {'title', 'description', '$schema', 'default', 'examples'}
KEYWORDS = ANNOTATIONS | {
    'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
    'const', 'minLength', 'maxLength', 'pattern', 'minItems', 'maxItems',
    'uniqueItems', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'multipleOf', 'anyOf', 'oneOf', 'allOf', 'not', '$defs', 'definitions', '$ref',
}
MAX_DEPTH = 64
MAX_NODES = 20000


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _json(value, depth=0, active=None, budget=None):
    """Reject Python-only values, nonfinite numbers, cycles and excessive input."""
    active = set() if active is None else active
    budget = [MAX_NODES] if budget is None else budget
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise _LimitError('JSON depth or node limit exceeded')
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise SchemaError('Nonfinite JSON number')
        return
    if type(value) not in (list, dict):
        raise SchemaError('Non-JSON value')
    marker = id(value)
    if marker in active:
        raise SchemaError('Cyclic Python value is not JSON')
    active.add(marker)
    try:
        if isinstance(value, dict):
            if any(type(key) is not str for key in value):
                raise SchemaError('JSON object keys must be strings')
            children = value.values()
        else:
            children = value
        for child in children:
            _json(child, depth + 1, active, budget)
    finally:
        active.remove(marker)


def _equal(left, right):
    # JSON equality distinguishes true from 1 but regards 1 and 1.0 as equal.
    if _number(left) and _number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    return left == right


def _unique(values):
    if len(values) * max(0, len(values) - 1) // 2 > MAX_NODES:
        raise _LimitError('JSON equality comparison limit exceeded')
    return all(not any(_equal(value, earlier) for earlier in values[:index])
               for index, value in enumerate(values))


def _pattern(pattern):
    if not isinstance(pattern, str) or len(pattern) > 1024:
        raise SchemaError('pattern must be a string of at most 1024 characters')
    # Fail closed on regex extensions/escapes whose Python/ECMAScript meaning
    # differs, and on quantified groups that often conceal explosive backtracking.
    if re.search(r'\(\?(?!:|=|!)|\)(?:\*|\+|\?|\{)|(?:[+*?]|\{[0-9]+(?:,[0-9]*)?\})\+', pattern):
        raise SchemaError('Unsupported pattern extension or quantified group')
    translated = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == '\\':
            index += 1
            if index == len(pattern):
                raise SchemaError('Invalid pattern escape')
            escaped = pattern[index]
            if escaped not in r'dDwWnrtfvux\\.^$*+?{}[]()|/-':
                raise SchemaError('Unsupported pattern escape')
            translated.append('\\' + escaped)
        elif char == '[':
            in_class = True
            translated.append(char)
        elif char == ']':
            in_class = False
            translated.append(char)
        elif char == '.' and not in_class:
            translated.append('[^\\n\\r\\u2028\\u2029]')
        elif char == '$' and not in_class:
            # ECMAScript $ without multiline requires end-of-input. Python's
            # own $ additionally accepts a final LF, so use its strict anchor.
            translated.append('\\Z')
        else:
            translated.append(char)
        index += 1
    try:
        return re.compile(''.join(translated), re.ASCII)
    except re.error:
        raise SchemaError('Invalid or unsupported pattern') from None


def _resolve(root, reference):
    if not isinstance(reference, str) or not reference.startswith('#'):
        raise SchemaError('Only local JSON Pointer $ref is supported')
    if re.search(r'%(?![A-Fa-f0-9]{2})', reference):
        raise SchemaError('Invalid URI fragment escape')
    try:
        fragment = unquote(reference[1:], errors='strict')
    except UnicodeError:
        raise SchemaError('Invalid URI fragment encoding') from None
    if not fragment:
        return root
    if not fragment.startswith('/'):
        raise SchemaError('Named anchors are unsupported')
    node = root
    try:
        for segment in fragment[1:].split('/'):
            if re.search(r'~(?![01])', segment):
                raise SchemaError('Invalid JSON Pointer escape')
            segment = segment.replace('~1', '/').replace('~0', '~')
            if isinstance(node, dict):
                node = node[segment]
            elif isinstance(node, list) and re.fullmatch(r'0|[1-9][0-9]*', segment):
                node = node[int(segment)]
            else:
                raise SchemaError('Unresolvable local $ref')
    except (KeyError, IndexError, ValueError, UnicodeError):
        raise SchemaError('Unresolvable local $ref') from None
    if type(node) not in (dict, bool):
        raise SchemaError('$ref must resolve to a schema')
    return node


def validate_schema(schema):
    """Check the supported schema subset; return None or raise SchemaError."""
    _json(schema)
    visited = set()
    def visit(node, depth=0):
        if depth > MAX_DEPTH:
            raise _LimitError('Schema depth limit exceeded')
        if type(node) is bool:
            return
        if type(node) is not dict:
            raise SchemaError('Schema must be an object or boolean')
        if id(node) in visited:
            return
        visited.add(id(node))
        if set(node) - KEYWORDS:
            raise SchemaError('Unsupported schema keyword')
        if 'type' in node:
            kinds = node['type'] if isinstance(node['type'], list) else [node['type']]
            if not kinds or any(type(kind) is not str or kind not in TYPES for kind in kinds) or not _unique(kinds):
                raise SchemaError('Invalid type constraint')
        for keyword in ('title', 'description', '$schema'):
            if keyword in node and not isinstance(node[keyword], str):
                raise SchemaError(keyword + ' must be a string')
        if 'examples' in node and not isinstance(node['examples'], list):
            raise SchemaError('examples must be an array')
        if 'required' in node:
            value = node['required']
            if not isinstance(value, list) or any(type(item) is not str for item in value) or not _unique(value):
                raise SchemaError('required must contain unique strings')
        if 'enum' in node and (not isinstance(node['enum'], list) or not node['enum'] or not _unique(node['enum'])):
            raise SchemaError('enum must contain unique JSON values')
        for keyword in ('minLength', 'maxLength', 'minItems', 'maxItems'):
            if keyword in node and (type(node[keyword]) is not int or node[keyword] < 0):
                raise SchemaError(keyword + ' must be a nonnegative integer')
        if 'uniqueItems' in node and type(node['uniqueItems']) is not bool:
            raise SchemaError('uniqueItems must be a boolean')
        for keyword in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'):
            if keyword in node and (not _number(node[keyword]) or keyword == 'multipleOf' and node[keyword] <= 0):
                raise SchemaError('Invalid numeric constraint: ' + keyword)
        if 'pattern' in node:
            _pattern(node['pattern'])
        for keyword in ('properties', '$defs', 'definitions'):
            if keyword in node:
                if not isinstance(node[keyword], dict):
                    raise SchemaError(keyword + ' must be an object')
                for child in node[keyword].values():
                    visit(child, depth + 1)
        for keyword in ('additionalProperties', 'items', 'not'):
            if keyword in node:
                visit(node[keyword], depth + 1)
        for keyword in ('anyOf', 'oneOf', 'allOf'):
            if keyword in node:
                value = node[keyword]
                if not isinstance(value, list) or not value:
                    raise SchemaError(keyword + ' must be a nonempty schema array')
                for child in value:
                    visit(child, depth + 1)
        if '$ref' in node:
            visit(_resolve(schema, node['$ref']), depth + 1)
    visit(schema)


def _is_type(value, kind):
    return {'null': value is None, 'boolean': type(value) is bool,
            'object': type(value) is dict, 'array': type(value) is list,
            'string': type(value) is str, 'number': _number(value),
            'integer': _number(value) and (type(value) is int or value.is_integer())}[kind]


def validate_instance(value, schema):
    """Validate a JSON value locally; unsupported schemas fail before matching."""
    validate_schema(schema)
    _json(value)
    budget = [MAX_NODES]
    active = set()
    def check(instance, node, depth=0):
        budget[0] -= 1
        if depth > MAX_DEPTH or budget[0] < 0:
            raise _LimitError('Validation depth or node limit exceeded')
        if node is True:
            return
        if node is False:
            raise SchemaError('Instance rejected by false schema')
        marker = (id(instance), id(node))
        if marker in active:
            raise _LimitError('Non-progressing recursive schema')
        active.add(marker)
        try:
            if '$ref' in node:
                check(instance, _resolve(schema, node['$ref']), depth + 1)
            if 'type' in node:
                kinds = node['type'] if isinstance(node['type'], list) else [node['type']]
                if not any(_is_type(instance, kind) for kind in kinds):
                    raise SchemaError('Instance type mismatch')
            if 'const' in node and not _equal(instance, node['const']):
                raise SchemaError('const mismatch')
            if 'enum' in node and not any(_equal(instance, item) for item in node['enum']):
                raise SchemaError('enum mismatch')
            if isinstance(instance, dict):
                if any(key not in instance for key in node.get('required', [])):
                    raise SchemaError('Missing required property')
                properties = node.get('properties', {})
                for key, child in instance.items():
                    check(child, properties.get(key, node.get('additionalProperties', True)), depth + 1)
            if isinstance(instance, list):
                if len(instance) < node.get('minItems', 0) or 'maxItems' in node and len(instance) > node['maxItems']:
                    raise SchemaError('Array size constraint failed')
                if node.get('uniqueItems') is True and not _unique(instance):
                    raise SchemaError('Array items are not unique')
                if 'items' in node:
                    for child in instance:
                        check(child, node['items'], depth + 1)
            if isinstance(instance, str):
                if len(instance) < node.get('minLength', 0) or 'maxLength' in node and len(instance) > node['maxLength']:
                    raise SchemaError('String length constraint failed')
                if 'pattern' in node and not _pattern(node['pattern']).search(instance):
                    raise SchemaError('String pattern constraint failed')
            if _number(instance):
                for keyword, compare in [('minimum', lambda a, b: a >= b), ('maximum', lambda a, b: a <= b),
                                         ('exclusiveMinimum', lambda a, b: a > b), ('exclusiveMaximum', lambda a, b: a < b)]:
                    if keyword in node and not compare(instance, node[keyword]):
                        raise SchemaError('Numeric bound constraint failed')
                if 'multipleOf' in node:
                    ratio = Fraction(Decimal(str(instance))) / Fraction(Decimal(str(node['multipleOf'])))
                    if ratio.denominator != 1:
                        raise SchemaError('multipleOf constraint failed')
            for child in node.get('allOf', []):
                check(instance, child, depth + 1)
            def matches(child):
                try:
                    check(instance, child, depth + 1)
                    return True
                except _LimitError:
                    raise
                except SchemaError:
                    return False
            if 'anyOf' in node and not any(matches(child) for child in node['anyOf']):
                raise SchemaError('anyOf constraint failed')
            if 'oneOf' in node and sum(matches(child) for child in node['oneOf']) != 1:
                raise SchemaError('oneOf constraint failed')
            if 'not' in node and matches(node['not']):
                raise SchemaError('not constraint failed')
        finally:
            active.remove(marker)
    check(value, schema)
