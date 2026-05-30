import os
import asyncio
import time
from enum import Enum
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, select
import grpc
import aio_pika

import notifications_pb2
import notifications_pb2_grpc

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5433/orderdb")
NOTIFICATION_HOST = os.getenv("NOTIFICATION_GRPC_HOST", "localhost")
NOTIFICATION_PORT = os.getenv("NOTIFICATION_GRPC_PORT", "50051")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
MAX_RETRIES = 3


# ─── Circuit Breaker ───
class CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple in-memory circuit breaker for gRPC calls to Notification Service."""

    def __init__(self, failure_threshold=5, recovery_timeout=30.0, half_open_max_calls=2):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.state = CBState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self.half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, coro):
        async with self._lock:
            if self.state == CBState.OPEN:
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = CBState.HALF_OPEN
                    self.half_open_calls = 0
                    print("[CB] Transition OPEN → HALF_OPEN")
                else:
                    raise Exception("Circuit breaker is OPEN — Notification Service unavailable")

            if self.state == CBState.HALF_OPEN and self.half_open_calls >= self.half_open_max_calls:
                raise Exception("Circuit breaker is HALF_OPEN — max probe calls reached")

            if self.state == CBState.HALF_OPEN:
                self.half_open_calls += 1

        try:
            result = await coro
            async with self._lock:
                if self.state == CBState.HALF_OPEN:
                    self.state = CBState.CLOSED
                    self.failure_count = 0
                    print("[CB] Transition HALF_OPEN → CLOSED")
                elif self.state == CBState.CLOSED:
                    self.failure_count = 0
            return result
        except Exception:
            async with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.state == CBState.HALF_OPEN:
                    self.state = CBState.OPEN
                    print("[CB] Transition HALF_OPEN → OPEN")
                elif self.failure_count >= self.failure_threshold:
                    self.state = CBState.OPEN
                    print(f"[CB] Transition CLOSED → OPEN (failures={self.failure_count})")
            raise


cb = CircuitBreaker(failure_threshold=3, recovery_timeout=15.0)

engine = create_async_engine(DATABASE_URL, echo=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    total = Column(Float, nullable=False)
    status = Column(String, default="PENDING")
    retry_count = Column(Integer, default=0)


class OrderCreate(BaseModel):
    user_id: int
    total: float


class OrderOut(BaseModel):
    id: int
    user_id: int
    total: float
    status: str
    notification_status: str | None = None
    model_config = {"from_attributes": True}


app = FastAPI(title="Order Service")


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(retry_consumer())


async def send_notification_grpc(user_id: int, message: str, channel: str):
    try:
        channel_grpc = grpc.aio.insecure_channel(f"{NOTIFICATION_HOST}:{NOTIFICATION_PORT}")
        stub = notifications_pb2_grpc.NotificationsServiceStub(channel_grpc)
        response = await stub.SendNotification(
            notifications_pb2.SendNotificationRequest(
                user_id=user_id,
                message=message,
                channel=channel,
            ),
            timeout=5,
        )
        return response.success
    except Exception as exc:
        print(f"[gRPC error] {exc}")
        return False


async def publish_retry(order_id: int):
    try:
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        async with connection:
            ch = await connection.channel()
            await ch.declare_queue("order_retry", durable=True)
            await ch.default_exchange.publish(
                aio_pika.Message(
                    body=str(order_id).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key="order_retry",
            )
        print(f"[RabbitMQ] Published retry for order {order_id}")
    except Exception as exc:
        print(f"[RabbitMQ publish error] {exc}")


async def retry_consumer():
    while True:
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            async with connection:
                ch = await connection.channel()
                queue = await ch.declare_queue("order_retry", durable=True)
                print("[Retry Consumer] Started, waiting for messages...")
                async with queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        async with message.process():
                            order_id = int(message.body.decode())
                            print(f"[Retry Consumer] Processing order {order_id}")
                            await process_retry(order_id)
        except Exception as exc:
            print(f"[Retry Consumer] Error: {exc}, reconnecting in 5s...")
            await asyncio.sleep(5)


async def process_retry(order_id: int):
    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        db_order = result.scalar_one_or_none()
        if not db_order:
            print(f"[Retry] Order {order_id} not found")
            return
        if db_order.status == "CONFIRMED":
            print(f"[Retry] Order {order_id} already confirmed")
            return
        if db_order.retry_count >= MAX_RETRIES:
            db_order.status = "NOTIFICATION_PERMANENTLY_FAILED"
            await session.commit()
            print(f"[Retry] Order {order_id} permanently failed after {MAX_RETRIES} attempts")
            return

        try:
            success = await cb.call(
                send_notification_grpc(
                    user_id=db_order.user_id,
                    message=f"Your order #{order_id} has been received.",
                    channel="push",
                )
            )
        except Exception as exc:
            print(f"[Retry] CB blocked: {exc}")
            success = False

        db_order.retry_count += 1
        if success:
            db_order.status = "CONFIRMED"
            print(f"[Retry] Order {order_id} confirmed on retry #{db_order.retry_count}")
        else:
            if db_order.retry_count >= MAX_RETRIES:
                db_order.status = "NOTIFICATION_PERMANENTLY_FAILED"
                print(f"[Retry] Order {order_id} permanently failed")
            else:
                await publish_retry_delayed(order_id)
                print(f"[Retry] Order {order_id} re-queued for attempt #{db_order.retry_count + 1}")
        await session.commit()


async def publish_retry_delayed(order_id: int):
    try:
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        async with connection:
            ch = await connection.channel()
            await ch.declare_queue("order_retry", durable=True)
            await ch.default_exchange.publish(
                aio_pika.Message(
                    body=str(order_id).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key="order_retry",
            )
    except Exception as exc:
        print(f"[RabbitMQ re-publish error] {exc}")


@app.post("/orders", response_model=OrderOut, status_code=201)
async def create_order(order: OrderCreate, background_tasks: BackgroundTasks):
    async with async_session() as session:
        db_order = Order(user_id=order.user_id, total=order.total, status="PENDING", retry_count=0)
        session.add(db_order)
        await session.commit()
        await session.refresh(db_order)
        background_tasks.add_task(process_notification_initial, db_order.id, db_order.user_id)
        return db_order


async def process_notification_initial(order_id: int, user_id: int):
    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        db_order = result.scalar_one_or_none()
        if not db_order:
            return

        try:
            success = await cb.call(
                send_notification_grpc(
                    user_id=user_id,
                    message=f"Your order #{order_id} has been received.",
                    channel="push",
                )
            )
        except Exception as exc:
            print(f"[Initial] CB blocked or error: {exc}")
            success = False

        if success:
            db_order.status = "CONFIRMED"
            print(f"[Initial] Order {order_id} confirmed")
        else:
            await publish_retry(order_id)
            db_order.status = "NOTIFICATION_FAILED"
            print(f"[Initial] Order {order_id} failed, queued for retry")
        await session.commit()


@app.get("/orders", response_model=list[OrderOut])
async def list_orders():
    async with async_session() as session:
        result = await session.execute(select(Order))
        orders = result.scalars().all()
        out = []
        for o in orders:
            item = OrderOut.model_validate(o)
            if o.status == "PENDING":
                item.notification_status = "notification pending"
            elif o.status == "CONFIRMED":
                item.notification_status = "notification sent"
            elif o.status == "NOTIFICATION_FAILED":
                item.notification_status = "retry in progress"
            elif o.status == "NOTIFICATION_PERMANENTLY_FAILED":
                item.notification_status = "notification permanently failed — manual intervention required"
            out.append(item)
        return out


@app.get("/health")
async def health():
    return {"status": "ok", "circuit_breaker": cb.state.value}
