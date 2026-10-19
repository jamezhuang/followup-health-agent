"""Explicit user ranges only; upload timestamps never stand in for clinical dates."""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from .clinical_fields import INDICATORS, ALIASES


def record_scope(records, goal):
    dates=[]
    for year,month,day in re.findall(r'(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})',goal):
        try:
            dates.append(date(int(year),int(month),int(day)).isoformat())
        except ValueError:
            continue
    start=end=''
    if len(dates)>=2:
        start,end=sorted(dates[:2])
    elif dates and re.search(r'从|自|以来|之后|开始',goal):
        start=dates[0]
    recent=re.search(r'最近\s*(\d{1,3})\s*天',goal)
    if recent:
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
        start=(today-timedelta(days=max(1,int(recent[1]))-1)).isoformat()
        end=today.isoformat()
    labels={label for label in INDICATORS if label in goal}
    labels.update(value for alias,value in ALIASES.items() if alias.lower() in goal.lower())
    if not labels and re.search(r'只看|比较|对比|变化|改成|改为',goal):
        if '血压' in goal:labels={'收缩压','舒张压'}
        elif '血糖' in goal:labels={'空腹血糖','餐后2小时血糖','糖化血红蛋白'}
    scoped=[row for row in records if (not start or row['date']>=start) and (not end or row['date']<=end)
            and (not labels or row['label'] in labels)]
    return scoped, {'from':start,'to':end,'labels':sorted(labels)}
