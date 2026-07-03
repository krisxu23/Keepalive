## UptimeKuma 

### 保活变量
```
STREAMLIT_APP_URL=https://python-xray-argo-yutian81.streamlit.app
```

### http_code
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://cfargo-domain/
```
return: 400 - OK!

### POST
- 地址
```
https://api.github.com/repos/yutian81/Keepalive/dispatches
```

- header
```json
{
  "Authorization": "Bearer <pat token>",
  "Accept": "application/vnd.github+json"
}
```

- body
```json
{
  "event_type": "streamlit",
  "client_payload": {
    "status": "{{ status }}"
  }
}
```
