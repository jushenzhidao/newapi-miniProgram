FROM python:3.12-slim

# curl：便于进容器手动排查（healthcheck 走 python 探针，不依赖 curl）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# uv：快速安装依赖（ghcr.io 在本机网络不稳定，改从 PyPI 装 uv 二进制）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /srv

# ⚠️ 顺序关键：pyproject.toml 里 hatchling 声明 packages = ["app"]，
#    构 wheel 时 app/ 必须已存在，否则 uv pip install 直接失败。
#    所以先 COPY app，再装依赖。
COPY pyproject.toml ./
COPY app ./app
RUN uv pip install --system -e .

# 其余运行期资源（放依赖层之后：改 alembic 不会触发重装依赖）
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000

# 默认命令由 docker-compose 覆盖；容器单跑时用 API
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
