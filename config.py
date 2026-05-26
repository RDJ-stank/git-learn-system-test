# config.py
import os


class Config:
    # 秘钥，用于保护 Session 等安全数据，随便写一串乱码即可
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'baijia_biyesheji_key_2026'

    # 数据库连接配置
    # 格式: mysql+pymysql://用户名:密码@地址:端口/数据库名
    # 假设你的数据库名叫 learning_system
    SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://root:123456@localhost:3306/learning_system'

    # 也就是在控制台打印具体的 SQL 语句，方便调试
    SQLALCHEMY_TRACK_MODIFICATIONS = False