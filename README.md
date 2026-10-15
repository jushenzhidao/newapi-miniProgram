# wechat-tools-bff

AI 工具小程序矩阵统一 BFF（FastAPI + Celery）。所有小程序共用一套用户体系（**手机号主键**）、积分与任务队列；新增小程序 = `apps` 表插一行配置 + 前端换 appid。

## 能力一览

| 模块 | 说明 |
|---|---|
| 登录 | `wx.login` 静默游客态 → 关键动作前 `getPhoneNumber` 绑定，同手机号自动合并 |
| 积分 | 注册送 100 / 签到 +10 / 激励视频 +20（日限 10） / 抠图 -10，全小程序通用 |
| 任务 | 提交即冻结积分 → Celery 队列 → 上游分割模型 → 成功结算 / 失败原路退回 |
| 抠图上游 | `tencent`（腾讯云人像分割，MVP 默认）/ `oneapis`（透传）/ `selfhosted`（BiRefNet） |
| 安全 | 每请求 HMAC-SHA256 签名 + 时间窗 + nonce 防重放；JWT 会话令牌 |
| 虚拟支付 | 表已建、代码开关 `VPAY_ENABLED=false`，二期开启 |

## 快速开始

### 方式 A：Docker 一把梭（推荐）

```bash
cp .env.example .env        # 按注释填 WECHAT_APPID/SECRET、腾讯云密钥
docker compose up -d        # MySQL + Redis + BFF(8000) + Worker
docker compose exec bff alembic upgrade head   # 建表
```

### 方式 B：本机开发（uv）

```bash
uv sync
docker compose up -d mysql redis          # 只起数据层
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload      # API
uv run celery -A app.worker.celery_app worker -l info   # Worker（另开终端）
```

打开 <http://127.0.0.1:8000/api/docs> 可直接调接口（Swagger 不带签名头，联调时用 curl/小程序真机）。

## 目录结构

```
wechat-tools-bff/
├── app/
│   ├── main.py            # FastAPI 入口：签名中间件 + 路由 + 统一异常
│   ├── config.py          # pydantic-settings 配置
│   ├── db.py              # SQLAlchemy 2.0 引擎/会话
│   ├── models.py          # 8 张表：users/user_identities/apps/tasks/credit_logs/...
│   ├── schemas.py         # Pydantic 请求响应模型
│   ├── security.py        # HMAC 签名 + JWT
│   ├── wechat.py          # code2session / access_token / 手机号换取
│   ├── credits.py         # 积分服务（行锁 + 流水）
│   ├── upstream.py        # 抠图多上游适配器
│   ├── redis_client.py    # Redis 单例（连不上自动降级）
│   ├── deps.py            # 依赖：当前 app / 当前用户
│   ├── worker.py          # Celery：抠图任务状态机
│   └── routers/           # auth / tasks / credits
├── alembic/               # 迁移（0001_initial 全表）
├── docker-compose.yml     # mysql + redis + bff + worker
└── pyproject.toml
```

## API 一览

所有 `/api/*` 请求需带签名头（`/api/health`、文档除外）：

```
X-App-Id:   wx1234567890abcdef
X-Timestamp: 1728872000
X-Nonce:    随机一次性串
X-Sign:     hex(HMAC-SHA256(sign_key, "{app_id}\n{ts}\n{nonce}\n{sha256(body)}"))
Authorization: Bearer <token>   # 登录后接口必带
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/session` | wx.login 静默登录（游客态，送 100 积分） |
| POST | `/api/auth/bind-phone` | 手机号绑定 + 账号合并（换发新 token） |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/auth/profile` | 更新昵称/头像（chooseAvatar） |
| POST | `/api/tasks/cutout` | 提交抠图（扣 10 积分，返回 task_no） |
| GET | `/api/tasks/{task_no}` | 任务状态（前端轮询 2s） |
| GET | `/api/tasks` | 最近 20 条任务 |
| GET | `/api/credits/logs` | 积分流水 |
| POST | `/api/credits/checkin` | 每日签到（+10，409=已签） |
| POST | `/api/credits/ad-reward` | 激励视频奖励（+20，429=日限） |
| POST | `/api/auth/scan/create` | PC 端创建扫码会话（返回 state，用于生成小程序码 scene） |
| GET | `/api/auth/scan/status` | PC 端轮询扫码状态（confirmed 时带用户基础信息） |
| POST | `/api/auth/scan` | 小程序确认扫码（skipAuth 但需签名头，返回 JWT） |
| POST | `/api/auth/cancel` | 用户拒绝授权静默上报 |
| POST | `/api/credits/recharge-order` | 安卓微信 JSAPI 下单（返回 wx.requestPayment 参数，需 WXPAY_ENABLED） |
| GET | `/api/credits/recharge-guide` | iOS 站外引导充值页（合规绕过苹果虚拟支付红线） |
| POST | `/api/credits/recharge-notify` | 微信支付异步回调（免 BFF 签名，AES-GCM 解密 + 配额幂等发放） |

错误码约定：`402` 积分不足 / `409` 已签到 / `429` 广告日限 / `401xx` 签名与令牌 / `502` 上游失败。

## 新增一个小程序（目标 2 天）

### 方式一（推荐，2026-10-15 起）：启动自动 seed，免手工 SQL

1. mp 后台注册小程序，拿 `appid` / `AppSecret`
2. 生成 `sign_key`：`openssl rand -hex 32`
3. 在 `.env` 填：

```bash
BOOTSTRAP_APP_ID=wxNEWAPPID
BOOTSTRAP_APP_SECRET=<微信 AppSecret>
BOOTSTRAP_APP_SIGN_KEY=<上面生成的 32 字节 hex>
BOOTSTRAP_APP_NAME=AI图片放大
```

4. 启动（或重启）BFF —— `ensure_bootstrap_apps` 在 startup 把该行 upsert 进 `apps` 表；已存在则更新 `secret/sign_key/name`（支持密钥轮换），不存在则插入。`sign_key` 为空会跳过该行（绝不写空值炸表）。
5. 前端改 `appid` + 内置同样的 `sign_key`（对齐 `utils/config.js` 的 `SIGN_KEY`），编译上传即通。

> ⚠️ 前后端 `sign_key` 必须**逐字节一致**：BFF 用 `apps.sign_key` 验签，前端用它签名，任一字符不同所有请求被 401 拒。生成一次后同时粘贴到两处。

### 方式二（手动）：直接插 apps 表

```sql
INSERT INTO apps (app_id, name, secret, sign_key, tools, theme)
VALUES ('wxNEWAPPID', 'AI图片放大', 'secret-xxx', 'signkey-xxx',
  '[{"code":"cutout","cost":10,"enabled":true}]',
  '{"primary":"#F97316","title":"AI图片放大"}');
```

前端 uni-app 工程改 appid + 内置新 sign_key，编译上传即可。BFF 零改动（两种方式等价，方式一只把步骤固化进启动流程）。

## 抠图上游切换

`.env` 改 `UPSTREAM_MODE` 一行，重启生效：

| 模式 | 用途 | 前置 |
|---|---|---|
| `tencent` | MVP 默认，人像分割 0.01 元/次，月 1000 免费 | 开通腾讯云人脸识别（iai） |
| `oneapis` | 二期：抠图模型挂 oneapis 自定义渠道透传 | oneapis 配好渠道 |
| `selfhosted` | 自部署 rembg/BiRefNet 服务 | GPU 服务器 + 推理服务 |

注意：腾讯云接口必须判 `Response.Error` 字段——`RequestId` 存在不代表成功（适配器已处理）。

## 积分规则（.env 可调）

| 事件 | 积分 | 说明 |
|---|---|---|
| 注册赠送 | +100 | 游客态即送，零门槛体验 |
| 每日签到 | +10 | `checkin_logs` 唯一约束幂等 |
| 激励视频 | +20/次，日限 10 | 需流量主开通（累计 UV≥1000） |
| AI 抠图 | -10/张 | 提交即扣，失败原路退回 |

## 自动部署（GitHub Actions 构建 → GHCR → 服务器直拉）

**整体链路**：本地推代码到 GitHub → Actions 自动 build 镜像推 GHCR（tag `sha-<commit前7位>`）→ 服务器改 compose 里的 image tag → `docker compose pull` + `up -d`。

### 1. GitHub 建仓库 + 推送

```bash
cd wechat-tools-bff
git init && git add -A && git commit -m "init bff"
git remote add origin git@github.com:<you>/wechat-tools-bff.git
git push -u origin main
```

推送 `main`（或打 `v*` tag）即触发 `.github/workflows/build.yml`：构建并推送两个 tag

- `ghcr.io/<you>/wechat-tools-bff:sha-<前7位>` ← 生产钉这个
- `ghcr.io/<you>/wechat-tools-bff:latest` ← 便于回滚/测试

> Actions 用仓库自带的 `GITHUB_TOKEN` 推 GHCR，无需自建 secret；`permissions: packages: write` 已在 workflow 声明。

### 2. 服务器拉镜像部署

```bash
# ① 登录 GHCR（私有仓库首次需登录；用有 read:packages 权限的 token）
echo <GITHUB_TOKEN> | docker login ghcr.io -u <you> --password-stdin

# ② 准备配置：把 .env.example 复制为 .env，填齐（含 BOOTSTRAP_APP_*、NEWAPI_*、JWT_SECRET…）
cp .env.example .env
vi .env

# ③ 改 docker-compose.server.yml 里三处 image 的 tag 为本次 Actions 实际 sha-xxxxxxxx
#    （Actions 运行日志 / GHCR Packages 页面可查）

# ④ 首次启动：起数据层 + 建表
docker compose -f docker-compose.server.yml up -d mysql redis
docker compose -f docker-compose.server.yml exec bff alembic upgrade head

# ⑤ 起全量服务（bff 启动会自动把 BOOTSTRAP_APP_ID 写进 apps 表）
docker compose -f docker-compose.server.yml up -d

# ⑥ 探活（应返回 {"code":0,...}）
curl -s http://127.0.0.1:8010/api/health
```

### 3. 后续更新（每次只改 tag）

```bash
git push origin main                 # Actions 重新打 sha-xxxxxxxx 镜像
# 改 docker-compose.server.yml 三处 image tag → sha-xxxxxxxx
docker compose -f docker-compose.server.yml pull bff worker beat
docker compose -f docker-compose.server.yml up -d
```

### 4. Nginx 反代（TLS 终止）

`docker-compose.server.yml` 里 BFF 只绑 `127.0.0.1:8010`，对外由 Nginx/Caddy 终止 HTTPS 后反代：

```nginx
server {
    listen 443 ssl;
    server_name bff.oneapis.cn;
    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 5. 微信侧收尾

- 小程序后台 → 开发设置 → 服务器域名 → request 合法域名加 `https://bff.oneapis.cn`
- 前端 `miniprogram/utils/config.js` 的 `SIGN_KEY` 必须 = `.env` 的 `BOOTSTRAP_APP_SIGN_KEY`（同一串 `openssl rand -hex 32`）
- 安卓真支付：填齐 `WXPAY_*`（商户号/证书/私钥/v3key/notify_url）+ `WXPAY_ENABLED=true`，并在微信商户平台配置回调 `https://bff.oneapis.cn/api/credits/recharge-notify`

## 待办 / 二期

- [ ] 结果图转存 COS（MVP 直存上游 URL，腾讯云返回 URL 约 30 分钟有效，需前置 CDN 或尽快转存）
- [x] 扫码登录闭环（`/auth/scan/create` + `/scan/status` + `/scan` + `/cancel` 已补齐，PC 端需注册为 BFF 的 app）
- [x] 安卓充值 JSAPI + iOS 引导（`/credits/recharge-*` 已补齐；`WXPAY_ENABLED=true` + 商户配置后安卓真支付可用）
- [ ] 虚拟支付 VPay（独立 offerId 体系，二期）
- [ ] 连续签到奖励梯度
- [ ] 上游失败告警（Celery 重试耗尽通知）
