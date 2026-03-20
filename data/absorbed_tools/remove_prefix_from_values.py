import json, sys
params = json.load(sys.stdin)
def remove_prefix_from_values(d):
    def _remove_prefix(v):
        if v.startswith('_'):
            return v[1:]
        elif v.startswith("'"):
            return v[1:]
        else:
            return v
    for k,v in d.items():
        if isinstance(v, dict):
            v = remove_prefix_from_values(v)
        d[k] = _remove_prefix(v)
    return d
print(remove_prefix_from_values(params))