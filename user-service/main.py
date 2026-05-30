import os
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, select

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5432/userdb")

engine = create_async_engine(DATABASE_URL, echo=True, pool_pre_ping=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)


class UserCreate(BaseModel):
    name: str
    email: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    model_config = {"from_attributes": True}


app = FastAPI(title="User Service")


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.get("/users", response_model=list[UserOut])
async def list_users():
    async with async_session() as session:
        result = await session.execute(select(User))
        return result.scalars().all()


@app.post("/users", response_model=UserOut, status_code=201)
async def create_user(user: UserCreate):
    async with async_session() as session:
        db_user = User(name=user.name, email=user.email)
        session.add(db_user)
        await session.commit()
        await session.refresh(db_user)
        return db_user


@app.get("/health")
async def health():
    return {"status": "ok"}
