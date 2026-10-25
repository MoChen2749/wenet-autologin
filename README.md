# wenet-autologin

让校园网认证「永久在线」的小脚本：跑在 OpenWrt / iStoreOS 软路由上，自动向校园网 portal 发起登录，**把账号绑定到路由器（网关）本身**，于是家里/宿舍的所有设备经 NAT 直接上网，不用每台设备各自登录。

针对 **华为 AC + WISPr** 拦截、前端为 **umi/React 的 wenet portal** 的校园网编写（登录接口 `/portal-conversion/api/v3/portal/connect`，请求体 AES-128-CBC 加密）。已在 iStoreOS 24.10 上实测。

## 特性

- **开机自启**：procd 服务（`S95`），进程崩溃自动拉起；
- **断线自动重登**：每 60 秒探测，掉线立即登录，失败指数退避（最长 10 分钟）；
- **验证到位**：登录接口返回 200 后再探测一次，确认真的能上网才算成功；
- **参数自动发现**：portal 地址、`userip`/`wlanacname`/`nas-ip`/`usermac` 都从 AC 的 302 跳转里解析，不写死任何 IP；
- **零额外依赖**：Python 标准库 + 系统 `openssl` 命令；
- **凭据安全**：只存路由器 `/etc/wenet-autologin.conf`，root 0600。

## 工作原理

```
路由器 WAN ──(被 AC 拦截)──> 302 Location: http://<portal>?userip=...&usermac=<WAN MAC>
     │
     ├─ 1. GET http://connect.rom.miui.com/generate_204  不跟随重定向 → 判断是否在线
     ├─ 2. GET <portal>/?<query>                          拿 Set-Cookie: redirectUrl=...
     ├─ 3. POST /portal-conversion/api/v3/portal/connect  body = AES(JSON) 的 hex
     └─ 4. 解密响应，statusCode==200 后再探测一次 → 成功
```

拓扑示意：

```
你的设备 ──LAN──> 软路由（本脚本运行在路由器上）──WAN──> 校园网 AC ──> Internet
                        └── 登录请求从这里发出，AC 按路由器 WAN MAC 记账
```

也就是说：**登录绑定的是网关设备，而不是某台电脑**。

## 兼容性检查

在路由器上执行（未登录状态下）：

```sh
curl -sI http://connect.rom.miui.com/generate_204 | head -5
```

如果看到 `HTTP/1.1 302` 且 `Location` 里带 `userip=...&wlanacname=...&nas-ip=...&usermac=...`，并且门户页是 `wenet` 风格的登录页，那大概率可以直接用。

不同学校/厂商（锐捷、Dr.COM、深澜 SRun 等）接口完全不同，此脚本不适用。

## 安装

### 前置条件

1. 路由器可 SSH 登录（OpenWrt 默认 dropbear，建议配好密钥）；
2. 路由器上装好依赖（一般 iStoreOS/OpenWrt 自带 python3，缺 `openssl-util` 时补上）：

   ```sh
   ssh root@192.168.1.1 'opkg update && opkg install python3 openssl-util'
   ```

3. 路由器就是你的出口网关（脚本要在 WAN 已连上、但被 portal 拦截的状态下工作）。

### 一键部署（推荐）

```sh
git clone https://github.com/<your-name>/wenet-autologin.git
cd wenet-autologin

ROUTER=root@192.168.1.1 SSH_KEY=~/.ssh/id_ed25519 ./install.sh
```

脚本会：上传守护脚本和 procd 服务 → 启用开机自启 → 提示输入校园网账号和密码（输入不回显）→ 重启服务并打印日志。

只更新代码、保留已有凭据：

```sh
ROUTER=root@192.168.1.1 SSH_KEY=~/.ssh/id_ed25519 ./install.sh --keep-conf
```

### 手动部署

```sh
ROUTER=root@192.168.1.1

ssh $ROUTER 'umask 022; cat > /usr/local/bin/wenet-autologin.py && chmod 755 /usr/local/bin/wenet-autologin.py' < wenet-autologin.py
ssh $ROUTER 'cat > /etc/init.d/wenet-autologin && chmod 755 /etc/init.d/wenet-autologin' < files/etc/init.d/wenet-autologin

# 凭据：文件内容为 PHONE=手机号 和 PASS=密码
ssh $ROUTER 'umask 077; vi /etc/wenet-autologin.conf && chmod 600 /etc/wenet-autologin.conf'
# 或从模板拷：
# ssh $ROUTER 'umask 077; cat > /etc/wenet-autologin.conf && chmod 600 /etc/wenet-autologin.conf' < files/etc/wenet-autologin.conf.example

ssh $ROUTER '/etc/init.d/wenet-autologin enable && /etc/init.d/wenet-autologin restart'
ssh $ROUTER 'tail -f /var/log/wenet-autologin.log'
```

## 配置

### 凭据文件 `/etc/wenet-autologin.conf`

```ini
PHONE=13800000000
PASS=your-password
```

文件必须是 root 所有、权限 0600；支持 `USER`/`PASSWORD` 作为 `PHONE`/`PASS` 的别名。

### 环境变量（可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WENET_CONF` | `/etc/wenet-autologin.conf` | 凭据文件路径 |
| `WENET_PROBE` | `http://connect.rom.miui.com/generate_204` | 探测 URL（会依次尝试多个备用地址） |
| `WENET_INTERVAL` | `60` | 在线时的探测间隔（秒） |
| `WENET_PORTAL_PORT` | `9090` | portal 端口（AC 跳转里没带端口时使用） |
| `WENET_AES_KEY` | `1234567890000000` | portal 前端使用的 AES key（key == iv） |
| `WENET_UA` | 内置 Chrome UA | 请求 User-Agent |
| `WENET_OPENSSL` | `openssl` | openssl 可执行文件路径 |
| `WENET_LOG` | 无 | 额外把日志写到这个文件（`--log` 也会设置它） |

## 命令行用法

```sh
python3 /usr/local/bin/wenet-autologin.py                 # 守护模式（procd 就是这么启动的）
python3 /usr/local/bin/wenet-autologin.py --once          # 只尝试登录一次（cron 可用），在线则跳过
python3 /usr/local/bin/wenet-autologin.py --check         # 只探测：online=True/False，打印拦截 URL
python3 /usr/local/bin/wenet-autologin.py --once --force --portal-base http://<portal>:9090 --query 'userip=...'
```

## 服务与日志

```sh
/etc/init.d/wenet-autologin status
/etc/init.d/wenet-autologin restart
/etc/init.d/wenet-autologin stop
tail -f /var/log/wenet-autologin.log     # 日志超过 200KB 自动轮转为 .1
```

日志示例：

```
2026-05-01 08:00:12 daemon started (interval 60s)
2026-05-01 08:00:12 captive portal detected, logging in
2026-05-01 08:00:13 LOGIN OK session=12345678 verify_online=True
2026-05-01 08:00:13 online
```

## 更新密码

```sh
read -rsp 'campus password: ' P
printf 'PHONE=%s\nPASS=%s\n' '13800000000' "$P" \
  | ssh root@192.168.1.1 'umask 077; cat > /etc/wenet-autologin.conf && chmod 600 /etc/wenet-autologin.conf'
unset P
ssh root@192.168.1.1 /etc/init.d/wenet-autologin restart
```

## 常见问题

**1. 日志里 `LOGIN FAILED ... error=81` / `The subscriber is deregistered or the password is incorrect`**

账号或密码不对；也可能是账号本身未开通/被冻结。先在浏览器里手工登录一次确认凭据有效。注意：如果设备**已经在线**，用错密码提交也可能返回 200，所以务必看 `verify_online=` 和是否真的能上网。

**2. 启动后一直 `network not ready, retry in 15s`**

WAN 还没拿到地址（PPPoE 未拨通/DHCP 未就绪）。这属于正常退避，等 15 秒会自动重试；若长期如此，先检查路由器 WAN 状态。

**3. 想强制重新登录**

```sh
/etc/init.d/wenet-autologin stop
python3 /usr/local/bin/wenet-autologin.py --once --force
/etc/init.d/wenet-autologin start
```

**4. 同账号其他设备会被踢吗？**

多数校园网一个账号限制在线设备数。本方案里路由器占着这个账号的会话；其他设备若再登录同账号可能互踢（路由器这边掉线后会自动重登回来，但会来回抢）。建议把同账号只给网关使用。

**5. 无感认证 / MAC 绑定接口能用吗？**

portal 前端另有 `POST /portal-conversion/api/v3/mac/saveMac`（无感认证）接口，但需要账号开通对应服务。若返回 `ACCOUNT_NOT_IN_LDAP`，说明账号不在校园 LDAP 中，用不了；此时本方案通过“由路由器发起登录”达到同样效果：AC 的会话记录里 `mac` 就是路由器 WAN 口 MAC。

**6. 换了学校/portal 改了怎么办？**

先按“兼容性检查”确认 302 跳转格式；再从 portal 前端 JS 里找新的 key/接口路径，改 `WENET_AES_KEY` 或代码里的路径即可。原理见 [docs/protocol.md](docs/protocol.md)。

**7. 路由器升级/重置固件后失效？**

`/usr/local/bin`、`/etc/wenet-autologin.conf`、`/etc/init.d` 会随固件重置丢失，重跑一次 `install.sh` 即可。配置文件也可自行放进 `/etc/config` 之外备份。

## 协议文档

登录接口、加密方式、错误码、无感认证接口等细节见 [docs/protocol.md](docs/protocol.md)。

## 安全说明

- 密码以明文形式保存在路由器本地文件里（`0600`）。这是守护进程能自动登录的前提；如果介意，可以把密码放进只有 root 可读的别处并修改 `WENET_CONF`。
- `install.sh` 输入密码时不会回显，也不会把密码写进日志或 shell 历史。
- 不要把 `/etc/wenet-autologin.conf`、抓包日志、浏览器 profile 提交到仓库；本仓库的 `.gitignore` 已忽略 `*.conf`（模板除外）和日志。
- 逆向别人校园网的 portal 前请先确认符合你们学校的网络使用规定。

## 卸载

```sh
ssh root@192.168.1.1 '
  /etc/init.d/wenet-autologin stop
  /etc/init.d/wenet-autologin disable
  rm -f /etc/init.d/wenet-autologin /usr/local/bin/wenet-autologin.py \
        /etc/wenet-autologin.conf /var/log/wenet-autologin.log*
'
```

## License

[MIT](LICENSE)

> 本项目仅用于学习与个人网络自动化。使用者需自行确认登录行为符合所在校园网的管理规定。
