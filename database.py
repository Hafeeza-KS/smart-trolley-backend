from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Debug: print the actual URL being used
print("Using DATABASE_URL:", DATABASE_URL)

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set. Check your .env file.")

# Create engine with SSL required
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={
        "sslmode": "require",
        "connect_timeout": 5
    }
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()