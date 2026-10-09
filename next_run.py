"""随机生成下一次定时任务的运行时间，避免每天在完全相同的时刻访问 YouTube。

输出可直接用于 T3 update_scheduled_task 的 schedule 参数：
    {"type": "fixed_time", "timeOfDay": "07:23", "weekdays": [0, 1, 2, 3, 4, 6]}

weekdays 排除今天：新时间即使晚于现在，也不会在今天再触发一次。
"""

import json
import random
from datetime import datetime

WINDOW = ("06:30", "08:00")  # 本地时间，随机范围


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def main() -> None:
    m = random.randint(minutes(WINDOW[0]), minutes(WINDOW[1]))
    today = (datetime.now().weekday() + 1) % 7  # Python 周一=0 → T3 周日=0
    print(json.dumps({
        "type": "fixed_time",
        "timeOfDay": f"{m // 60:02d}:{m % 60:02d}",
        "weekdays": [d for d in range(7) if d != today],
    }))


if __name__ == "__main__":
    main()
