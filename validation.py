"""Portable record contracts shared by archive writers, readers and API responses."""
import math


def object_value(value, label):
    if not isinstance(value, dict):
        raise ValueError('Invalid ' + label)
    return value


def json_value(value, depth=0):
    if depth > 64:
        raise ValueError('Metadata nesting exceeds limit')
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            json_value(item, depth + 1)
        return
    raise ValueError('Invalid metadata value')


def metadata(value):
    object_value(value, 'metadata')
    json_value(value)
    if 'loras' in value and (not isinstance(value['loras'], list) or
                             not all(isinstance(v, str) for v in value['loras'])):
        raise ValueError('Invalid LoRA metadata')
    return value


def extras(value):
    if not isinstance(value, list):
        raise ValueError('Invalid extra prompts')
    for item in value:
        object_value(item, 'extra prompt')
        if item.get('role') not in ('positive', 'negative', 'other') or not isinstance(item.get('text'), str):
            raise ValueError('Invalid extra prompt')
        if 'label' in item and not isinstance(item['label'], str):
            raise ValueError('Invalid prompt label')
    return value


def details(value):
    object_value(value, 'prompt details')
    if set(value) - {'warnings', 'provenance', 'stages'}:
        raise ValueError('Unknown or reserved prompt detail field')
    warnings = value.get('warnings')
    warnings = [] if warnings is None else warnings
    if not isinstance(warnings, list) or not all(isinstance(v, str) for v in warnings):
        raise ValueError('Invalid prompt warnings')
    provenance = value.get('provenance')
    provenance = {} if provenance is None else provenance
    object_value(provenance, 'provenance')
    json_value(provenance)
    stages = value.get('stages')
    stages = [] if stages is None else stages
    if not isinstance(stages, list):
        raise ValueError('Invalid stages')
    for stage in stages:
        object_value(stage, 'stage')
        if not all(isinstance(stage.get(k), str) for k in ('positive', 'negative')):
            raise ValueError('Invalid stage text')
        metadata(stage.get('meta', {}))
        if 'node' in stage and not isinstance(stage['node'], str):
            raise ValueError('Invalid stage node')
        if 'sources' in stage and (not isinstance(stage['sources'], list) or
                                   not all(isinstance(v, str) for v in stage['sources'])):
            raise ValueError('Invalid stage sources')
    return {'warnings': warnings, 'provenance': provenance, 'stages': stages}


def safe_details(value):
    """Old libraries may contain unsafe imports: never merge arbitrary record keys."""
    clean = {}
    if isinstance(value, dict):
        for key in ('warnings', 'provenance', 'stages'):
            try:
                clean[key] = details({key: value.get(key)})[key]
            except (ValueError, TypeError):
                pass
    return details(clean)


def snapshot(value):
    object_value(value, 'generation snapshot')
    for key in ('positive', 'negative'):
        if key in value and not isinstance(value[key], str):
            raise ValueError('Invalid snapshot text')
    extras(value.get('extra', []))
    metadata(value.get('meta', {}))
    details({k: value[k] for k in ('warnings', 'provenance', 'stages') if k in value})
    json_value(value)
    return value


def generation(value):
    for key in ('root', 'path', 'snapshot'):
        if not isinstance(value.get(key), str):
            raise ValueError('Invalid generation ' + key)
    for key in ('mtime', 'created_at'):
        if type(value.get(key)) not in (int, float) or not math.isfinite(value[key]):
            raise ValueError('Invalid generation timestamp')
    for key in ('size', 'extractor'):
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError('Invalid generation ' + key)
