"""Foreground FC definitions. Read-only network calls stay outside the actor loop."""


def tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required,
                       'additionalProperties': False}}}


MAPS_TOOLS = [
    tool('search_places', '用Google Places搜索真实POI。query保留地点/类别及明确城市或地标。附近没有位置先问用户，不能假设GPS。仅搜索，不启动导航；结果按相关性排序，返回顺序不代表距离最近；多个候选先让用户选择。',
         {'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
          'area': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                   'description': '搜索范围：逐字照抄用户说过的城市、地址或地标。用户没说过自己在哪、也没说地标时，不要调用本工具，先问用户在哪里。'},
          'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, ['query', 'area']),
    tool('get_weather', '直接查询天气，在本轮返回，不委托后台。location填明确城市及国家（例如巴黎 法国、深圳 中国），不得猜地点。date为today/tomorrow/day_after_tomorrow/now或YYYY-MM-DD；相对日期由工具按目标当地时区解析。Google不支持地点时真实回退Open-Meteo并标明来源。指定官网核验或复杂调研才delegate_task。',
         {'location': {'type': 'string', 'minLength': 1, 'maxLength': 200},
          'date': {'type': 'string', 'pattern': r'^(today|tomorrow|day_after_tomorrow|now|\d{4}-\d{2}-\d{2})$'}}, ['location', 'date']),
    tool('start_poi_navigation', '用户明确要求导航或选择导航候选后，使用本会话search_places返回的place_id启动模拟导航。不可编造ID；仅查询/推荐不能调用。只设置真实POI目的地，不计算路线、距离或到达时间。',
         {'place_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}, ['place_id']),
]
MAPS_BY_NAME = {item['function']['name']: item for item in MAPS_TOOLS}
READ_TOOLS = {'search_places', 'get_weather'}
