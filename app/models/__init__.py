"""ORM 模型包：导入即向 Base.metadata 注册全部实体，供 create_all 建表。"""

from app.models import entities  # noqa: F401
