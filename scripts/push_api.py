# -*- coding: utf-8 -*-
"""旧兼容入口已停用。

它曾使用过期路径并直接写默认分支。需要同步完整页面时,请在用户明确同意后运行
``python scripts/push_stocks.py``;该脚本固定写入 dev 分支。
"""

raise SystemExit("push_api.py 已停用;请在用户确认后使用 push_stocks.py 写入 dev 分支")
