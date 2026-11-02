from sqlalchemy import text

from app.db.session import engine

try:
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        print("Database connected successfully!")
        print("Test result:", result.scalar())

except Exception as error:
    print("Database connection failed!")
    print(error)