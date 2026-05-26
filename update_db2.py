from app import create_app
from models import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    print("⏳ 正在为 Chapter 表增加讲义字段...")
    try:
        db.session.execute(text("ALTER TABLE chapter ADD COLUMN content_summary TEXT NULL;"))
        db.session.commit()
        print("✅ 更新成功！现在可以运行 app.py 了。")
    except Exception as e:
        print("⚠️ 字段可能已存在或出现错误:", e)