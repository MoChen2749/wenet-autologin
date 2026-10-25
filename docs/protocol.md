# 协议笔记：华为 AC + WISPr 校园网 portal

以下是在某一高校校园网（SSID 类似「xx校园网」）实测得到的协议细节，已做去敏处理（IP/MAC/账号均为占位符）。不同学校部署可能不同，但整体套路通常一致。

## 1. 拦截形态

未认证时访问任意 http 站点，AC 返回：

```http
HTTP/1.1 302 Moved Temporarily
Server: NetEngine
Location: http://<portal-ip>:9090?userip=<wan-ip>&wlanacname=<ac-name>&nas-ip=<nas-ip>&usermac=<wan-mac-dashed>
```

- `userip`：当前出口 IP（认证对象）；
- `usermac`：发送请求设备的 MAC，用 `-` 分隔（即登录绑定的设备）；
- `wlanacname` / `nas-ip`：AC 侧标识。

请求 WISPr 探测地址（如 `http://connect.rom.miui.com/generate_204`）时，AC 可能返回 WISPr XML（`MessageType=110`，登录页地址）。

portal 页面本身是 umi/React SPA，接口前缀 `/portal-conversion/api/v3/`。

## 2. 加密

请求体和响应体都是 **JSON → AES-128-CBC → hex**：

- key = iv = `1234567890000000`（16 字节 ASCII，硬编码在前端 bundle 里）；
- 填充：PKCS7（openssl 默认）；
- 请求体：`hex(AES(JSON字符串))`，作为原始 body 提交；
- 响应体：`hex` 字符串，解密后是 JSON。

前端实现（可搜 `encrypt`/`decrypt`）：

```js
const key = CryptoJS.enc.Utf8.parse("1234567890000000");
CryptoJS.AES.encrypt(JSON.stringify(obj), key, { iv: key, mode: CBC, padding: Pkcs7 }).ciphertext.toString()
```

## 3. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/portal-conversion/api/v3/portal/get/jsonConfig?filename=configration.json` | 页面配置（表单字段、提示文案），带 Authorization |
| POST | `/portal-conversion/api/v3/portal/connect` | **登录接口**（不需要 Authorization / Cookie） |
| GET | `/portal-conversion/api/v3/portal/connect/token` | 实测 405；token 来自页面 localStorage |
| GET | `/portal-conversion/api/v3/session/{plans,list,acctUniqueId}` | 套餐/会话信息 |
| POST | `/portal-conversion/api/v3/mac/saveMac` | 无感认证：绑定 MAC |
| GET/POST | `/portal-conversion/api/v3/mac/{getMacList,deleteMac}` | 查询/删除绑定 |
| GET | `/radacct-service/session/me?userIp=<wan-ip>` | 当前会话（用户名/MAC/IP） |

登录前先 `GET <portal>/?<query>`，服务端会 `Set-Cookie: redirectUrl=http://<portal>:80/?<query>`，把该值作为 `redirectUrl` 提交。

### 登录请求

```json
{
  "deviceType": "PC",
  "redirectUrl": "http://<portal-ip>:80/?userip=<wan-ip>&wlanacname=<ac-name>&nas-ip=<nas-ip>&usermac=<wan-mac-dashed>",
  "webAuthUser": "13800000000",
  "webAuthPassword": "<password>"
}
```

`webAuthUser` 是手机号，`webAuthPassword` 是密码。请求头只需 `Content-Type: application/json`（可带 Origin/Referer/UA）。

### 成功响应（解密后）

```json
{
  "statusCode": 200,
  "error": 0,
  "session": {
    "id": "12345678",
    "context": "<base64, 类似 JWT>",
    "token_expires_in": 0,
    "keepalive": false,
    "started_at": 1700000000,
    "keepalive_interval": 0,
    "network_changed": false
  },
  "token": "<128 hex>"
}
```

### 失败响应

```json
{
  "statusCode": 400,
  "error": 81,
  "errorDescription": "109026012|109020105|The subscriber is deregistered or the password is incorrect",
  "token": "<128 hex>"
}
```

常见 `errorDescription`：

- `The subscriber is deregistered or the password is incorrect` → 账号/密码错误，或账号未开通；
- `challenge already online` → 已在线，可视为成功；
- `phone already have N sessions` → 并发会话数限制；
- 其它还有套餐过期、账号冻结、未绑定套餐等文案。

**注意**：设备已经在线时，即使提交错误账号也可能返回 `statusCode=200`（会新建会话）。所以判断登录是否成功，应以“响应 200 且随后探测确实能上网”为准，而不是只看接口返回。

### 会话查询

```json
// GET /radacct-service/session/me?userIp=<wan-ip>
{
  "username": "138****0000",
  "mac": "<wan-mac-12hex>",
  "nasPortId": "trunk 1/5/142:4000.3072 0/0/0/0/0/0",
  "framedIPAddress": "<wan-ip>"
}
```

`mac` 即提交登录的设备 MAC（这里是路由器 WAN 口），所以从路由器登录 = 把账号绑定到网关设备。

## 4. 无感认证接口

前端 `/noSense` 页面使用：

```
POST /portal-conversion/api/v3/mac/saveMac
{
  "deviceType": "PC",
  "username": "13800000000",
  "mac": "<device-mac-12hex>",
  "password": "<password>"
}
```

实测在账号未开通该服务时返回明文 JSON：

```json
{"error": "ACCOUNT_NOT_IN_LDAP", "code": 1022, "description": "username is not find"}
```

说明该账号不在校园 LDAP 里，无法使用无感认证；此时采用“路由器主动登录”的方案达到同样的 MAC 绑定效果。

## 5. 适配其它 portal 的方法

1. 在未认证状态下 `curl -sI http://<probe>` 看 302 的 `Location`，确认参数名（`userip`/`usermac` 业界通用）；
2. 打开 portal 页面，从 bundle 里搜 `AES`、`encrypt`、`connect`，找 key/iv 和登录路径；
3. 用 `--once --force --portal-base ... --query ...` 在命令行验证一次；
4. 把常量/路径改到脚本里即可。
