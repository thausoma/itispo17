# Финальный проект — Week 17 (notifications-s19)

## Что это?
Распределённая система уведомлений на микросервисной архитектуре:
User Service, Order Service, Notification Service и API Gateway.

## Зачем?
Демонстрация интеграции REST, gRPC, GraphQL, RabbitMQ (retry), Circuit Breaker,
Graceful Degradation, Docker, Kubernetes и CI/CD в едином проекте.

## Технологический стек
- **Язык**: Python 3.11
- **Фреймворк**: FastAPI
- **Базы данных**: PostgreSQL 15 (отдельный инстанс на сервис)
- **gRPC**: grpcio — межсервисное общение Order ↔ Notification
- **GraphQL**: Ariadne (в Gateway) — агрегация REST-ответов, статическая схема
- **Сообщения / Retry**: RabbitMQ + aio-pika
- **CI/CD**: GitHub Actions (`.github/workflows/ci.yml`)
- **Контейнеризация**: Docker + Docker Compose
- **Оркестрация**: Kubernetes (манифесты в `k8s/`)

## Как запустить

### Локально (Docker Compose)
```bash
docker-compose up --build
```
API Gateway: http://localhost:8090

### Kubernetes (Zero Downtime)

**Требования:** kind, kubectl, Docker

```bash
# 1. Собрать образы
docker-compose build

# 2. Создать кластер kind с пробросом порта
cat > kind-config.yaml << 'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30090
        hostPort: 8090
        protocol: TCP
EOF

kind delete cluster
kind create cluster --config kind-config.yaml

# 3. Сохранить образы в tar и загрузить в kind
docker save week-17-gateway -o ~/gateway.tar
docker save week-17-user-service -o ~/user-service.tar
docker save week-17-order-service -o ~/order-service.tar
docker save week-17-notification-service -o ~/notification-service.tar

kind load image-archive ~/gateway.tar
kind load image-archive ~/user-service.tar
kind load image-archive ~/order-service.tar
kind load image-archive ~/notification-service.tar

# 4. Применить манифесты
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/

# 5. Ждать готовности
kubectl wait --for=condition=ready pod --all -n notifications-s19 --timeout=120s

# 6. Проверить
kubectl get pods -n notifications-s19
curl http://localhost:8090/health
```

## Endpoints
- REST API: `http://localhost:8090/api/{users,orders,notifications}`
- GraphQL: `http://localhost:8090/graphql`
- Health: `http://localhost:8090/health`

## Проверка работы
```bash
# 1. Создать пользователя
curl -X POST http://localhost:8090/api/users \
  -H "Content-Type: application/json" \
  -d '{"name":"Kirill","email":"kirill@test.com"}'

# 2. Создать заказ (триггерит gRPC + Circuit Breaker + RabbitMQ retry)
curl -X POST http://localhost:8090/api/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id":1,"total":99.99}'

# 3. Проверить уведомления
curl http://localhost:8090/api/notifications

# 4. GraphQL
curl -X POST http://localhost:8090/graphql \
  -H "Content-Type: application/json" \
  -d '{"query":"query { users { id name email } orders { id status } notifications { id message } }"}'

# 5. Проверить Circuit Breaker
curl http://localhost:8090/api/orders
```

## Демонстрация Zero Downtime

В одном терминале запустите бесконечные запросы к Gateway:
```bash
while true; do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/health
  sleep 0.3
done
```

В другом терминале выполните rolling restart:
```bash
kubectl rollout restart deployment/gateway -n notifications-s19
kubectl rollout status deployment/gateway -n notifications-s19 --timeout=120s
```

Во время rolling update Kubernetes создаёт новый под перед остановкой старого (`maxSurge: 1`, `maxUnavailable: 0`), а readinessProbe гарантирует, что трафик поступает только на готовый контейнер. В первом терминале не должно появиться кодов 5xx или длительных пропаданий ответа.

## Ответы на вопросы самопроверки

### 1. Критерии разделения на микросервисы
Границы выбраны по принципам **Domain-Driven Design (DDD)**:

| Сервис | Bounded Context | Почему отдельно |
|--------|-----------------|-----------------|
| User Service | Управление идентичностью | Пользователи живут независимо от заказов. Могут быть созданы до первого заказа. |
| Order Service | Бизнес-логика заказов | Транзакционная целостность, жизненный цикл заказа (PENDING → CONFIRMED). |
| Notification Service | Доставка уведомлений | Cross-cutting concern, но вынесен отдельно, т.к. (а) может падать независимо — не ломает создание заказа, (б) масштабируется отдельно при росте нагрузки, (в) может разрабатываться другой командой. |

**Coupling:** Order Service не зависит от доступности Notification Service для создания заказа. При сбое gRPC заказ создаётся со статусом `PENDING`, а уведомление уходит в очередь retry.

### 2. Почему этот стек
| Технология | Почему выбрана | Рассмотренные альтернативы |
|------------|----------------|---------------------------|
| **Python + FastAPI** | Единообразие стека курса, async из коробки, автогенерация OpenAPI | Go — лучше для gRPC, но требует контекст-переключения в команде; Node.js — callback-hell |
| **PostgreSQL** | ACID для заказов и пользователей, знакомая экосистема | MongoDB — для notifications подошла бы (схема редко меняется), но PG выбран для единообразия |
| **gRPC** | Скорость, строгие контракты, streaming на будущее | REST для межсервисного — проще, но медленнее и без типизации; GraphQL — оверхед для machine-to-machine |
| **RabbitMQ** | Простота для MVP, встроенные retry/DLX | Kafka — для высокой нагрузки и логов, но избыточен для 3 сервисов |
| **Docker + Compose** | Локальная разработка, один `docker-compose up` | Podman — аналог, но Docker стандарт де-факто |
| **K8s (манифесты)** | Zero Downtime деплой, production-ready оркестрация | Docker Swarm — проще, но устаревает; Nomad — меньше экосистема |

### 3. Как справляемся с ошибками
- **Retry**: RabbitMQ очередь `order_retry` + фоновый consumer (до 3 попыток).
- **Circuit Breaker**: 3 ошибки подряд → OPEN на 15 сек → HALF_OPEN (2 пробных запроса) → CLOSED. Реализован в `order-service/main.py`.
- **Graceful Degradation**: заказ создаётся даже если Notification Service упал, статус `PENDING`. `GET /orders` возвращает `notification_status` с пояснением: "retry in progress" или "permanently failed — manual intervention required".
- **Health endpoint** (`/health`) возвращает текущее состояние Circuit Breaker.

### 4. Zero Downtime деплой
K8s Deployment для каждого сервиса:
- `replicas: 2` — отказоустойчивость.
- `strategy: RollingUpdate` с `maxSurge: 1`, `maxUnavailable: 0` — новый под поднимается до остановки старого.
- `readinessProbe` / `livenessProbe` — Kubernetes не направляет трафик, пока под не готов.
- `initContainers` с `nc` — гарантия порядка старта (БД → сервисы → Gateway).
- Service `ClusterIP` для межсервисного общения, `NodePort` для Gateway (порт 30090 → 8090 на хосте).

### 5. Сложности интеграции
1. **gRPC + asyncio в одном контейнере**: Notification Service слушает REST (uvicorn, порт 8131) и gRPC (порт 50051). Первоначально был конфликт портов — решено разнесением.
2. **RabbitMQ consumer в фоне FastAPI**: Нужно было запустить `asyncio.create_task()` в `startup`, не блокируя приём HTTP-запросов, но и не теряя сообщения при рестарте.
3. **Docker Compose race condition**: `depends_on` запускает контейнер, но не ждёт готовности БД. Решено через `healthcheck` + `condition: service_healthy`.
4. **Circuit Breaker + SQLAlchemy async**: CB работает в async-контексте, нужна была синхронизация состояния через `asyncio.Lock`.
5. **Загрузка образов в kind**: `kind load docker-image` не всегда стабилен с tmp dir. Решено через `docker save` → `kind load image-archive`.

### 6. Что бы улучшили за месяц
1. **Observability**: Prometheus метрики (`/metrics`), Grafana дашборд, distributed tracing (Jaeger/Zipkin).
2. **API Gateway production-ready**: Envoy или nginx вместо FastAPI-прокси — rate limiting, auth, caching.
3. **Kafka вместо RabbitMQ**: При росте нагрузки — Kafka с persistent log и replay.
4. **MongoDB для notifications**: Логи уведомлений — высокий объём, слабая схема, хорошо ложится на document store.
5. **gRPC Streaming**: Server-Side Streaming для real-time push-уведомлений.
6. **Helm chart**: Параметризация K8s манифестов для разных сред (dev/staging/prod).
7. **GitOps**: ArgoCD для автоматического деплоя из Git.
8. **Ingress Controller**: Внешний доступ через Ingress вместо NodePort.

## Проектный код
notifications-s19
