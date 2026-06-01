
```bash
curl -X GET http://localhost:8000/health
```

Create a tenant
```bash

```

## Books

Get books for a tenant (bad request)
```bash
curl -X GET http://localhost:8000/books
```

Get books for a tenant (404)
```bash
curl -X GET http://localhost:8000/books \
  -H "X-Tenant: demo" | jq
```

Get books for a tenant
```bash
curl -X GET http://localhost:8000/books \
  -H "X-Tenant: demo" | jq
```

Get books for a tenant
```bash
curl -X GET "http://localhost:8000/books?year_min=2005" \
  -H "X-Tenant: demo" | jq
```
Get books for a tenant
```bash
curl -X GET "http://localhost:8000/books?year_min=2005" \
  -H "X-Tenant: demo" | jq
```

## Activity

Get activity log for a tenant
```bash
curl -X GET http://localhost:8000/activity \
  -H "X-Tenant: demo" | jq
```