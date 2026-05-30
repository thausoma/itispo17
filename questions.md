# Вопросы для самопроверки

## 1. Какие критерии вы использовали при разделении монолита на микросервисы (или при проектировании сервисов)?

Границы выбраны по принципам **Domain-Driven Design (DDD)**:

| Сервис | Bounded Context | Почему отдельно |
|--------|-----------------|-----------------|
| User Service | Управление идентичностью | Пользователи живут независимо от заказов. Могут быть созданы до первого заказа. |
| Order Service | Бизнес-логика заказов | Транзакционная целостность, жизненный цикл заказа (PENDING → CONFIRMED). |
| Notification Service | Доставка уведомлений | Cross-cutting concern, но вынесен отдельно, т.к. (а) может падать независимо — не ломает создание заказа, (б) масштабируется отдельно при росте нагрузки, (в) может разрабатываться другой командой. |

**Coupling:** Order Service не зависит от доступности Notification Service для создания заказа. При сбое gRPC заказ создаётся со статусом `PENDING`, а уведомление уходит в очередь retry.

---

## 2. Почему вы выбрали именно этот стек технологий (язык, БД, протоколы)?

| Технология | Почему выбрана | Рассмотренные альтернативы |
|------------|----------------|---------------------------|
| **Python + FastAPI** | Единообразие стека курса, async из коробки, автогенерация OpenAPI | Go — лучше для gRPC, но требует контекст-переключения в команде; Node.js — callback-hell |
| **PostgreSQL** | ACID для заказов и пользователей, знакомая экосистема | MongoDB — для notifications подошла бы (схема редко меняется), но PG выбран для единообразия |
| **gRPC** | Скорость, строгие контракты, streaming на будущее | REST для межсервисного — проще, но медленнее и без типизации; GraphQL — оверхед для machine-to-machine |
| **RabbitMQ** | Простота для MVP, встроенные retry/DLX | Kafka — для высокой нагрузки и логов, но избыточен для 3 сервисов |
| **Docker + Compose** | Локальная разработка, один `docker-compose up` | Podman — аналог, но Docker стандарт де-факто |
| **K8s (манифесты)** | Zero Downtime деплой, production-ready оркестрация | Docker Swarm — проще, но устаревает; Nomad — меньше экосистема |

---

## 3. Как ваши сервисы справляются с ошибками (retry, circuit breaker, graceful degradation)?

### Retry
- При создании заказа gRPC-запрос в Notification Service выполняется в фоне (`BackgroundTasks`).
- При ошибке — публикация в RabbitMQ очередь `order_retry`.
- **Retry Consumer** (фоновый воркер в Order Service) читает очередь и переотправляет до 3 попыток.

### Circuit Breaker
- Реализован в `order-service/main.py`: `CircuitBreaker` класс с состояниями `CLOSED → OPEN → HALF_OPEN → CLOSED`.
- Порог: 3 ошибки подряд → OPEN на 15 секунд.
- В состоянии OPEN — быстрый отказ, без ожидания таймаута gRPC.
- HALF_OPEN — 2 пробных запроса для проверки восстановления.

### Graceful Degradation
- Если Notification Service недоступен — заказ **всё равно создаётся**.
- Клиент получает заказ со статусом `PENDING`.
- `GET /orders` возвращает `notification_status` с пояснением: "retry in progress" или "permanently failed — manual intervention required".
- Health endpoint (`/health`) возвращает текущее состояние Circuit Breaker.

---

## 4. Как организован деплой и обновление вашей системы без простоя (Zero Downtime)?

### Локально: Docker Compose
```bash
docker-compose up --build
```
- Healthcheck'и для PostgreSQL и RabbitMQ.
- `depends_on` с `condition: service_healthy`.
- `restart: on-failure` для всех сервисов.

### Production: Kubernetes
Манифесты в `k8s/`:
- **Namespace** `notifications-s19` — изоляция.
- **ConfigMap** — централизованная конфигурация.
- **Deployment** для каждого сервиса:
  - `replicas: 2` — отказоустойчивость.
  - `strategy: RollingUpdate` с `maxSurge: 1`, `maxUnavailable: 0` — **Zero Downtime**.
  - `readinessProbe` / `livenessProbe` — Kubernetes не направляет трафик, пока под не готов.
  - `initContainers` с `busybox nc` — ожидание готовности зависимостей (БД, RabbitMQ, gRPC).
- **Service** `ClusterIP` для межсервисного общения, `NodePort` для Gateway (порт 30090 → хост 8090).

### Демонстрация Zero Downtime
```bash
# Терминал 1 — бесконечные запросы
while true; do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/health
  sleep 0.3
done

# Терминал 2 — rolling restart
kubectl rollout restart deployment/gateway -n notifications-s19
kubectl rollout status deployment/gateway -n notifications-s19 --timeout=120s
```

---

## 5. Что было самым сложным в интеграции всех компонентов вместе?

1. **gRPC + asyncio в одном контейнере**: Notification Service слушает REST (uvicorn, порт 8131) и gRPC (порт 50051). Первоначально был конфликт портов — решено разнесением.
2. **RabbitMQ consumer в фоне FastAPI**: Нужно было запустить `asyncio.create_task()` в `startup`, не блокируя приём HTTP-запросов, но и не теряя сообщения при рестарте.
3. **Docker Compose race condition**: `depends_on` запускает контейнер, но не ждёт готовности БД. Решено через `healthcheck` + `condition: service_healthy`.
4. **Circuit Breaker + SQLAlchemy async**: CB работает в async-контексте, нужна была синхронизация состояния через `asyncio.Lock`.
5. **Загрузка образов в kind**: `kind load docker-image` не всегда стабилен с tmp dir. Решено через `docker save` → `kind load image-archive`.

---

## 6. Что бы вы улучшили, если бы у вас был еще месяц?

1. **Observability**: Prometheus метрики (`/metrics`), Grafana дашборд, distributed tracing (Jaeger/Zipkin).
2. **API Gateway production-ready**: Envoy или nginx вместо FastAPI-прокси — rate limiting, auth, caching.
3. **Kafka вместо RabbitMQ**: При росте нагрузки — Kafka с persistent log и replay.
4. **MongoDB для notifications**: Логи уведомлений — высокий объём, слабая схема, хорошо ложится на document store.
5. **gRPC Streaming**: Server-Side Streaming для real-time push-уведомлений.
6. **Helm chart**: Параметризация K8s манифестов для разных сред (dev/staging/prod).
7. **GitOps**: ArgoCD для автоматического деплоя из Git.
8. **Ingress Controller**: Внешний доступ через Ingress вместо NodePort.
