import json, sys
def dict_case_everywhere_except_last(params):
    result = {}
    for key, value in params.items():
        if len(result) == 0 or key != next(reversed(list(result.keys()))):
            if isinstance(value, str): 
                result[key] = value.casefold()
            else:
                result[key] = type(value)(value)
    return result

params = json.load(sys.stdin)
print(dict_case_everywhere_except_last(params))