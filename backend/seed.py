"""数据初始化（空库兜底）。

原则：
  - 规则：数据库为空时，以《内容合规性审核规则_飞书文档》提取的规则作为兜底
    （唯一权威来源，见 import_rules.py），不再使用任何旧的演示规则。
  - 视频号：系统**绝不**自动创建任何视频号。所有视频号必须由用户真实指定，
    避免出现非真实数据。
"""
from database import db_cursor


def seed_if_empty() -> None:
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM rules")
        rules_empty = cur.fetchone()["c"] == 0
    if rules_empty:
        import import_rules
        import_rules.import_rules()
