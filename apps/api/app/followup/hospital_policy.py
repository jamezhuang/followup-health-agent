"""Shared scope/coverage policy, independent of particular cities or hospitals."""
import re
from .hospital_planner import PROVINCES
from .hospital_knowledge import region_terms


def directory_policy(plan, goal):
    region = plan.region.rstrip('省市自治区特别行政区')
    if len(region_terms(plan.region))>1:
        scope,low,high='regional_group',8,10
    elif len(plan.regions)>1:
        scope,low,high='multi_region',8,10
    elif re.search(r'(?:区|县|镇|街道)$', plan.region) and not re.search(r'地区|区域$',plan.region):
        scope, low, high = 'district', 4, 6
    elif (region in PROVINCES and region not in {'北京','上海','天津','重庆'}) or re.search(r'省$|自治区$',plan.region):
        scope, low, high = 'province', 8, 10
    elif not plan.region or re.search(r'全国|中国',plan.region):
        scope, low, high = 'national', 8, 10
    else:
        scope, low, high = 'city', 5, 8
    # "有几个" often requests examples. Explicit totals require a statistical
    # source and must not be answered with the number of examples we found.
    task = 'count' if re.search(r'总数|总共|一共|总计|合计|统计|准确.{0,4}数量|到底有多少',goal) else 'list'
    numbers = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
    request = re.search(r'(?:列出|列举|推荐|介绍|给我|找|看看)\s*([0-9]{1,2}|[一二两三四五六七八九十])\s*(?:家|个|所)',goal)
    explicit = min(20, max(1, int(request[1]) if request[1].isdigit() else numbers[request[1]])) if request else None
    if explicit: low = high = explicit
    return {'task':task,'scope':scope,'minimum':min(4,low),
            'target_min':low,'target_max':high,'explicit_count':explicit,
            'diversify_regions':scope in {'province','national','multi_region','regional_group'},
            'allow_scope_relaxation':False}
