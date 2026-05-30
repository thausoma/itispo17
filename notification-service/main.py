import os
import asyncio
import threading
from concurrent import futures
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, DateTime, select
from datetime import datetime
import grpc
import uvicorn

import notifications_pb2
import notifications_pb2_grpc

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5434/notificationdb")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))

engine = create_async_engine(DATABASE_URL, echo=True, pool_pre_ping=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    message = Column(String, nullable=False)
    channel = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class NotificationCreate(BaseModel):
    user_id: int
    message: str
    channel: str


class NotificationOut(BaseModel):
    id: int
    user_id: int
    message: str
    channel: str
    created_at: datetime
    model_config = {"from_attributes": True}


_loop: asyncio.AbstractEventLoop | None = None


def serve_grpc():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    notifications_pb2_grpc.add_NotificationsServiceServicer_to_server(NotificationsServicer(), server)
    server.add_insecure_port(f"0.0.0.0:{GRPC_PORT}")
    server.start()
    print(f"gRPC server started on port {GRPC_PORT}")
    server.wait_for_termination()


class NotificationsServicer(notifications_pb2_grpc.NotificationsServiceServicer):
    def SendNotification(self, request, context):
        if _loop is None:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Event loop not ready")
            return notifications_pb2.SendNotificationResponse(success=False, notification_id="")
        future = asyncio.run_coroutine_threadsafe(self._save(request), _loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return notifications_pb2.SendNotificationResponse(success=False, notification_id="")

    async def _save(self, request):
        async with async_session() as session:
            db_notif = Notification(
                user_id=request.user_id,
                message=request.message,
                channel=request.channel,
            )
            session.add(db_notif)
            await session.commit()
            await session.refresh(db_notif)
            return notifications_pb2.SendNotificationResponse(success=True, notification_id=str(db_notif.id))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    thread = threading.Thread(target=serve_grpc, daemon=True)
    thread.start()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="Notification Service", lifespan=lifespan)


@app.get("/notifications", response_model=list[NotificationOut])
async def list_notifications():
    async with async_session() as session:
        result = await session.execute(select(Notification))
        return result.scalars().all()


@app.post("/notifications", response_model=NotificationOut, status_code=201)
async def create_notification(notification: NotificationCreate):
    async with async_session() as session:
        db_notif = Notification(
            user_id=notification.user_id,
            message=notification.message,
            channel=notification.channel,
        )
        session.add(db_notif)
        await session.commit()
        await session.refresh(db_notif)
        return db_notif


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8131)
