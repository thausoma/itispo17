import os
import httpx
from fastapi import FastAPI, Request
from ariadne import QueryType, MutationType, make_executable_schema
from ariadne.asgi import GraphQL
from starlette.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

app = FastAPI(title="API Gateway", redirect_slashes=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_URL = os.getenv("USER_SERVICE_URL", "http://localhost:8001")
ORDER_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8002")
NOTIFICATION_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8131")

http_client = httpx.AsyncClient(timeout=10.0)


async def proxy_request(method: str, base_url: str, prefix: str, path: str, request: Request):
    if path:
        url = f"{base_url}/{prefix}/{path}"
    else:
        url = f"{base_url}/{prefix}"

    body = await request.body()
    resp = await http_client.request(
        method=method,
        url=url,
        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        content=body,
        params=request.query_params,
    )
    return JSONResponse(
        content=resp.json() if resp.text else None,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "transfer-encoding")},
    )


@app.api_route("/api/users", methods=["GET", "POST", "PUT", "DELETE"])
@app.api_route("/api/users/", methods=["GET", "POST", "PUT", "DELETE"])
@app.api_route("/api/users/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_users(request: Request, path: str = ""):
    return await proxy_request(request.method, USER_URL, "users", path, request)


@app.api_route("/api/orders", methods=["GET", "POST", "PUT", "DELETE"])
@app.api_route("/api/orders/", methods=["GET", "POST", "PUT", "DELETE"])
@app.api_route("/api/orders/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_orders(request: Request, path: str = ""):
    return await proxy_request(request.method, ORDER_URL, "orders", path, request)


@app.api_route("/api/notifications", methods=["GET", "POST", "PUT", "DELETE"])
@app.api_route("/api/notifications/", methods=["GET", "POST", "PUT", "DELETE"])
@app.api_route("/api/notifications/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_notifications(request: Request, path: str = ""):
    return await proxy_request(request.method, NOTIFICATION_URL, "notifications", path, request)


type_defs = """
    type User {
        id: ID!
        name: String!
        email: String!
    }
    type Order {
        id: ID!
        user_id: ID!
        total: Float!
        status: String!
    }
    type Notification {
        id: ID!
        user_id: ID!
        message: String!
        channel: String!
        created_at: String!
    }
    type Query {
        users: [User!]!
        orders: [Order!]!
        notifications: [Notification!]!
    }
    type Mutation {
        createNotification(user_id: ID!, message: String!, channel: String!): Notification!
    }
"""

query = QueryType()
mutation = MutationType()


@query.field("users")
async def resolve_users(*_):
    r = await http_client.get(f"{USER_URL}/users")
    return r.json()


@query.field("orders")
async def resolve_orders(*_):
    r = await http_client.get(f"{ORDER_URL}/orders")
    return r.json()


@query.field("notifications")
async def resolve_notifications(*_):
    r = await http_client.get(f"{NOTIFICATION_URL}/notifications")
    return r.json()


@mutation.field("createNotification")
async def resolve_create_notification(*_, user_id, message, channel):
    r = await http_client.post(
        f"{NOTIFICATION_URL}/notifications",
        json={"user_id": user_id, "message": message, "channel": channel},
    )
    return r.json()


schema = make_executable_schema(type_defs, query, mutation)
app.mount("/graphql", GraphQL(schema, debug=True))


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
